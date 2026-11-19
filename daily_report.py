#!/usr/bin/env python3
"""daily_report.py - yesterday's feeding report, by email, for a person and for an AI
agent (Claude Duet) that reads the series of them and reports on trends.

    daily_report.py              send the report for yesterday
    daily_report.py --print      print it instead (nothing sent)
    daily_report.py --date 2026-11-18 [--print]

Sources: the feeder's history (meals served, eating sessions), its live status (tanks,
clock), the monitor's database (jams, rescues, top-ups, setting changes), the push
log (power and state changes) and Mr Meow's weigh-ins (cat_weight.py's database). Read-only: it never changes anything on the feeder.

Email: Gmail SMTP with an app password, from the environment (.env):
    RIKO_SMTP_USER       the Gmail address that sends it
    RIKO_SMTP_PASSWORD   its app password (myaccount.google.com/apppasswords)
    RIKO_REPORT_TO       where it goes (default: RIKO_SMTP_USER)
The subject is always "Riko daily report YYYY-MM-DD" and the body ends with a JSON
block of the same numbers, so an agent can find every report and compare them.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import smtplib
import sqlite3
import ssl
import sys
import time
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import load
from riko import ERROR_CODES, FoodLevel, Riko, Schedule, WaterLevel
import cat_weight

HERE = Path(__file__).resolve().parent
PUSHES = HERE / "riko_capture" / "pushes.jsonl"
DB = HERE / "riko_state" / "monitor.db"
log = logging.getLogger("daily_report")


def day_bounds(day: str) -> tuple[float, float]:
    start = time.mktime(time.strptime(day, "%Y-%m-%d"))
    return start, start + 86400


def hm(t: float) -> str:
    return time.strftime("%H:%M", time.localtime(t))


# ------------------------------------------------------------------ meals
def meals(led: dict, t0: float, t1: float) -> list[dict]:
    """Each meal of the day with what he ate of it: the eating sessions after it,
    until the next meal (food and water together, by the bowl scale)."""
    feeds = sorted((f for f in led.get("feed_list", []) if t0 <= f.get("feed_time", 0) < t1),
                   key=lambda f: f["feed_time"])
    eats = sorted(led.get("eat_list", []), key=lambda e: e.get("start_time") or 0)
    every = sorted(led.get("feed_list", []), key=lambda f: f.get("feed_time", 0))
    out = []
    for f in feeds:
        ft = f["feed_time"]
        nxt = next((g["feed_time"] for g in every if g["feed_time"] > ft), ft + 4 * 3600)
        mine = [e for e in eats if ft <= (e.get("start_time") or 0) < nxt]
        served = (f.get("feed_weight") or 0) + (f.get("water_weight") or 0)
        eaten = sum(e.get("eat_weight") or 0 for e in mine)
        m = {"time": hm(ft), "kind": "scheduled" if f.get("way") == "auto" else "manual",
             "ok": f.get("status") == 1, "food_g": f.get("feed_weight") or 0,
             "water_g": f.get("water_weight") or 0, "planned_food_g": f.get("plan_feed_weight") or 0,
             "eaten_g": eaten, "finished": bool(served) and eaten >= 0.8 * served,
             "first_bite_min": round(((mine[0].get("start_time") or ft) - ft) / 60) if mine else None}
        if not m["ok"]:
            try:
                from monitor import decode_fail_reason
                m["failed_because"] = decode_fail_reason(f.get("fail_reason", ""))
            except Exception:
                m["failed_because"] = "unknown"
        out.append(m)
    return out


def missed(st, served: list[dict], day: str) -> list[str]:
    """Scheduled times with no record that day (skip-for-today flags only cover today,
    so this is an estimate for a past day)."""
    sched = Schedule.from_status(st)
    if not sched:
        return []
    have = {m["time"] for m in served if m["kind"] == "scheduled"}
    t0, _ = day_bounds(day)
    def near(slot):   # a scheduled record within 15 min of the slot counts (clock drift)
        s = t0 + slot.seconds_from_midnight
        return any(abs(time.mktime(time.strptime(f"{day} {h}", "%Y-%m-%d %H:%M")) - s) <= 900 for h in have)
    return [sl.hhmm for sl in sched.slots if sl.enabled and not near(sl)]


# ------------------------------------------------------------------ events
def power_and_stops(t0: float, t1: float) -> dict:
    """From the push log: minutes on battery, and stops (suspended) by reason."""
    battery_s, on_batt_since, stops = 0.0, None, {}
    last_pwr = None
    if PUSHES.exists():
        with PUSHES.open() as fh:
            for line in fh:
                if not line.startswith('{"ts": "'):
                    continue
                ts = time.mktime(time.strptime(line[8:27], "%Y-%m-%d %H:%M:%S"))
                if ts >= t1:
                    break
                try:
                    items = json.loads(line)["body"].get("params", {}).get("items", {})
                except (ValueError, KeyError, AttributeError):
                    continue
                pwr = (items.get("pwrStatus") or {}).get("value", {}).get("pwrType")
                if pwr is not None and pwr != last_pwr:
                    if pwr == 1:
                        on_batt_since = max(ts, t0)
                    elif on_batt_since is not None:
                        if ts > t0:
                            battery_s += ts - max(on_batt_since, t0)
                        on_batt_since = None
                    last_pwr = pwr
                ds = (items.get("deviceState") or {}).get("value") or {}
                if ts >= t0 and ds.get("state") == 3:
                    why = ERROR_CODES.get(ds.get("param"), f"code {ds.get('param')}")
                    stops.setdefault(why, set()).add(hm(ts))
    if on_batt_since is not None:
        battery_s += t1 - max(on_batt_since, t0)
    return {"battery_min": round(battery_s / 60), "stops": {k: sorted(v) for k, v in stops.items()}}


def monitor_events(day: str, t0: float, t1: float) -> dict:
    out = {"jams": [], "monitor_actions": [], "setting_changes": []}
    if not DB.exists():
        return out
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        for start, slot, outcome in db.execute(
                "SELECT start, slot, outcome FROM jams WHERE start >= ? AND start < ? ORDER BY start", (t0, t1)):
            out["jams"].append({"time": hm(start), "meal": slot, "outcome": outcome})
        for ts, slot, kind, result in db.execute(
                "SELECT ts, slot, kind, result FROM remediations WHERE ts >= ? AND ts < ? ORDER BY ts", (t0, t1)):
            out["monitor_actions"].append({"time": hm(ts), "meal": slot, "action": kind, "result": result})
        for ts, key in db.execute(
                "SELECT ts, key FROM setting_changes WHERE ts >= ? AND ts < ? ORDER BY ts", (t0, t1)):
            out["setting_changes"].append({"time": hm(ts), "setting": key})
    except sqlite3.Error as exc:
        log.warning("monitor.db: %r", exc)
    finally:
        db.close()
    return out


# ------------------------------------------------------------------ build
async def build(day: str) -> dict:
    t0, t1 = day_bounds(day)
    cfg = load()
    async with Riko.from_config(cfg) as r:
        r.relogin = r.login                       # share the monitor's session
        st = await r.status()
        from neakasa import Feeder as Ledger
        days_back = max(2, int((time.time() - t0) // 86400) + 2)
        led = await Ledger(client=r.client).ledger(r.device.device_name, days=days_back,
                                                   owner_user_id=cfg.feeder_owner_id or None)
        try:
            clock = round(await asyncio.wait_for(r.device_time(), 10) - time.time())
        except Exception:
            clock = None
    ms = meals(led, t0, t1)
    sched = Schedule.from_status(st)
    planned = sum(sl.food_g for sl in sched.slots if sl.enabled) if sched else None
    served_food = sum(m["food_g"] for m in ms if m["ok"])
    served_total = sum(m["food_g"] + m["water_g"] for m in ms if m["ok"])
    eaten = sum(m["eaten_g"] for m in ms)
    ev = power_and_stops(t0, t1)
    pwr = st._p("pwrStatus", {}) or {}
    return {
        "date": day,
        "summary": {
            "meals_served": sum(m["ok"] for m in ms), "meals_failed": sum(not m["ok"] for m in ms),
            "manual_feeds": sum(m["kind"] == "manual" and m["ok"] for m in ms),
            "scheduled_with_no_record": missed(st, ms, day),
            "food_served_g": served_food, "food_planned_g": planned,
            "served_total_g": served_total, "eaten_total_g": eaten,
            "eaten_share": round(eaten / served_total, 2) if served_total else None,
            "meals_finished": sum(m["finished"] for m in ms if m["ok"]),
            "meals_untouched": sum(m["eaten_g"] == 0 for m in ms if m["ok"]),
        },
        "meals": ms,
        "events": {**ev, **monitor_events(day, t0, t1)},
        "weight": cat_weight.report_section(),
        "feeder_now": {
            "food_tank": st.food_level.name.lower(), "water_tank": st.water_level.name.lower(),
            "power": "battery" if pwr.get("pwrType") == 1 else "mains", "battery_percent": st.battery_pct,
            "clock_offset_s": clock, "desiccant_days_left": st.desiccant_days_left, "firmware": st.firmware,
        },
    }


def render(r: dict) -> str:
    s, e, f = r["summary"], r["events"], r["feeder_now"]
    lines = [f"Riko daily report for {r['date']} (Mr Meow)", ""]
    lines.append(f"Meals: {s['meals_served']} served, {s['meals_failed']} failed"
                 + (f", {s['manual_feeds']} manual" if s["manual_feeds"] else "")
                 + (f"; no record for {', '.join(s['scheduled_with_no_record'])}" if s["scheduled_with_no_record"] else ""))
    lines.append(f"Food: {s['food_served_g']:g} g served (schedule: {s['food_planned_g']:g} g)")
    if s["served_total_g"]:
        lines.append(f"Eaten: {s['eaten_total_g']} g of {s['served_total_g']} g served ({s['eaten_share']:.0%}, food + water); "
                     f"{s['meals_finished']} meals finished, {s['meals_untouched']} untouched")
    lines += ["", "Meal by meal (time, served food/water, eaten, first bite):"]
    for m in r["meals"]:
        tail = (f"FAILED: {m.get('failed_because')}" if not m["ok"] else
                f"ate {m['eaten_g']} g" + (f", first bite after {m['first_bite_min']} min" if m["first_bite_min"] is not None else ", not touched"))
        lines.append(f"  {m['time']} {m['kind']:<9} {m['food_g']} g / {m['water_g']} g  {tail}")
    lines += ["", "Events:"]
    ev = []
    if e["battery_min"]:
        ev.append(f"on battery {e['battery_min']} min")
    for why, times in e["stops"].items():
        ev.append(f"stopped ({why}) at {', '.join(times)}")
    for j in e["jams"]:
        ev.append(f"jam at {j['time']} ({j['meal']} meal): {j['outcome']}")
    for a in e["monitor_actions"]:
        ev.append(f"monitor {a['action']} at {a['time']} ({a['meal']}): {a['result']}")
    for c in e["setting_changes"]:
        ev.append(f"setting changed at {c['time']}: {c['setting']}")
    lines += [f"  - {x}" for x in ev] or ["  none"]
    w = r.get("weight")
    if w:
        ch = [f"{v:+.1f} lb over {k}" for k, v in (("2 weeks", w["change_2w_lb"]), ("4 weeks", w["change_4w_lb"])) if v is not None]
        lines += ["", f"Weight: {w['latest_lb']} lb on {w['latest_date']}"
                      + (f" ({w['days_since']} days ago)" if w["days_since"] > 1 else "")
                      + (f"; {', '.join(ch)}" if ch else "")
                      + f"; {w['weigh_ins_30d']} weigh-ins in 30 days, history since {w['first_date']}"]
    clock = f["clock_offset_s"]
    lines += ["", f"Feeder now: food {f['food_tank']}, water {f['water_tank']}, on {f['power']}"
                  + (f" ({f['battery_percent']}%)" if f["power"] == "battery" else "")
                  + (f", clock {clock:+d} s" if clock is not None else "")
                  + (f", desiccant {f['desiccant_days_left']} days" if f["desiccant_days_left"] is not None else ""),
              "", "Notes: eaten grams are food and water together, by the bowl scale; meals are 1 part food",
              "to 7 parts water. Each meal lands ~3-4 g heavier than recorded (water drip), so a fully",
              "eaten day reads ~105-110%. Times are the feeder's clock.", "",
              "DATA (JSON, same numbers):", json.dumps(r, separators=(",", ":"))]
    return "\n".join(lines)


def send(subject: str, body: str) -> None:
    user, pw = os.environ.get("RIKO_SMTP_USER"), os.environ.get("RIKO_SMTP_PASSWORD")
    if not user or not pw:
        raise SystemExit("RIKO_SMTP_USER / RIKO_SMTP_PASSWORD are not set in .env")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = user, os.environ.get("RIKO_REPORT_TO") or user, subject
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as s:
        s.login(user, pw.replace(" ", ""))
        s.send_message(msg)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", default=time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400)))
    ap.add_argument("--print", action="store_true", help="print instead of sending")
    a = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    rep = asyncio.run(build(a.date))
    subject, body = f"Riko daily report {a.date}", render(rep)
    if a.print:
        print(subject, "\n", body, sep="\n")
    else:
        send(subject, body)
        print(f"sent: {subject}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
