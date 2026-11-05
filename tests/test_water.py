"""Water top-up (monitor._check_water_short): a meal served short of water gets the rest
as a water-only feed. Simulated feeder and ledger, no network."""
import asyncio, json, sys, tempfile, time, types
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor as M
from riko import RikoStatus

M._near_a_feed_slot = lambda st, *a: True
PLAN = {"bEn": [1, 1], "bDayEn": [1, 1], "time": [35700, 42900], "rept": [127] * 2,
        "food": [5, 5], "water": [35, 35], "aud": [2] * 2, "type": [1] * 2}

def status(state=0, bowl_in=1, plan=PLAN):
    return RikoStatus({"fdPlanStr": {"value": json.dumps(plan)},
                       "deviceState": {"value": {"state": state, "param": 0}},
                       "bowlStatus": {"value": {"bBowlIn": bowl_in, "curWeight": 95, "bowlGram": 68}}})

def make(food, water, plan=(5, 35), ago=120, kill=False, policy_off=False):
    now = int(time.time()); notes, feeds = [], []
    led = {"feed_list": [{"feed_time": now - ago, "status": 1, "way": "auto", "feed_weight": food,
                          "water_weight": water, "plan_feed_weight": plan[0], "plan_water_weight": plan[1],
                          "left_weight": food + water}], "eat_list": []}
    class Ledger:
        async def ledger(self, *a, **k): return led
    class Feeder:
        async def feed(self, f, w=None, **k): feeds.append((f, w))
    n = types.SimpleNamespace(action_needed=lambda t, b: notes.append(("ACT", t, b)) or True,
                              fyi=lambda t, b: notes.append(("FYI", t, b)) or True)
    d = Path(tempfile.mkdtemp())
    if kill: (d / "KILL").touch()
    mon = M.Monitor(Feeder(), M.Store(d / "m.db"), n, M.Policy(killswitch=d / "KILL", water_topup=not policy_off),
                    0, False, neakasa=Ledger(), device_name="dev")
    mon.store.put_setting("ledger_last_feed_time", str(now - 3600))
    return mon, notes, feeds

# A plan whose next meal is far from now, and one whose next meal starts in 10 minutes.
far = lambda: dict(PLAN, time=[(M._tod() + 3 * 3600) % 86400, (M._tod() + 5 * 3600) % 86400])
soon = lambda: dict(PLAN, time=[(M._tod() + 10 * 60) % 86400, (M._tod() + 5 * 3600) % 86400])

async def main():
    # 1. the 2026-11-05 09:55 meal: 5 + 21 of 5 + 35 -> 14 g of water, once
    mon, notes, feeds = make(5, 21)
    await mon.check_ledger_failures(status(plan=far()))
    assert feeds == [(0, 14)], feeds
    assert notes == [("FYI", "Added 14 g of water to the " + notes[0][1].split("the ")[1], notes[0][2])], notes
    await mon.check_ledger_failures(status(plan=far()))
    assert feeds == [(0, 14)] and len(notes) == 1, "once per meal"
    print("1 ok:", notes[0][1])

    # 2. a normal meal (1 g short) and a partial meal cut short by a jam (water matches the food) -> nothing
    for food, water in ((5, 34), (2, 14), (3, 21)):
        mon, notes, feeds = make(food, water)
        await mon.check_ledger_failures(status(plan=far()))
        assert feeds == [] and notes == [], (food, water, feeds, notes)
    # a water-only feed (ours, or a manual one) -> nothing
    mon, notes, feeds = make(0, 14, plan=(0, 14))
    await mon.check_ledger_failures(status(plan=far()))
    assert feeds == [] and notes == []
    print("2 ok: normal, partial and water-only meals are left alone")

    # 3. held off, with a note saying how much would make it up
    cases = [(dict(ago=45 * 60), {}, "served too long ago"),
             ({}, dict(plan=soon()), "is about to start"),
             (dict(kill=True), {}, "automatic water top-up is off"),
             (dict(policy_off=True), {}, "automatic water top-up is off"),
             ({}, dict(state=1), "busy or the bowl is off"),
             ({}, dict(bowl_in=0), "busy or the bowl is off")]
    for kw, st_kw, why in cases:
        mon, notes, feeds = make(5, 21, **kw)
        await mon.check_ledger_failures(status(**dict(dict(plan=far()), **st_kw)))
        assert feeds == [] and len(notes) == 1 and why in notes[0][2] and "14 g would make it up" in notes[0][2], (kw, st_kw, notes)
    print("3 ok: held off when too late, a meal is due, kill switch, policy off, feeder busy, bowl off")

    # 4. capped at 60 g
    mon, notes, feeds = make(10, 0, plan=(10, 80))
    await mon.check_ledger_failures(status(plan=far()))
    assert feeds == [(0, 60)], feeds
    print("4 ok: capped at 60 g")

    # 5. the ledger is read right after a meal even when it is far from a slot
    M._near_a_feed_slot = lambda st, *a: False
    mon, notes, feeds = make(5, 21)
    await mon.check_ledger_failures(status(plan=far())); assert feeds == []
    mon._served_at = time.time() - 60
    await mon.check_ledger_failures(status(plan=far())); assert feeds == [(0, 14)], feeds
    print("5 ok: a late meal (served 30 min after its slot) is still checked")

asyncio.run(main())
