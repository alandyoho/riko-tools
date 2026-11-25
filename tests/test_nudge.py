"""Nudge (monitor._check_nudge): a meal still untouched 10 and 25 min after serving gets
a double ping (two same-value child-lock writes, 2 s apart). Simulated feeder, no network."""
import asyncio, json, sys, tempfile, time, types
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor as M
from riko import RikoStatus

def status(state=0, bowl_in=1):
    return RikoStatus({"deviceState": {"value": {"state": state, "param": 0}},
                       "childLockOnOff": {"value": 0},
                       "bowlStatus": {"value": {"bBowlIn": bowl_in, "curWeight": 95, "bowlGram": 68}}})

def make(food=5, water=35, nudge=True, record=None):
    notes, writes, sleeps = [], [], []
    bowl = {"g": food + water}
    class Feeder:
        async def set_property(self, k, v): writes.append((k, v))
        async def live_scale(self):
            return (0.0, 68) if bowl["g"] is None else (68 + bowl["g"], 68)
    n = types.SimpleNamespace(action_needed=lambda t, b: notes.append(t) or True,
                              fyi=lambda t, b: notes.append(t) or True)
    d = Path(tempfile.mkdtemp())
    mon = M.Monitor(Feeder(), M.Store(d / "m.db"), n, M.Policy(killswitch=d / "KILL", nudge=nudge),
                    0, False, device_name="dev")
    async def fake_sleep(s): sleeps.append(s)
    mon._sleep = fake_sleep
    feed = record or {"feed_time": int(time.time()), "status": 1, "way": "auto", "feed_weight": food,
                      "water_weight": water, "plan_feed_weight": food, "plan_water_weight": water,
                      "left_weight": food + water}
    mon._nudge_arm(feed)
    def at(minutes):     # pretend the meal was served this many minutes ago
        p = json.loads(mon.store.get_setting("nudge_pending") or "null")
        if p:
            p["feed_time"] = int(time.time() - minutes * 60)
            mon.store.put_setting("nudge_pending", json.dumps(p))
    def actions():
        return [(k, r) for k, r in mon.store.db.execute("SELECT kind, result FROM remediations")]
    return mon, writes, sleeps, bowl, at, actions, notes

async def main():
    st = status()
    # 1. untouched: ping x2 at 10 min, again at 25, final look at 35 -> "still untouched"
    mon, writes, sleeps, bowl, at, actions, notes = make()
    at(5); await mon._check_nudge(st)
    assert writes == [], "too early"
    at(10.5); await mon._check_nudge(st)
    assert writes == [("childLockOnOff", 0)] * 2 and sleeps == [2.0], (writes, sleeps)
    await mon._check_nudge(st)
    assert len(writes) == 2, "once per mark"
    at(25.5); await mon._check_nudge(st)
    assert len(writes) == 4
    at(35.5); await mon._check_nudge(st)
    assert len(writes) == 4 and mon.store.get_setting("nudge_pending") == ""
    a = actions()
    assert [k for k, _ in a] == ["nudge", "nudge", "nudge result"] and "still untouched" in a[2][1], a
    assert notes == [], "no phone alerts"
    print("1 ok:", a[2][1])

    # 2. he comes after the first nudge -> recorded, no second ping
    mon, writes, sleeps, bowl, at, actions, _ = make()
    at(10.5); await mon._check_nudge(st)
    bowl["g"] = 22
    at(25.5); await mon._check_nudge(st)
    assert len(writes) == 2 and "he came after 1 nudge" in actions()[-1][1], actions()
    print("2 ok:", actions()[-1][1])

    # 3. eating before 10 min -> no ping, nothing recorded
    mon, writes, sleeps, bowl, at, actions, _ = make()
    bowl["g"] = 30
    at(10.5); await mon._check_nudge(st)
    assert writes == [] and actions() == [] and mon.store.get_setting("nudge_pending") == ""
    # a 2 g dip is not eating (scale noise)
    mon, writes, sleeps, bowl, at, actions, _ = make()
    bowl["g"] = 38
    at(10.5); await mon._check_nudge(st)
    assert len(writes) == 2
    print("3 ok: eating early = no ping; 2 g dip still pings")

    # 4. bowl off / feeder busy / on battery -> stop watching, no ping
    for s, b in ((status(bowl_in=0), 40), (status(state=1), 40), (st, None)):
        mon, writes, sleeps, bowl, at, actions, _ = make()
        bowl["g"] = b
        at(10.5); await mon._check_nudge(s)
        assert writes == [] and mon.store.get_setting("nudge_pending") == "", (s.state, b)
    mon, writes, sleeps, bowl, at, actions, _ = make()
    M._on_battery, real = (lambda s: True), M._on_battery
    at(10.5); await mon._check_nudge(st)
    M._on_battery = real
    assert writes == []
    print("4 ok: bowl off, busy, unweighable, battery -> no ping")

    # 5. nudge off -> watched and logged, never pings
    mon, writes, sleeps, bowl, at, actions, _ = make(nudge=False)
    at(10.5); await mon._check_nudge(st); at(25.5); await mon._check_nudge(st)
    at(35.5); await mon._check_nudge(st)
    assert writes == [] and actions() == []
    print("5 ok: RIKO_NUDGE=off never pings")

    # 6. not armed: water-only, failed, misweigh (11-24 07:55), old record
    now = int(time.time())
    base = {"feed_time": now, "status": 1, "feed_weight": 5, "water_weight": 35,
            "plan_feed_weight": 5, "plan_water_weight": 35, "left_weight": 40}
    for rec in (dict(base, feed_weight=0, plan_feed_weight=0), dict(base, status=0),
                dict(base, feed_weight=192, water_weight=7, left_weight=12),
                dict(base, left_weight=0), dict(base, feed_time=now - 3600)):
        mon, writes, *_ = make(record=rec)
        assert not mon.store.get_setting("nudge_pending"), rec
    print("6 ok: water-only, failed, misweigh, no reading, stale -> not watched")

    # 7. monitor down through the 10 min mark -> one nudge at 25, not two at once
    mon, writes, sleeps, bowl, at, actions, _ = make()
    at(26); await mon._check_nudge(st)
    assert len(writes) == 2
    await mon._check_nudge(st)
    assert len(writes) == 2
    print("7 ok: catches up with one nudge")

asyncio.run(main())
print("nudge: all ok")
