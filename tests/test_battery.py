"""The feeder on battery (BUGS.md #1, #11-#13): the on-battery alert, the rescue of a meal it
skips, the fast poll that catches its short pre-meal wake, and the clock hold that must not
protect a meal that never ran. Replays the 2026-11-09 tests. Simulated feeder, ledger, clock."""
import asyncio, json, sys, tempfile, time as real, types
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor as M
from riko import RikoStatus

clock = {"now": 0.0}
def at(h, m, s=0): clock["now"] = real.mktime((2026, 11, 9, h, m, s, 0, 0, -1))
M.time = types.SimpleNamespace(
    time=lambda: clock["now"], localtime=lambda t=None: real.localtime(clock["now"] if t is None else t),
    gmtime=real.gmtime, mktime=real.mktime, monotonic=lambda: clock["now"],
    strftime=lambda fmt, t=None: real.strftime(fmt, real.localtime(clock["now"]) if t is None else t))
SLOTS = [57300, 64500, 71700, 78900]                 # 15:55 17:55 19:55 21:55
PLAN = {"bEn": [1] * 4, "bDayEn": [1] * 4, "time": SLOTS, "rept": [127] * 4,
        "food": [5] * 4, "water": [35] * 4, "aud": [2] * 4, "type": [1] * 4}
ms = lambda: int(clock["now"] * 1000)

class Feeder:
    def __init__(self):
        self.state, self.pwr, self.feeds, self.fast, self.syncs = 5, 1, [], 0.0, []
        self.after_confirm = None                    # state to switch to during the confirm wait
        self.feed_fails = False
    async def status(self):
        return RikoStatus({"fdPlanStr": {"value": json.dumps(PLAN), "time": ms()},
                           "deviceState": {"value": {"state": self.state, "param": 0}, "time": ms()},
                           "pwrStatus": {"value": {"pwrType": self.pwr, "batPercentage": 90}, "time": ms()},
                           "defFdCfg": {"value": {"food": 5, "water": 35, "mixMin": 10}}})
    async def feed(self, f, w=None, **k):
        if self.feed_fails: raise RuntimeError("20056")
        self.feeds.append((f, w))
    async def device_time(self): return clock["now"] + self.fast
    async def sync_time(self, source=1): self.syncs.append(clock["now"]); self.fast = 0.0

class Ledger:
    def __init__(self): self.feeds = []
    async def ledger(self, *a, **k): return {"feed_list": self.feeds, "eat_list": []}
    def add(self, h, m, way="auto"):
        self.feeds.append({"feed_time": int(real.mktime((2026, 11, 9, h, m, 7, 0, 0, -1))), "status": 1,
                           "way": way, "feed_weight": 5, "water_weight": 34})

def make(kill=False):
    d = Path(tempfile.mkdtemp()); f, led, notes = Feeder(), Ledger(), []
    n = types.SimpleNamespace(action_needed=lambda t, b: notes.append(("ACT", t, b)) or True,
                              fyi=lambda t, b: notes.append(("FYI", t, b)) or True)
    if kill: (d / "KILL").touch()
    mon = M.Monitor(f, M.Store(d / "m.db"), n, M.Policy(killswitch=d / "KILL"), 0, False,
                    neakasa=led, device_name="dev")
    async def wait(s):                               # the confirm wait: time passes, the feeder may move
        clock["now"] += s
        if f.after_confirm is not None: f.state = f.after_confirm
    mon._sleep = wait
    return mon, f, led, notes

async def unplug(mon, f, h, m, s=0):
    at(h, m, s); f.pwr, f.state = 1, 0
    mon._check_feeder_power(await f.status())
    f.state = 5

async def main():
    # A. the on-battery alert: once per episode, and a note when it's back on mains
    mon, f, led, notes = make()
    await unplug(mon, f, 19, 31, 3)
    mon._check_feeder_power(await f.status())
    assert [n[:2] for n in notes] == [("ACT", "Feeder is on battery")] and "19:55 meal" in notes[0][2], notes
    at(21, 57); f.pwr = 0; mon._check_feeder_power(await f.status())
    assert notes[-1][:2] == ("FYI", "Feeder back on wall power") and "146 min" in notes[-1][2], notes[-1]
    print("A ok: on-battery alert once (names the 19:55 meal); back-on-mains note")

    # B. 19:55 on 11-09: woke 19:47:40, IDLE (it should be preparing) -> confirmed -> meal sent, once
    mon, f, led, notes = make()
    await unplug(mon, f, 19, 31, 3)
    at(19, 47, 40); f.state = 0
    await mon.battery_tick()
    assert f.feeds == [(5, 35)], f.feeds
    assert notes[-1][1] == "Sent the 19:55 meal — feeder on battery", notes[-1]
    clock["now"] += 3; await mon.battery_tick(); assert f.feeds == [(5, 35)], "once per meal"
    print("B ok: brief idle wake before 19:55 -> sent 5 + 35 after the confirm wait, once")

    # C. a meal it does run (21:55): IDLE reported, then PREPARING -> nothing sent, nothing owed after
    mon, f, led, notes = make()
    await unplug(mon, f, 20, 4, 56)                  # asleep after the rescue meal...
    mon.store.put_setting("feeder_battery_since", str(clock["now"] - 3600))
    led.add(20, 4, "manual")                         # the 19:55 rescue that evening
    at(21, 46, 19); f.state, f.after_confirm = 0, 1
    await mon.battery_tick()
    assert f.feeds == [], f.feeds
    f.after_confirm = None
    at(21, 53, 26); f.state = 0; await mon.battery_tick()       # idle after serving it
    assert f.feeds == [], "21:55 ran; nothing owed"
    print("C ok: idle-then-preparing is a meal it runs -> left alone, and not owed afterwards")

    # D. no doubling up: 19:55 skipped and its wake missed; 21:55 runs; nothing extra after it
    mon, f, led, notes = make()
    await unplug(mon, f, 19, 31, 3)
    at(21, 46, 19); f.state = 1; await mon.battery_tick()       # preparing 21:55
    at(21, 53, 26); f.state = 0; await mon.battery_tick()
    assert f.feeds == [], f.feeds
    print("D ok: a missed 19:55 is dropped once 21:55 starts preparing, never sent with it")

    # D2. a button press after the missed wake, before the next prep -> that one meal is sent...
    mon, f, led, notes = make()
    await unplug(mon, f, 19, 31)
    at(20, 10); f.state = 0; await mon.battery_tick()
    assert f.feeds == [(5, 35)] and notes[-1][1] == "Sent the 19:55 meal — feeder on battery", (f.feeds, notes[-1])
    # ...but not once the next meal's prep time (21:45) has passed: then only 21:55 is owed
    mon, f, led, notes = make()
    await unplug(mon, f, 19, 31)
    at(21, 50); f.state = 0; await mon.battery_tick()
    assert f.feeds == [(5, 35)] and notes[-1][1] == "Sent the 21:55 meal — feeder on battery", (f.feeds, notes[-1])
    # ...and three meals skipped while nobody was watching -> only the current one, never several
    mon, f, led, notes = make()
    await unplug(mon, f, 15, 31)
    at(19, 47, 40); f.state = 0; await mon.battery_tick()
    assert f.feeds == [(5, 35)] and notes[-1][1] == "Sent the 19:55 meal — feeder on battery", (f.feeds, notes[-1])
    print("D2 ok: a press before the next prep gets the missed meal; after it, only the current one; one at a time")

    # E. on mains at its prep time (unplugged 19:50) -> not a battery miss; nothing sent
    mon, f, led, notes = make()
    await unplug(mon, f, 19, 50)
    at(19, 51); f.state = 0; await mon.battery_tick()
    assert f.feeds == []
    # ...and a meal with a record is never resent
    mon, f, led, notes = make()
    await unplug(mon, f, 19, 31); led.add(19, 55, "manual")
    at(19, 58); f.state = 0; await mon.battery_tick()
    assert f.feeds == []
    print("E ok: unplugged after prep started, or a record exists -> nothing sent")

    # F. kill switch -> one alert to feed it from the app, no feed
    mon, f, led, notes = make(kill=True)
    await unplug(mon, f, 19, 31)
    at(19, 47, 40); f.state = 0
    await mon.battery_tick(); clock["now"] += 3; await mon.battery_tick()
    assert f.feeds == [] and [n[1] for n in notes].count("19:55 meal not sent — kill switch set") == 1, notes
    print("F ok: kill switch -> no feed, one alert")

    # G. it fell asleep before the feed went through -> still owed, sent at the next wake
    mon, f, led, notes = make()
    await unplug(mon, f, 19, 31)
    at(19, 47, 40); f.state, f.feed_fails = 0, True; await mon.battery_tick()
    assert f.feeds == []
    f.feed_fails = False; at(19, 57); await mon.battery_tick()  # e.g. a button press
    assert f.feeds == [(5, 35)]
    print("G ok: failed send -> retried the next time it is awake")

    # H. the fast poll: on battery near a meal (or with one owed); never on mains or far from meals
    mon, f, led, notes = make()
    await unplug(mon, f, 19, 31)
    at(19, 42); mon._battery_st = await f.status()
    assert mon.fast_poll_s(False) == 3.0 and mon.fast_poll_s(True) == 8.0
    at(19, 0); mon._battery_st = await f.status(); assert mon.fast_poll_s(False) is None
    f.pwr = 0; at(19, 45); mon._battery_st = await f.status(); assert mon.fast_poll_s(False) is None
    print("H ok: fast poll only on battery, from 15 min before a meal to 5 after")

    # I. the clock hold (BUGS.md #11): 15:47:47 on 11-09, 80 min fast, 15:55 never ran -> sync
    mon, f, led, notes = make()
    at(15, 47, 47); f.state, f.fast, f.pwr = 0, 80.3 * 60, 1
    await mon._keep_clock(await f.status())
    assert len(f.syncs) == 1 and not any("fixing it after" in n[1] for n in notes), (f.syncs, notes)
    # ...but if that meal did run, a sync back would repeat it -> hold, as before
    mon, f, led, notes = make()
    at(15, 47, 47); f.state, f.fast = 0, 80.3 * 60; led.add(15, 55)
    await mon._keep_clock(await f.status())
    assert f.syncs == [] and any("fixing it after the 15:55" in n[1] for n in notes), (f.syncs, notes)
    print("I ok: a fast clock is synced when the meal it 'protects' has no record; held when it ran")

    # K. the clock on battery: checked the moment it switches (awake ~30-40 s), synced if wrong
    mon, f, led, notes = make()
    at(14, 0); f.state, f.pwr, f.fast = 0, 1, -200.0             # 3.3 min slow, far from meals
    st = await f.status(); mon._check_feeder_power(st); await mon._battery_rescue(st)
    assert len(f.syncs) == 1 and f.fast == 0, f.syncs
    await mon._battery_rescue(await f.status()); assert len(f.syncs) == 1, "once per wake"
    # ...and at every wake after: 80 min fast at the 19:47 wake -> synced first, then the meal sent
    mon, f, led, notes = make()
    await unplug(mon, f, 19, 31)
    at(19, 47, 40); f.state, f.fast = 0, 80.3 * 60
    await mon.battery_tick()
    assert len(f.syncs) == 1 and f.feeds == [(5, 35)], (f.syncs, f.feeds)
    print("K ok: clock checked at the switch and at each wake on battery; synced before the rescue")

    # L. the missed-meal alert counts meals seen running, not just ledger records (late upload)
    mon, f, led, notes = make()
    await unplug(mon, f, 19, 31)
    at(19, 47); f.state = 1; await mon.battery_tick()           # seen preparing 19:55, no record yet
    at(20, 2); f.state = 5; await mon._check_missed_meal(await f.status())
    assert not any("hasn't run" in n[1] for n in notes), notes
    mon, f, led, notes = make()
    mon._served_at = real.mktime((2026, 11, 9, 19, 53, 20, 0, 0, -1))   # seen serving on mains
    at(20, 2); f.state, f.pwr = 0, 0; await mon._check_missed_meal(await f.status())
    assert not any("hasn't run" in n[1] for n in notes), notes
    print("L ok: a meal seen preparing or serving isn't reported missed while its record is late")

    # J. the missed-meal alert says what to do when the feeder is on battery
    mon, f, led, notes = make()
    at(20, 2); f.state = 5
    await mon._check_missed_meal(await f.status())
    assert notes[-1][1] == "19:55 meal hasn't run — feeder on battery" and "feed button ONCE" in notes[-1][2], notes
    print("J ok:", notes[-1][1])

asyncio.run(main())
