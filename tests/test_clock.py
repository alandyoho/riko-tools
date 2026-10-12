"""Clock upkeep in monitor.py (_keep_clock): simulated feeder, fake clock. No network."""
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
    def __init__(self): self.state, self.pwr, self.syncs, self.plan = 0, 0, [], json.loads(json.dumps(PLAN))
    def status(self):
        return RikoStatus({"fdPlanStr": {"value": json.dumps(self.plan)}, "deviceState": {"value": {"state": self.state, "param": 0}},
                           "pwrStatus": {"value": {"pwrType": self.pwr, "batPercentage": 90}},
                           "defFdCfg": {"value": {"food": 8, "water": 56, "mixMin": 10}}})
    async def sync_time(self, source=1): self.syncs.append(real.strftime("%H:%M:%S", real.localtime(clock["now"])))
def make(d=None, started=True):
    d = d or Path(tempfile.mkdtemp()); f, notes = Feeder(), []
    n = types.SimpleNamespace(action_needed=lambda t, b: notes.append(("ACT", t)) or True, fyi=lambda t, b: notes.append(("FYI", t)) or True)
    mon = M.Monitor(f, M.Store(d / "m.db"), n, M.Policy(killswitch=d / "KILL"), 0, False)
    mon._clock_synced_since_start = started
    return mon, f, notes, d
async def step(mon, f, secs=30, state=None, pwr=None):
    clock["now"] += secs
    if state is not None: f.state = state
    if pwr is not None: f.pwr = pwr
    await mon._keep_clock(f.status())

async def main():
    # a. last night, replayed: 3 minutes on battery in the evening -> synced as soon as power is back
    at(20, 17); mon, f, notes, _ = make(); mon._meal_seen_at = clock["now"] - 22 * 60
    await step(mon, f, 0)
    await step(mon, f, pwr=1); await step(mon, f, state=5); await step(mon, f); assert not f.syncs
    await step(mon, f, state=0, pwr=0)
    assert len(f.syncs) == 1 and notes == [("FYI", "Feeder lost power — clock re-synced")], (f.syncs, notes)
    await step(mon, f); await step(mon, f); assert len(f.syncs) == 1
    print("a ok: power back ->", f.syncs, notes[0][1])

    # b. after every meal, quietly
    at(11, 44); mon, f, notes, _ = make()
    await step(mon, f, 0); await step(mon, f, 60, state=1)
    for _ in range(19): await step(mon, f)
    await step(mon, f, state=2); assert not f.syncs, "never mid-meal"
    await step(mon, f, state=0); assert len(f.syncs) == 1 and not notes
    await step(mon, f); assert len(f.syncs) == 1
    print("b ok: synced after the meal at", f.syncs[0], "(no notification)")

    # c. slow clock caught before the slot: 9 minutes to go and still idle
    at(3, 44); mon, f, notes, _ = make(); mon._meal_seen_at = clock["now"] - 3.8 * 3600
    await step(mon, f, 0); await step(mon, f, 60); assert not f.syncs, "prep isn't late yet at T-10"
    await step(mon, f, 45)                                  # 03:45:45
    assert len(f.syncs) == 1 and notes[0][1].startswith("Feeder clock was behind — synced before the 03:55"), (f.syncs, notes)
    await step(mon, f); await step(mon, f); assert len(f.syncs) == 1, "one try per meal"
    print("c ok: late prep -> synced at", f.syncs[0], "|", notes[0][1])
    # ...but not when it is preparing on time, or the meal is skipped for today
    at(3, 44); mon, f, notes, _ = make(); await step(mon, f, 0); await step(mon, f, 62, state=1); await step(mon, f, 45)
    assert not f.syncs
    at(3, 44); mon, f, notes, _ = make(); f.plan["bDayEn"][0] = 0; await step(mon, f, 0); await step(mon, f, 105); assert not f.syncs
    print("c2 ok: preparing on time, or skipped for today -> no sync")

    # d. the 2026-10-12 case: meal overdue and not seen to run -> hold, so it can still serve late
    at(4, 1); mon, f, notes, _ = make(); mon._meal_seen_at = clock["now"] - 3.7 * 3600
    mon._feeder_on_battery_since = clock["now"] - 600      # even with a power-return sync waiting
    await step(mon, f, 0); await step(mon, f); assert not f.syncs, "must not jump the clock past an unserved meal"
    at(4, 7, 43); await step(mon, f, 0, state=1); at(4, 17, 42); await step(mon, f, 0, state=2)
    await step(mon, f, 10, state=0)
    assert len(f.syncs) == 1, f.syncs
    print("d ok: held while 03:55 was unserved; meal ran late by itself; synced after it at", f.syncs[0])
    # the old check now only alerts
    at(4, 1, 30); mon, f, notes, _ = make()
    class L:
        async def ledger(self, *a, **k): return {"feed_list": [], "eat_list": []}
    mon.neakasa, mon.device_name = L(), "dev"
    await mon._check_schedule_drift(f.status()); await mon._check_schedule_drift(f.status())
    assert not f.syncs and notes == [("ACT", "03:55 meal hasn't run")], (f.syncs, notes)
    print("d2 ok: overdue check ->", notes[0][1], "(once, no sync)")

    # e. once after the monitor starts; f. kill switch; g. not while asleep on battery
    at(14, 0); mon, f, notes, _ = make(started=False); mon._meal_seen_at = clock["now"] - 2 * 3600
    await step(mon, f, 0); await step(mon, f); assert len(f.syncs) == 1 and not notes
    at(20, 17); mon, f, notes, d = make(); (d / "KILL").touch(); mon._meal_seen_at = clock["now"]
    await step(mon, f, 0, pwr=1); await step(mon, f, pwr=0); assert not f.syncs
    at(20, 17); mon, f, notes, _ = make(); mon._meal_seen_at = clock["now"]
    await step(mon, f, 0, state=5, pwr=1); await step(mon, f, 600); assert not f.syncs
    await step(mon, f, state=0, pwr=0); assert len(f.syncs) == 1
    print("e/f/g ok: one sync at start; kill switch blocks it; nothing while asleep on battery")
asyncio.run(main())
