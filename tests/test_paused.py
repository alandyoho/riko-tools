"""A meal paused on purpose (suspension code 5): one quiet note, one reminder after 15 min,
no repeated urgent alerts; a paused-then-resumed meal logged "stopped manually" but served
is a note, not a "Feed failed" alert. Simulated feeder and clock."""
import asyncio, json, sys, tempfile, time as real, types
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor as M
from riko import RikoStatus

clock = {"now": real.mktime((2026, 11, 12, 19, 50, 0, 0, 0, -1))}
M.time = types.SimpleNamespace(
    time=lambda: clock["now"], localtime=lambda t=None: real.localtime(clock["now"] if t is None else t),
    gmtime=real.gmtime, mktime=real.mktime, monotonic=lambda: clock["now"],
    strftime=lambda fmt, t=None: real.strftime(fmt, real.localtime(clock["now"]) if t is None else t))
PLAN = {"bEn": [1], "bDayEn": [1], "time": [71700], "rept": [127], "food": [5], "water": [35], "aud": [2], "type": [1]}

def st(state, param=0):
    return RikoStatus({"deviceState": {"value": {"state": state, "param": param}},
                       "fdPlanStr": {"value": json.dumps(PLAN)},
                       "bowlStatus": {"value": {"bBowlIn": 1, "curWeight": 68, "bowlGram": 68}}})

class F:
    async def live_scale(self): return 68.0, 68
    async def status(self): return st(3, 5)

async def main():
    d = Path(tempfile.mkdtemp()); notes = []
    n = types.SimpleNamespace(action_needed=lambda t, b: notes.append(("ACT", t)) or True,
                              fyi=lambda t, b: notes.append(("FYI", t)) or True)
    mon = M.Monitor(F(), M.Store(d / "m.db"), n, M.Policy(killswitch=d / "KILL"), 0, False)
    for _ in range(40):                                  # 20 minutes paused, a pass every 30 s
        await mon._check_state(st(3, 5)); clock["now"] += 30
    assert notes == [("FYI", "Meal paused"), ("ACT", "Meal still paused")], notes
    await mon._check_state(st(1)); await mon._check_state(st(3, 5))
    assert notes[-1] == ("FYI", "Meal paused") and len(notes) == 3, "a new pause is a new note"
    print("ok: paused -> one note, one reminder at 15 min, no urgent alert every pass")

    # the ledger: stopped manually but served -> a note; stopped manually with nothing served -> alert
    M._near_a_feed_slot = lambda st, *a: True
    t = int(real.mktime((2026, 11, 12, 19, 55, 0, 0, 0, -1)))
    for food, expect in ((5, ("FYI", "19:55 meal logged as stopped manually")), (0, ("ACT", "Feed failed at 19:55"))):
        notes.clear()
        rec = {"feed_time": t, "status": 2, "way": "auto", "feed_weight": food, "water_weight": 35 if food else 0,
               "plan_feed_weight": 5, "plan_water_weight": 35, "left_weight": 0,
               "fail_reason": json.dumps({"reason": 1, "resParam": 0})}
        class L:
            async def ledger(self, *a, **k): return {"feed_list": [rec], "eat_list": []}
        mon2 = M.Monitor(F(), M.Store(Path(tempfile.mkdtemp()) / "m.db"), n,
                         M.Policy(killswitch=d / "KILL", water_topup=False), 0, False, neakasa=L(), device_name="dev")
        mon2.store.put_setting("ledger_last_feed_time", str(t - 60))
        await mon2.check_ledger_failures(st(0))
        assert expect in notes, (food, notes)
        if expect[0] == "FYI":
            assert not any(x[1].startswith("Feed failed") for x in notes), notes
    print("ok: 'stopped manually' but served -> note; nothing served -> Feed failed alert")

asyncio.run(main())
