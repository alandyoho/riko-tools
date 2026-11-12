"""Voice commands for the feeder, run from an iPhone Shortcut ("Run Script over SSH") via
siri.sh. Each command prints ONE short sentence, meant to be spoken back by Siri.

    siri.sh resume      resume a paused or stopped meal
    siri.sh status      state, power, food and water, last and next meal, bowl
    siri.sh pause       pause a meal that is preparing (it holds its food until resumed)
    siri.sh feed        an extra meal at the default amounts (5 g + 35 g), with guards;
                        on battery it wakes the feeder with the SwitchBot first
    siri.sh wake        press the SwitchBot to wake a sleeping feeder, then fix its clock
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import load
from riko import ERROR_CODES, FeedCtrl, FeederState, FoodLevel, Riko, Schedule, WaterLevel
import switchbot_press

LOG = Path(__file__).resolve().parent / "riko_state" / "siri.log"
JAM = 13          # grinder jammed with food: resume doesn't clear it (0 of 10, 2026-11-05)
RECENT_MIN = 20   # "feed" refuses within this long of the last meal - safe to say twice
DAILY_MAX_G = 60  # same ceiling as the monitor's daily_gram_ceiling
WAKE_WAIT_S = 60  # after a press, how long to wait for the feeder to come online
CLOCK_OK_S = 10   # closer than this counts as right
_press = switchbot_press.press   # replaced in tests


def _on_battery(st) -> bool:
    return (st._p("pwrStatus", {}) or {}).get("pwrType") == 1


async def _wake(r: Riko) -> tuple[bool, str]:
    """Press the SwitchBot on the feed button and wait for the feeder to come online. A press
    wakes a sleeping feeder without feeding (2026-11-09); on mains it only beeps."""
    mac = os.environ.get("RIKO_SWITCHBOT_MAC", "")
    if not mac:
        return False, "No SwitchBot is set up. Press the feeder's button by hand."
    res = await _press(mac)
    if not res.ok:
        return False, f"The SwitchBot press didn't go through ({res.error or 'no answer'}). Press the button by hand."
    for _ in range(int(WAKE_WAIT_S / 3)):
        await asyncio.sleep(3)
        try:
            st = await r.status()
        except Exception:
            continue
        if st.state != FeederState.SLEEPING:
            return True, ""
    return False, "I pressed the button, but the feeder didn't wake up."


def _log(cmd: str, said: str) -> None:
    try:
        with LOG.open("a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {cmd}: {said}\n")
    except OSError:
        pass


async def resume(r: Riko) -> str:
    st = await r.status()
    if st.state == FeederState.SLEEPING:
        return "The feeder is asleep on battery. There's no meal to resume."
    if st.state in (FeederState.PREPARING, FeederState.SERVING):
        return f"It's already {st.state.name.lower()} a meal. Nothing to resume."
    if st.state == FeederState.IDLE:
        return "The feeder is idle. There's no meal to resume."
    if st.state != FeederState.SUSPENDED:
        why = ERROR_CODES.get(st.state_param, f"code {st.state_param}")
        return f"The feeder is in {st.state.name.lower().replace('_', ' ')} ({why}), not a paused meal. I didn't send anything."

    code = st.state_param
    why = ERROR_CODES.get(code, f"code {code}")
    await r.feed_control(FeedCtrl.RESUME)
    for _ in range(10):                       # give it ~20 s to get going
        await asyncio.sleep(2)
        st = await r.status()
        if st.state != FeederState.SUSPENDED:
            return f"Resumed. The feeder is {st.state.name.lower()} again."
    tip = " A grinder jam usually has to be cleared by hand: take the bowl off first." if code == JAM else ""
    return f"I sent resume, but it's still stopped: {why}.{tip}"


async def pause(r: Riko) -> str:
    st = await r.status()
    if st.state == FeederState.SERVING:
        return "It's serving right now. Too late to pause."
    if st.state == FeederState.SUSPENDED:
        why = "paused" if st.state_param == 5 else ERROR_CODES.get(st.state_param, f"code {st.state_param}")
        return f"It's already stopped: {why}. Say resume feeder to continue."
    if st.state != FeederState.PREPARING:
        return f"The feeder is {st.state.name.lower().replace('_', ' ')}. There's no meal to pause."
    await r.feed_control(FeedCtrl.PAUSE)
    for _ in range(5):
        await asyncio.sleep(2)
        st = await r.status()
        if st.state == FeederState.SUSPENDED:
            return ("Paused. The meal is holding its food. Say resume feeder to finish it. "
                    "It will be logged as stopped manually.")
        if st.state != FeederState.PREPARING:
            return f"I sent pause, but the feeder is now {st.state.name.lower()}."
    return "I sent pause, but it's still preparing."


def _clock(t: float) -> str:
    """3:55 PM - reads naturally."""
    return time.strftime("%I:%M %p", time.localtime(t)).lstrip("0")


def _g(x: float) -> str:
    return f"{x:g}"


async def status(r: Riko, ledger_client=None, device: str = "", owner: int | None = None) -> str:
    st = await r.status()
    parts = []
    if st.state == FeederState.SUSPENDED:
        parts.append(f"Stopped: {ERROR_CODES.get(st.state_param, f'code {st.state_param}')}.")
    elif st.state == FeederState.FAULT:
        parts.append(f"Fault: {ERROR_CODES.get(st.state_param, f'code {st.state_param}')}.")
    else:
        parts.append(st.state.name.lower().replace("_", " ").capitalize()
                     .replace("Freshness protect", "Bowl pulled in for freshness") + ",")
    pwr = (st._p("pwrStatus", {}) or {})
    parts[-1] = parts[-1].rstrip(",.") + (f", on battery at {st.battery_pct} percent." if pwr.get("pwrType") == 1
                                          else ", on mains.")
    food, water = st.food_level, st.water_level
    lv = []
    if food in (FoodLevel.EMPTY, FoodLevel.INSUFFICIENT):
        lv.append(f"food is {'empty' if food == FoodLevel.EMPTY else 'low'}")
    if water in (WaterLevel.EMPTY, WaterLevel.INSUFFICIENT, WaterLevel.LOW):
        lv.append(f"water is {water.name.lower().replace('insufficient', 'very low')}")
    parts.append(("Heads up: " + " and ".join(lv) + ".") if lv else "Food and water OK.")

    if ledger_client is not None:
        try:
            led = await ledger_client.ledger(device, days=1, owner_user_id=owner)
            ok = [f for f in led.get("feed_list", []) if f.get("status") == 1 and (f.get("feed_weight") or 0) > 0]
            if ok:
                f = max(ok, key=lambda x: x["feed_time"])
                parts.append(f"Last meal {_clock(f['feed_time'])}, {_g(f['feed_weight'])} grams.")
        except Exception:
            pass
    sched = Schedule.from_status(st)
    if sched:
        lt = time.localtime()
        now = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec
        ahead = sorted(((sl.seconds_from_midnight - now) % 86400, sl) for _, sl in sched.active_today())
        if ahead:
            secs, sl = ahead[0]
            parts.append(f"Next {_clock(time.time() + secs)}, {_g(sl.food_g)} grams.")
    if st.state != FeederState.SLEEPING:
        try:
            gram, bowl = await asyncio.wait_for(r.live_scale(), 10)
            if gram > bowl / 2:
                left = max(0.0, gram - bowl)
                parts.append("Bowl is empty." if left < 1.5 else f"Bowl has {left:.0f} grams.")
            else:
                parts.append("The bowl is off.")
        except Exception:
            pass
    return " ".join(parts)


async def feed(r: Riko, ledger_client=None, device: str = "", owner: int | None = None) -> str:
    st = await r.status()
    asleep = st.state == FeederState.SLEEPING
    if not asleep:
        busy = _busy(st)
        if busy:
            return busy
    if not st.bowl_in:
        return "The bowl is off the tray. Put it back first."
    d = st.default_feed
    food, water = d.food_g or 5.0, d.water_g or 35.0
    if ledger_client is None:
        return "I couldn't check when he last ate, so I didn't feed. Try again in a minute."
    try:
        led = await ledger_client.ledger(device, days=1, owner_user_id=owner)
    except Exception:
        return "I couldn't check when he last ate, so I didn't feed. Try again in a minute."
    now = time.time()
    lt = time.localtime(now)
    midnight = now - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)
    served = [f for f in led.get("feed_list", []) if (f.get("feed_weight") or 0) > 0]
    recent = [f for f in served if now - f["feed_time"] < RECENT_MIN * 60]
    if recent:
        f = max(recent, key=lambda x: x["feed_time"])
        return (f"He had a meal at {_clock(f['feed_time'])}, under {RECENT_MIN} minutes ago, "
                f"so I didn't feed again.")
    fed_today = sum(f["feed_weight"] for f in served if f["feed_time"] >= midnight)
    sched = Schedule.from_status(st)
    sec_now = now - midnight
    still_planned = sum(sl.food_g for _, sl in (sched.active_today() if sched else [])
                        if sl.seconds_from_midnight > sec_now)
    if fed_today + still_planned + food > DAILY_MAX_G:
        return (f"That would put today over {DAILY_MAX_G} grams: {_g(fed_today)} so far and "
                f"{_g(still_planned)} still scheduled. I didn't feed.")
    woke = ""
    if asleep:                                # on battery: wake it with the SwitchBot first
        ok, msg = await _wake(r)
        if not ok:
            return msg + " I didn't feed."
        st = await r.status()
        if st.state == FeederState.PREPARING:
            return "I woke it, and it started a meal by itself."
        busy = _busy(st)
        if busy:
            return "I woke it, but " + busy[0].lower() + busy[1:]
        woke = "I woke it with the SwitchBot. "
    await r.feed(food, water)
    soak = 6.5 if _on_battery(st) else (d.soak_min or 10) + 0.5     # manual feeds soak ~6 min on battery
    return f"{woke}Feeding {_g(food)} grams with {_g(water)} of water. It'll be ready around {_clock(now + soak * 60)}."


def _busy(st) -> str:
    """Why the feeder can't take a feed right now, or "" if it can."""
    if st.state in (FeederState.PREPARING, FeederState.SERVING):
        return f"It's already {st.state.name.lower()} a meal."
    if st.state != FeederState.IDLE:
        why = "paused" if st.state_param == 5 else ERROR_CODES.get(st.state_param, f"code {st.state_param}")
        return f"The feeder is stopped ({why}). Sort that out first."
    return ""


async def wake(r: Riko) -> str:
    st = await r.status()
    said = []
    if st.state == FeederState.SLEEPING:
        ok, msg = await _wake(r)
        if not ok:
            return msg
        said.append("Awake.")
        st = await r.status()
    else:
        said.append("It's already awake.")
    if st.state == FeederState.IDLE:          # never touch the clock mid-meal
        try:
            d = await asyncio.wait_for(r.device_time(), 10)
            drift = d - time.time()
            if abs(drift) < CLOCK_OK_S:
                said.append("Its clock is right.")
            else:
                await r.sync_time()
                how = f"{abs(drift) / 60:.0f} minutes" if abs(drift) >= 90 else f"{abs(drift):.0f} seconds"
                said.append(f"Its clock was {how} {'fast' if drift > 0 else 'slow'}, so I synced it.")
        except Exception:
            said.append("I couldn't read its clock.")
    elif st.state in (FeederState.PREPARING, FeederState.SERVING):
        said.append(f"It's {st.state.name.lower()} a meal, so I left the clock alone.")
    if _on_battery(st):
        said.append("It's on battery and will sleep again in about a minute.")
    return " ".join(said)


COMMANDS = {"resume": resume, "status": status, "pause": pause, "feed": feed, "wake": wake}


async def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in COMMANDS:
        print("Say one of: " + ", ".join(COMMANDS) + ".")
        return 2
    logging.basicConfig(level=logging.ERROR)
    try:
        async with Riko.from_config(load()) as r:
            r.relogin = r.login               # share the account's one session with the monitor
            if cmd in ("status", "feed"):
                cfg = load()
                owner = getattr(cfg, "feeder_owner_id", 0) or None
                led = None
                if owner:
                    from neakasa import Feeder as NeakasaFeeder
                    led = NeakasaFeeder(client=r.client)
                said = await COMMANDS[cmd](r, led, r.device.device_name, owner)
            else:
                said = await COMMANDS[cmd](r)
    except Exception as exc:
        msg = str(exc)
        said = ("The feeder isn't answering. It may be asleep on battery or offline."
                if "20056" in msg or "6205" in msg else f"Something went wrong: {msg[:120]}")
    _log(cmd, said)
    print(said)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
