"""BUGS.md #13 side effect: the feeder's own try at a meal the rescue already sent is refused
"device busy (FoodPreparing)" and logged as a failed auto feed. That is a note, not a
"Feed failed" alert - but a busy refusal with no other meal in sight still alerts.
Built from the 2026-11-11 13:55 records. Simulated ledger and clock."""
import asyncio, json, sys, tempfile, time as real, types
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor as M
from riko import RikoStatus

T = lambda h, m, s=0: int(real.mktime((2026, 11, 11, h, m, s, 0, 0, -1)))
clock = {"now": T(13, 53)}
M.time = types.SimpleNamespace(
    time=lambda: clock["now"], localtime=lambda t=None: real.localtime(clock["now"] if t is None else t),
    gmtime=real.gmtime, mktime=real.mktime, monotonic=lambda: clock["now"],
    strftime=lambda fmt, t=None: real.strftime(fmt, real.localtime(clock["now"]) if t is None else t))
M._near_a_feed_slot = lambda st, *a: True
PLAN = {"bEn": [1], "bDayEn": [1], "time": [50100], "rept": [127], "food": [5], "water": [35], "aud": [2], "type": [1]}

def st(state=0):
    return RikoStatus({"deviceState": {"value": {"state": state, "param": 0}},
                       "fdPlanStr": {"value": json.dumps(PLAN)},
                       "bowlStatus": {"value": {"bBowlIn": 1, "curWeight": 68, "bowlGram": 68}}})

def busy(t, param=1):
    return {"feed_time": t, "status": 2, "way": "auto", "feed_weight": 0, "water_weight": 0,
            "plan_feed_weight": 5, "plan_water_weight": 35, "left_weight": 0,
            "fail_reason": json.dumps({"reason": 4, "resParam": param})}

RESCUE = {"feed_time": T(13, 52, 53), "status": 1, "way": "manual", "feed_weight": 5, "water_weight": 35,
          "plan_feed_weight": 5, "plan_water_weight": 35, "left_weight": 0, "fail_reason": ""}

async def run(feeds, state=0):
    notes = []
    n = types.SimpleNamespace(action_needed=lambda t, b: notes.append(("ACT", t)) or True,
                              fyi=lambda t, b: notes.append(("FYI", t)) or True)
    class L:
        async def ledger(self, *a, **k): return {"feed_list": feeds, "eat_list": []}
    class F:
        async def live_scale(self): return 68.0, 68
    d = Path(tempfile.mkdtemp())
    mon = M.Monitor(F(), M.Store(d / "m.db"), n, M.Policy(killswitch=d / "KILL", water_topup=False),
                    0, False, neakasa=L(), device_name="dev")
    mon.store.put_setting("ledger_last_feed_time", str(T(13, 40)))
    await mon.check_ledger_failures(st(state))
    return notes

async def main():
    # 11-11 as it happened: the rescue's feed served 13:52:53, the own try refused at 13:48:00
    notes = await run([RESCUE, busy(T(13, 48))])
    assert notes == [("FYI", "13:48 meal: duplicate turned away")], notes
    # seen before the rescue's feed reaches the ledger: the feeder is preparing it right now
    clock["now"] = T(13, 48, 30)
    notes = await run([busy(T(13, 48))], state=1)
    assert notes == [("FYI", "13:48 meal: duplicate turned away")], notes
    print("ok: busy refusal during the rescue's feed -> one note, no Feed failed alert")

    # no other meal anywhere: a real failure (a stuck state) still alerts
    clock["now"] = T(13, 53)
    notes = await run([busy(T(13, 48))], state=0)
    assert notes == [("ACT", "Feed failed at 13:48")], notes
    # another feed, but long before: not this meal's duplicate
    old = dict(RESCUE, feed_time=T(13, 20))
    clock["now"] = T(13, 50)
    notes = await run([old, busy(T(13, 48))])
    assert ("ACT", "Feed failed at 13:48") in notes, notes
    # busy for another reason (sleeping, faulting): not a duplicate
    notes = await run([RESCUE, busy(T(13, 48), param=4)])
    assert ("ACT", "Feed failed at 13:48") in notes, notes
    print("ok: busy with no other meal / a meal 28 min away / busy while faulting -> Feed failed alert")

asyncio.run(main())
