"""daily_report: meal-by-meal eating from a simulated ledger, and the rendered text + JSON."""
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import daily_report as D

T = lambda h, m: time.mktime((2026, 11, 18, h, m, 0, 0, 0, -1))
t0, t1 = D.day_bounds("2026-11-18")
feed = lambda h, m, food, water, ok=True, way="auto": {
    "feed_time": T(h, m), "way": way, "status": 1 if ok else 2, "feed_weight": food if ok else 0,
    "water_weight": water if ok else 0, "plan_feed_weight": food,
    "fail_reason": "" if ok else json.dumps({"reason": 2, "resParam": 13})}
eat = lambda h, m, g, mins=5: {"start_time": T(h, m), "end_time": T(h, m) + mins * 60, "eat_weight": g}
led = {"feed_list": [feed(9, 55, 5, 35), feed(11, 55, 5, 35), feed(13, 55, 4, 28, ok=False), feed(15, 55, 5, 35)],
       "eat_list": [eat(10, 5, 30), eat(10, 40, 12), eat(16, 30, 41)]}

ms = D.meals(led, t0, t1)
assert [m["eaten_g"] for m in ms] == [42, 0, 0, 41], ms
assert ms[0]["finished"] and ms[0]["first_bite_min"] == 10 and ms[1]["first_bite_min"] is None
assert not ms[2]["ok"] and "jammed" in ms[2]["failed_because"], ms[2]
print("ok: eating is matched to the meal before it; untouched and failed meals are marked")

rep = {"date": "2026-11-18", "meals": ms,
       "summary": {"meals_served": 3, "meals_failed": 1, "manual_feeds": 0, "scheduled_with_no_record": ["03:55"],
                   "food_served_g": 15, "food_planned_g": 46.0, "served_total_g": 120, "eaten_total_g": 83,
                   "eaten_share": 0.69, "meals_finished": 2, "meals_untouched": 1},
       "events": {"battery_min": 12, "stops": {"grinder jammed with food": ["13:57"]}, "jams": [],
                  "monitor_actions": [], "setting_changes": []},
       "feeder_now": {"food_tank": "sufficient", "water_tank": "full", "power": "mains", "battery_percent": 90,
                      "clock_offset_s": -2, "desiccant_days_left": 12, "firmware": "x"}}
text = D.render(rep)
assert "schedule: 46 g" in text and "no record for 03:55" in text and "on battery 12 min" in text
assert "11:55 scheduled 5 g / 35 g  ate 0 g, not touched" in text, text
assert json.loads(text.split("DATA (JSON, same numbers):\n")[1]) == rep
print("ok: report text has the summary, each meal, the events, and a JSON block that round-trips")
