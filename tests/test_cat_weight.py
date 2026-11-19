"""cat_weight: picking the cat profile, storing weigh-ins once, the trend, and the report line."""
import sys, tempfile, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cat_weight as W

family = [{"id": "me1", "nick_name": "person", "member_property": ""},
          {"id": "cat1", "nick_name": "MrCoconutMeow", "member_property": "Cat", "breed": "American Shorthair"}]
assert W.cat_profiles(family) == {"cat1": "MrCoconutMeow"}
print("ok: only the profile the scale marks as a cat is read")

DAY = 86400
now = time.mktime((2026, 11, 20, 8, 20, 0, 0, 0, -1))
rec = lambda did, days_ago, kg, deleted=0: {"data_id": did, "measure_ts": int((now - days_ago * DAY) * 1000),
                                            "weight": kg, "family_member_id": "cat1", "is_delete": deleted}
db = W.open_db(Path(tempfile.mkdtemp()) / "w.db")
rows = W.rows_from_records([rec(1, 29, 5.03), rec(2, 15, 5.10), rec(3, 1, 5.17), rec(4, 3, 9.9, deleted=1)], "owner", "MrCoconutMeow")
assert W.store(db, rows) == 3
assert W.store(db, rows) == 0
same_from_other_account = W.rows_from_records([dict(rec(99, 1, 5.17), measure_ts=rec(3, 1, 5.17)["measure_ts"] + 20000)], "mine", "x")
assert W.store(db, same_from_other_account) == 0
print("ok: each weigh-in is stored once, deleted ones skipped, the same reading on both accounts counted once")

t = W.trend(db, now)
assert t["latest_lb"] == 11.4 and t["days_since"] == 1, t
assert t["change_2w_lb"] == 0.2 and t["change_4w_lb"] == 0.3, t
assert t["weigh_ins_30d"] == 3 and t["weigh_ins_total"] == 3
db.execute("DELETE FROM weigh_ins WHERE data_id = '1'")
assert W.trend(db, now)["change_4w_lb"] is None
print("ok: latest weight and change over 2 and 4 weeks; no 4-week change without a reading near then")

import daily_report as D
rep = {"date": "2026-11-19", "meals": [], "weight": t,
       "summary": {"meals_served": 0, "meals_failed": 0, "manual_feeds": 0, "scheduled_with_no_record": [],
                   "food_served_g": 0, "food_planned_g": 46.0, "served_total_g": 0, "eaten_total_g": 0,
                   "eaten_share": None, "meals_finished": 0, "meals_untouched": 0},
       "events": {"battery_min": 0, "stops": {}, "jams": [], "monitor_actions": [], "setting_changes": []},
       "feeder_now": {"food_tank": "sufficient", "water_tank": "full", "power": "mains", "battery_percent": 90,
                      "clock_offset_s": 0, "desiccant_days_left": 12, "firmware": "x"}}
text = D.render(rep)
assert "Weight: 11.4 lb on 2026-11-19; +0.2 lb over 2 weeks, +0.3 lb over 4 weeks; 3 weigh-ins in 30 days" in text, text
assert "Weight:" not in D.render({**rep, "weight": None})
print("ok: the report shows the weight line, and leaves it out when there is no history")
