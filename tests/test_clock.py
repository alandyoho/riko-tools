"""Clock upkeep in monitor.py (_keep_clock): read the feeder's own clock, sync when it is
wrong, and hold off only when the jump itself would cost a meal. Simulated feeder and clock."""
import asyncio, json, sys, tempfile, time as real, types
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor as M
from riko import RikoStatus

clock = {"now": 0.0}
def at(h, m, s=0): clock["now"] = real.mktime((2026, 10, 13, h, m, s, 0, 0, -1))
M.time = types.SimpleNamespace(
    time=lambda: clock["now"], localtime=lambda t=None: real.localtime(clock["now"] if t is None else t),
    gmtime=real.gmtime, mktime=real.mktime,
    strftime=lambda fmt, t=None: real.strftime(fmt, real.localtime(clock["now"]) if t is None else t))
PLAN = {"bEn": [1]*6, "bDayEn": [1]*6, "time": [14100, 28500, 42900, 57300, 71700, 86100], "rept": [127]*6,
        "food": [3, 8, 8, 8, 8, 8], "water": [21, 56, 56, 56, 56, 56], "aud": [2]*6, "type": [1]*6}

class Feeder:
    """Its clock runs `slow` seconds behind ours; a sync fixes that unless `sync_works` is off."""
    def __init__(self, slow=0.0): self.state, self.slow, self.syncs, self.reads = 0, slow, [], 0; self.sync_works = True; self.fail = False
    def status(self):
        return RikoStatus({"fdPlanStr": {"value": json.dumps(self.plan if hasattr(self, "plan") else PLAN)},
                           "deviceState": {"value": {"state": self.state, "param": 0}},
                           "defFdCfg": {"value": {"food": 8, "water": 56, "mixMin": 10}}})
    async def device_time(self):
        self.reads += 1
        if self.fail: raise TimeoutError()
        return clock["now"] - self.slow
    async def sync_time(self, source=1):
        self.syncs.append(real.strftime("%H:%M:%S", real.localtime(clock["now"])))
        if self.sync_works: self.slow = 0.0
def make(slow=0.0, d=None):
    d = d or Path(tempfile.mkdtemp()); f, notes = Feeder(slow), []
    n = types.SimpleNamespace(action_needed=lambda t, b: notes.append(("ACT", t)) or True, fyi=lambda t, b: notes.append(("FYI", t)) or True)
    return M.Monitor(f, M.Store(d / "m.db"), n, M.Policy(killswitch=d / "KILL"), 0, False), f, notes, d
async def step(mon, f, secs=30, **changes):
    clock["now"] += secs
    for k, v in changes.items(): setattr(f, k, v)
    await mon._keep_clock(f.status())

async def main():
    # a. in step: reads the feeder's clock every couple of minutes, never syncs
    at(14, 0); mon, f, notes, _ = make()
    for _ in range(20): await step(mon, f)                     # 10 minutes of 30 s polls
    assert not f.syncs and not notes and 4 <= f.reads <= 6, f.reads
    print("a ok: in step ->", f.reads, "clock reads in 10 min, no sync")

    # b. last night: 22.7 min slow from 20:20 -> fixed within two minutes, hours before any meal
    at(20, 19); mon, f, notes, _ = make()
    await step(mon, f, 0)
    f.slow = 22.7 * 60
    for _ in range(5): await step(mon, f)
    assert len(f.syncs) == 1 and f.syncs[0] <= "20:21:30" and notes == [("FYI", "Feeder clock was 22.7 min slow — synced")], (f.syncs, notes)
    for _ in range(10): await step(mon, f)
    assert len(f.syncs) == 1 and len(notes) == 1
    print("b ok: 22.7 min slow -> synced at", f.syncs[0], "|", notes[0][1])

    # c. slow, noticed 15 min before a meal: the jump lands before the meal starts -> sync
    at(3, 40); mon, f, notes, _ = make(slow=22.7 * 60)
    await step(mon, f, 0); assert f.syncs == ["03:40:00"]
    # d. slow, noticed 5 min before the slot: the jump passes the start but lands before the meal is due -> sync
    at(3, 50); mon, f, notes, _ = make(slow=22.7 * 60)
    await step(mon, f, 0); assert f.syncs == ["03:50:00"]
    # ...and up to 4 minutes after the slot (a jump landing 5 min after did serve on 2026-09-28)
    at(3, 58); mon, f, notes, _ = make(slow=22.7 * 60)
    await step(mon, f, 0); assert f.syncs == ["03:58:00"]
    print("c/d ok: slow clock noticed before, or just after, a meal is due -> synced")

    # e. the 2026-10-12 case: 6 minutes past the slot and the feeder hasn't started -> hold, sync after
    at(4, 1, 14); mon, f, notes, _ = make(slow=22.7 * 60)
    await step(mon, f, 0)
    assert not f.syncs and notes == [("FYI", "Feeder clock is 22.7 min slow — fixing it after the 03:55 meal")], (f.syncs, notes)
    for _ in range(10): await step(mon, f)                     # still holding, one notice only
    assert not f.syncs and len(notes) == 1
    at(4, 7, 43); await step(mon, f, 0, state=1); at(4, 17, 42); await step(mon, f, 0, state=2)
    assert not f.syncs, "never mid-meal"
    await step(mon, f, 10, state=0); await step(mon, f, 120)
    assert len(f.syncs) == 1 and f.syncs[0] >= "04:17:52", f.syncs
    print("e ok: held while 03:55 was unserved; it ran late by itself; synced after at", f.syncs[0])

    # f. a meal skipped for today can't be lost -> sync straight away
    at(4, 1, 14); mon, f, notes, _ = make(slow=22.7 * 60)
    f.plan = json.loads(json.dumps(PLAN)); f.plan["bDayEn"][0] = 0
    await step(mon, f, 0); assert len(f.syncs) == 1
    print("f ok: skipped meal -> no hold")

    # g. fast clock: don't jump back into a meal it has already started; fine once that meal has expired
    at(3, 50); mon, f, notes, _ = make(slow=-600)              # feeder thinks 04:00; it began 03:55 early
    await step(mon, f, 0); assert not f.syncs and "fixing it after the 03:55" in notes[0][1]
    at(4, 0); await step(mon, f, 0); assert f.syncs == ["04:00:00"]
    print("g ok: fast clock held until the meal it already ran has expired, then synced")

    # h. a sync that doesn't take: two tries, one alert, then every 30 minutes
    at(14, 0); mon, f, notes, _ = make(slow=900); f.sync_works = False
    for _ in range(12): await step(mon, f)
    assert len(f.syncs) == 3 and [t for _, t in notes] == ["Feeder clock was 15.0 min slow — synced", "Feeder clock won't sync"], (f.syncs, notes)
    for _ in range(60): await step(mon, f)                     # 30 more minutes
    assert len(f.syncs) == 4 and len(notes) == 2
    f.sync_works = True
    for _ in range(62): await step(mon, f)
    assert f.slow == 0 and len(notes) == 2
    print("h ok: stuck clock -> 2 tries, one alert, retry every 30 min; recovers quietly")

    # i. hours off is a timezone setting: leave it, say so once; j. kill switch; k. unreadable clock
    at(14, 0); mon, f, notes, _ = make(slow=3600 * 5)
    for _ in range(10): await step(mon, f)
    assert not f.syncs and notes == [("ACT", "Feeder clock is hours off")]
    at(14, 0); mon, f, notes, d = make(slow=900); (d / "KILL").touch()
    for _ in range(6): await step(mon, f)
    assert not f.syncs
    at(14, 0); mon, f, notes, _ = make(slow=900); f.fail = True
    for _ in range(6): await step(mon, f)
    assert not f.syncs and not notes
    await step(mon, f, 120, state=5); r0 = f.reads; await step(mon, f, 300); assert f.reads == r0, "not while asleep on battery"
    print("i/j/k ok: timezone-sized error left alone; kill switch blocks it; unreadable or asleep -> nothing")

    # l. the overdue check only alerts, once, and never syncs
    at(4, 1, 30); mon, f, notes, _ = make()
    class L:
        async def ledger(self, *a, **k): return {"feed_list": [], "eat_list": []}
    mon.neakasa, mon.device_name = L(), "dev"
    await mon._check_schedule_drift(f.status()); await mon._check_schedule_drift(f.status())
    assert not f.syncs and notes == [("ACT", "03:55 meal hasn't run")], (f.syncs, notes)
    print("l ok: overdue check ->", notes[0][1], "(once, no sync)")
asyncio.run(main())
