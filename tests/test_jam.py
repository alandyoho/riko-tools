"""Grinder faults in monitor.py: one alert per jam (not one per poll), the live scale read,
and the alert when food drops into the bowl while the feeder is stuck. Simulated feeder."""
import asyncio, json, sys, tempfile, time as real, types
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor as M
from riko import RikoStatus

clock = {"now": real.mktime((2026, 10, 12, 19, 52, 16, 0, 0, -1))}
M.time = types.SimpleNamespace(
    time=lambda: clock["now"], localtime=lambda t=None: real.localtime(clock["now"] if t is None else t),
    gmtime=real.gmtime, mktime=real.mktime,
    strftime=lambda fmt, t=None: real.strftime(fmt, real.localtime(clock["now"]) if t is None else t))
PLAN = {"bEn": [1]*6, "bDayEn": [1]*6, "time": [14100, 28500, 42900, 57300, 71700, 86100], "rept": [127]*6,
        "food": [3, 8, 8, 8, 8, 8], "water": [21, 56, 56, 56, 56, 56], "aud": [2]*6, "type": [1]*6}
class Feeder:
    def __init__(self): self.state, self.param, self.gram, self.fail, self.reads = 3, 13, 95.0, False, 0
    cover_at = None                 # when the feeder last reported the hopper/lid taken out or put back
    def status(self):
        raw = {"fdPlanStr": {"value": json.dumps(PLAN)},
               "deviceState": {"value": {"state": self.state, "param": self.param}}}
        if self.cover_at: raw["bCoverAbsent"] = {"time": self.cover_at * 1000, "value": 1}
        return RikoStatus(raw)
    async def live_scale(self):
        self.reads += 1
        if self.fail: raise TimeoutError()
        return self.gram, 68
def make():
    f, notes = Feeder(), []
    n = types.SimpleNamespace(action_needed=lambda t, b: notes.append((t, b)) or True, fyi=lambda t, b: True)
    mon = M.Monitor(f, M.Store(Path(tempfile.mkdtemp()) / "m.db"), n, M.Policy(killswitch=Path("/nonexistent")), 0, False)
    return mon, f, notes
async def poll(mon, f, secs=30, **changes):
    clock["now"] += secs
    for k, v in changes.items(): setattr(f, k, v)
    await mon._check_state(f.status())

async def main():
    # 1. a jam: one alert, with the bowl weight and the take-the-bowl-off advice; no repeat every poll
    mon, f, notes = make()
    await poll(mon, f, 0)
    assert len(notes) == 1 and notes[0][0] == "Feeder stuck: grinder jammed with food", notes
    assert "Take the bowl off" in notes[0][1] and "about 27 g right now" in notes[0][1], notes[0][1]
    for _ in range(5): await poll(mon, f)
    assert len(notes) == 1, "one alert per jam, not one per poll"
    print("1 ok:", notes[0][0], "|", notes[0][1][-40:])

    # 2. food drops into the bowl while it is stuck -> a second alert, once
    await poll(mon, f, gram=151.0)                         # 27 g -> 83 g, as on 2026-10-12
    assert len(notes) == 2 and notes[1][0] == "About 56 g just dropped into the bowl", notes[1]
    await poll(mon, f); await poll(mon, f, gram=153.0); assert len(notes) == 2
    print("2 ok:", notes[1][0])

    # 3. still stuck after 10 minutes -> one reminder; the fault ending resets everything
    for _ in range(14): await poll(mon, f)
    assert [t for t, _ in notes][2:] == ["Feeder still stuck: grinder jammed with food"], [t for t, _ in notes]
    await poll(mon, f, state=0, param=0); assert mon._jam is None and len(notes) == 3
    await poll(mon, f, state=3, param=13, gram=70.0); assert len(notes) == 4 and "about 2 g right now" in notes[3][1]
    print("3 ok: reminder after 10 min; a new jam alerts again")

    # 4. bowl off the tray, then put back: measure the jump from when it came back
    mon, f, notes = make(); f.gram = -1.0
    await poll(mon, f, 0); assert "off the tray" in notes[0][1]
    await poll(mon, f, gram=70.0); await poll(mon, f, gram=72.0); assert len(notes) == 1
    await poll(mon, f, gram=95.0); assert notes[-1][0] == "About 25 g just dropped into the bowl"
    print("4 ok: bowl off -> on is not a dump; a later jump is")

    # 5. the scale can't be read -> the alert still goes out; 6. other grinder faults keep the plain text
    mon, f, notes = make(); f.fail = True
    await poll(mon, f, 0); assert len(notes) == 1 and "Take the bowl off" in notes[0][1]
    mon, f, notes = make(); f.param = 10
    await poll(mon, f, 0); await poll(mon, f)
    assert len(notes) == 1 and notes[0][0] == "Feeder stuck: grinder head missing" and "bowl" not in notes[0][1]
    print("5/6 ok: alert survives a failed scale read; grinder-head-missing keeps the plain message")

    # 7. the jam log: one row per episode, bowl samples while stuck, and how it ended
    jams = lambda mon: mon.store.db.execute(
        "select slot, code, end - start, outcome, bowl_start, bowl_max, bowl_end, samples from jams order by rowid").fetchall()
    mon, f, notes = make(); f.gram = 95.0
    await poll(mon, f, 0); await poll(mon, f); await poll(mon, f, gram=97.0)
    r = jams(mon)[0]; assert r[3] is None and r[5] == 29.0 and len(json.loads(r[7])) == 3, r       # open, still stuck
    f.cover_at = clock["now"] + 10; await poll(mon, f, state=1, param=0)  # hopper out and back, then preparing
    await poll(mon, f, state=3, param=13); await poll(mon, f, 600, state=2, param=2)   # nobody came: times out
    await poll(mon, f, state=0, param=0)
    await poll(mon, f, state=3, param=13, gram=90.0); await poll(mon, f, state=3, param=12)   # lid opened
    rows = jams(mon)
    assert [x[3] for x in rows] == ["cleared by hand (hopper out and back)", "gave up, served what it had",
                                    "then cover missing"], rows
    assert len(rows[0][0]) == 5 and rows[0][1] == 13 and rows[0][2] == 90 and (rows[0][4], rows[0][6]) == (27.0, 29.0), rows[0]
    assert rows[1][2] == 600, rows[1]
    print("7 ok: jam log ->", [(x[2], x[3]) for x in rows])
asyncio.run(main())
