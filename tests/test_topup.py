"""Meal top-up: topup.py arithmetic and the monitor's decide / follow-up / restore flow.

Simulated feeder, fake ledger, fake clock. No network, nothing is written to a device.
"""
import asyncio, json, sys, tempfile, time as real, types
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor as M
import topup as TU
from riko import RikoStatus

# ---- the arithmetic -----------------------------------------------------------
P = TU.plan_topup
assert P(8, 56, 0).action == "full" and P(8, 56, 9).action == "full"
assert (P(8, 56, 40).action, P(8, 56, 40).food_g, P(8, 56, 40).water_g) == ("topup", 3, 21)
assert (P(8, 56, 28).food_g, P(8, 56, 28).water_g) == (4, 28)            # the first live top-up, 2026-10-09
assert P(8, 56, 66).action == "skip" and P(8, 56, 91).action == "skip"
assert (P(8, 56, 16).food_g, P(8, 56, 16).water_g) == (6, 42)
assert (P(3, 21, 12).action, P(3, 21, 12).food_g) == ("topup", 2) and P(3, 21, 22).action == "skip"
assert P(0, 24, 30).action == "full"
for f, w in ((8, 56), (3, 21), (8, 40)):
    for left in range(0, 120):
        p = P(f, w, left)
        if p.action == "topup":
            assert 1 <= p.food_g < f and abs(p.water_g / p.food_g - w / f) < 0.01
            assert abs((left + p.food_g + p.water_g) - (f + w)) <= (1 + w / f) / 2 + 0.5, (f, w, left, p)
b = TU.bowl_now({"feed_list": [{"feed_time": 100, "left_weight": 64, "status": 1, "feed_weight": 8, "water_weight": 56},
                               {"feed_time": 500, "left_weight": 0, "status": 1, "feed_weight": 0, "water_weight": 24}],
                 "eat_list": [{"start_time": 200, "end_time": 300, "eat_weight": 40, "left_weight": 24}]})
assert (b.grams, b.as_of, b.last_feed_at) == (48, 500, 500), b      # 24 left + a water-only feed with no bowl reading
assert TU.bowl_now({"feed_list": [], "eat_list": []}) is None
assert TU.handled_since_meal(b, 500 + 3600) and not TU.handled_since_meal(b, 500 + 30) and not TU.handled_since_meal(b, None)
# ---- the schedule model ---------------------------------------------------------
from riko import Schedule
RAW = {"bEn": [1, 1, 0], "bDayEn": [1, 0, 1], "time": [14100, 28500, 42900], "food": [3, 8, 8], "water": [21, 56, 56]}
sc = Schedule.from_status(RikoStatus({"fdPlanStr": {"value": json.dumps(RAW)}}))
assert [i for i, _ in sc.active_today()] == [0] and sc.slots[0].hhmm == "03:55"
assert sc.index_of(28500) == 1 and sc.index_of(999) is None and sc.amounts(1) == [8, 56]
edited = sc.with_amounts(1, 4, 28); skipped = sc.with_skipped(0)
assert edited.amounts(1) == [4, 28] and sc.amounts(1) == [8, 56], "edits must not change the original"
assert skipped.raw["bDayEn"][0] == 0 and sc.raw["bDayEn"][0] == 1 and skipped.with_skipped(0, False).raw == sc.raw
no_flags = Schedule({"time": [14100], "food": [3], "water": [21]})
assert [i for i, _ in no_flags.active_today()] == [0] and "bDayEn" not in no_flags.raw
assert no_flags.with_skipped(0).raw["bDayEn"] == [0]
assert Schedule.from_status(RikoStatus({})) is None and Schedule.from_status(RikoStatus({"fdPlanStr": {"value": "not json"}})) is None
assert Schedule.seconds_since(86100, 300) == 600 and Schedule.seconds_since(300, 86100) == -600      # across midnight
print("unit ok: plan_topup, bowl_now, handled_since_meal, Schedule")

# ---- the flow -----------------------------------------------------------------
clock = {"now": 0.0}
def reset(): clock["now"] = real.mktime((2026, 10, 8, 11, 41, 0, 0, 0, -1))      # 14 min before the 11:55 slot
M.time = types.SimpleNamespace(
    time=lambda: clock["now"], localtime=lambda t=None: real.localtime(clock["now"] if t is None else t),
    gmtime=real.gmtime, mktime=real.mktime,
    strftime=lambda fmt, t=None: real.strftime(fmt, real.localtime(clock["now"]) if t is None else t))
PLAN = {"bEn": [1]*6, "bDayEn": [1]*6, "time": [14100, 28500, 42900, 57300, 71700, 86100], "rept": [127]*6,
        "food": [3, 8, 8, 8, 8, 8], "water": [21, 56, 56, 56, 56, 56], "aud": [2]*6, "type": [1]*6}

class Feeder:                       # the fake Riko: holds the schedule and state
    scale_at = None                 # when the feeder's scale last reported (bowl handled)
    def __init__(self): self.plan = json.loads(json.dumps(PLAN)); self.state = 0; self.writes = []
    def status(self):
        raw = {"fdPlanStr": {"value": json.dumps(self.plan)}, "deviceState": {"value": {"state": self.state, "param": 0}}}
        if self.scale_at: raw["bowlStatus"] = {"time": self.scale_at * 1000, "value": {"bBowlIn": 1, "curWeight": 143, "bowlGram": 68}}
        return RikoStatus(raw)
    async def set_schedule_raw(self, plan): self.plan = json.loads(json.dumps(plan)); self.writes.append(self.plan)

class Ledger:                       # the bowl holds `bowl` grams; it was last empty `emptied_h` hours ago
    def __init__(self): self.bowl = 0; self.calls = 0; self.emptied_h = 4
    async def ledger(self, device, days=1, owner_user_id=None):
        self.calls += 1
        t = int(clock["now"]); e = int(self.emptied_h * 3600)
        eats = [{"start_time": t - e - 600, "end_time": t - e, "eat_weight": 64, "left_weight": 0}]
        if self.bowl > 0:
            eats.append({"start_time": t - 7200, "end_time": t - 6600, "eat_weight": 0, "left_weight": self.bowl})
        else:
            eats = [{"start_time": t - 7200, "end_time": t - 6600, "eat_weight": 64, "left_weight": 0}]
        return {"feed_list": [{"feed_time": t - e - 600, "left_weight": 64, "status": 1, "feed_weight": 8, "water_weight": 56}],
                "eat_list": eats}

def make(mode="on", store=None, d=None):
    d = d or Path(tempfile.mkdtemp()); f, l, notes = Feeder(), Ledger(), []
    n = types.SimpleNamespace(action_needed=lambda t, b: notes.append(("ACT", t)) or True,
                              fyi=lambda t, b: notes.append(("FYI", t)) or True)
    mon = M.Monitor(f, store or M.Store(d / "m.db"), n, M.Policy(killswitch=d / "KILL", topup_mode=mode), 0, False,
                    neakasa=l, device_name="dev")
    return mon, f, l, notes, d
async def step(mon, f, mins=0.5, state=None):
    clock["now"] += mins * 60
    if state is not None: f.state = state
    await mon._check_topup(f.status())
rows = lambda mon: mon.store.db.execute("select slot, kind, result from remediations").fetchall()

async def main():
    # A. observe: decides, writes nothing, decides once per slot
    reset(); mon, f, l, notes, _ = make("observe"); l.bowl = 66
    await step(mon, f, 0); assert not f.writes and not notes and rows(mon)[0][1] == "topup_observe", rows(mon)
    await step(mon, f); assert l.calls == 1
    print("A ok: observe ->", rows(mon)[0][2])

    # B. skip: marks the slot skipped for today, clears itself after the slot, no extra writes
    reset(); mon, f, l, notes, _ = make(); l.bowl = 66
    await step(mon, f, 0)
    assert f.plan["bDayEn"][2] == 0 and f.plan["food"] == PLAN["food"] and notes[-1][0] == "FYI", f.plan
    assert mon._topup_pending()["action"] == "skip"
    for _ in range(8): await step(mon, f)
    assert f.plan["bDayEn"][2] == 0 and len(f.writes) == 1
    for _ in range(26): await step(mon, f)
    assert mon._topup_pending() is None and len(f.writes) == 1 and len(notes) == 1
    print("B ok: skip ->", notes[0][1])

    # B2. skipped, then the bowl turns out empty after the slot -> alert
    reset(); mon, f, l, notes, _ = make(); l.bowl = 66
    await step(mon, f, 0); clock["now"] += 12 * 60; l.bowl = 0; await step(mon, f, 6)
    assert mon._topup_pending() is None and notes[-1][0] == "ACT" and "empty" in notes[-1][1], notes
    print("B2 ok:", notes[-1][1])

    # C. top-up: rewrites the slot, hands off during the meal, restores a minute after it is served
    reset(); mon, f, l, notes, _ = make(); l.bowl = 40
    await step(mon, f, 0)
    assert (f.plan["food"][2], f.plan["water"][2]) == (3, 21) and f.plan["food"][1] == 8, f.plan
    assert M._same_setting(mon.store.get_setting("schedule"),
                           M._fmt([(s.hhmm, s.food_g, s.water_g, s.enabled) for s in f.status().schedule]))
    for _ in range(9): await step(mon, f)
    await step(mon, f, state=1); assert (f.plan["food"][2], len(f.writes)) == (3, 1)
    for _ in range(17): await step(mon, f)                     # 11:54:30, still preparing
    await step(mon, f, state=2); await step(mon, f, state=0)   # serving 11:55:00, idle 11:55:30
    assert len(f.writes) == 1, "must wait until a minute after the slot"
    await step(mon, f, 1.5)
    assert f.plan == PLAN and mon._topup_pending() is None and len(f.writes) == 2
    print("C ok: top-up ->", notes[0][1], "| restored, schedule identical to the original")

    # D. bowl emptied before the feeder starts preparing -> full meal put back
    reset(); mon, f, l, notes, _ = make(); l.bowl = 66
    await step(mon, f, 0); assert f.plan["bDayEn"][2] == 0
    l.bowl = 0; await step(mon, f, 1); assert f.plan == PLAN and mon._topup_pending() is None
    reset(); mon, f, l, notes, _ = make(); l.bowl = 40
    await step(mon, f, 0); l.bowl = 0; await step(mon, f, 1); assert f.plan == PLAN
    print("D ok: bowl emptied before prep -> skip undone / full amounts restored")

    # E. edited in the app while a top-up is pending -> left alone
    reset(); mon, f, l, notes, _ = make(); l.bowl = 40
    await step(mon, f, 0); f.plan["food"][2], f.plan["water"][2] = 5, 25
    clock["now"] += 5 * 60; await step(mon, f, 40)
    assert (f.plan["food"][2], f.plan["water"][2]) == (5, 25) and mon._topup_pending() is None and len(f.writes) == 1
    print("E ok: an edit made in the app wins")

    # F. restart mid-way: a new monitor on the same database still restores, at the backstop
    reset(); mon, f, l, notes, d = make(); l.bowl = 40
    await step(mon, f, 0)
    mon2, f2, l2, notes2, _ = make(store=mon.store, d=d); f2.plan = f.plan; l2.bowl = 40
    clock["now"] += 6 * 60; await step(mon2, f2, 20); assert f2.plan["food"][2] == 3, "not before the backstop"
    await step(mon2, f2, 20); assert f2.plan == PLAN and mon2._topup_pending() is None
    print("F ok: restart mid-way -> restored at the 30-minute backstop")

    # G. kill switch -> observe only; H. nearly empty -> full meal; I. stale-food alert, once
    reset(); mon, f, l, notes, d = make(); (d / "KILL").touch(); l.bowl = 66
    await step(mon, f, 0); assert not f.writes and rows(mon)[0][1] == "topup_observe"
    reset(); mon, f, l, notes, _ = make(); l.bowl = 8
    await step(mon, f, 0); assert not f.writes and not notes
    reset(); mon, f, l, notes, _ = make("observe"); l.bowl = 30; l.emptied_h = 13
    await step(mon, f, 0); assert notes == [("ACT", "Food has been sitting in the bowl")], notes
    clock["now"] += 4 * 3600; l.emptied_h = 17; await step(mon, f, 0)      # same stretch, next meal
    assert len(notes) == 1, notes
    print("G/H/I ok: kill switch observes; 8 g left serves the full meal; stale-food alert fires once")

    # J. outside the decision window, or feeder asleep -> untouched
    reset(); mon, f, l, notes, _ = make(); l.bowl = 66; clock["now"] -= 30 * 60
    await step(mon, f, 0); assert l.calls == 0
    reset(); mon, f, l, notes, _ = make(); l.bowl = 66; await step(mon, f, 0, state=5); assert l.calls == 0 and not f.writes
    print("J ok: outside the window or feeder asleep -> untouched")

    # K. bowl taken off / put back since the last meal -> ledger not trusted, meal left alone
    reset(); mon, f, l, notes, _ = make(); l.bowl = 75; f.scale_at = clock["now"] - 3 * 3600
    await step(mon, f, 0); assert not f.writes and not notes and "handled" in rows(mon)[0][2], rows(mon)
    reset(); mon, f, l, notes, _ = make(); l.bowl = 66; f.scale_at = clock["now"] - 9 * 3600   # before the last feed
    await step(mon, f, 0); assert f.plan["bDayEn"][2] == 0
    # L. bowl swapped after a skip was applied, before prep -> skip undone
    reset(); mon, f, l, notes, _ = make(); l.bowl = 66
    await step(mon, f, 0); assert f.plan["bDayEn"][2] == 0
    f.scale_at = clock["now"] + 20; await step(mon, f, 1)
    assert f.plan == PLAN and mon._topup_pending() is None
    print("K/L ok: a handled bowl leaves the meal alone; a swap after a skip puts the meal back")

asyncio.run(main())
