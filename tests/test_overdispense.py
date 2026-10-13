"""Over-dispense alert (topup.unexplained_extra + monitor._check_overdispense). No network."""
import asyncio, json, sys, tempfile, time, types
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor as M, topup as TU
T = lambda t: time.strftime("%m-%d %H:%M", time.localtime(t))

# 1. against the real records: which meals would it flag?
if len(sys.argv) > 1:      # optional: a ledger dump (python neakasa.py raw --days 18 > file)
    d = json.load(open(sys.argv[1])); feeds = sorted(d["feed_list"], key=lambda f: f["feed_time"])
    flagged, n = [], 0
    for f in feeds:
        res = TU.unexplained_extra(d, f)
        if res is None: continue
        n += 1
        if res[0] >= 8: flagged.append("%s %+d g" % (T(f["feed_time"]), res[0]))
    print("1 ok: %d meals checked, flagged:" % n, flagged)
else:
    print("1 skipped: pass a ledger dump to replay real meals")

# 2. through the monitor: alert only when the reading can be trusted
M._near_a_feed_slot = lambda st, *a: True
now = int(time.time()); prev, this = now - 4 * 3600, now - 60
def ledger(after):
    return {"feed_list": [{"feed_time": prev, "status": 1, "feed_weight": 8, "water_weight": 56, "left_weight": 64, "way": "auto"},
                          {"feed_time": this, "status": 1, "feed_weight": 8, "water_weight": 56, "left_weight": after, "way": "auto",
                           "plan_feed_weight": 8, "plan_water_weight": 56}],
            "eat_list": [{"start_time": prev + 600, "end_time": prev + 1200, "eat_weight": 64, "left_weight": 0}]}
async def run(after, scale_at_prep):
    notes = []
    n = types.SimpleNamespace(action_needed=lambda t, b: notes.append((t, b)) or True, fyi=lambda t, b: True)
    led = ledger(after)
    class F:
        async def ledger(self, *a, **k): return led
    mon = M.Monitor(None, M.Store(Path(tempfile.mkdtemp()) / "m.db"), n, M.Policy(killswitch=Path("/nonexistent")), 0, False, neakasa=F(), device_name="dev")
    mon.store.put_setting("ledger_last_feed_time", str(prev))
    mon._scale_at_prep = scale_at_prep
    await mon.check_ledger_failures(None)
    await mon.check_ledger_failures(None)          # second pass must not repeat it
    return notes
async def main():
    a = await run(76, prev - 3600); assert len(a) == 1 and a[0][0].endswith("about 12 g too much in the bowl"), a
    print("2a ok:", a[0][0], "|", a[0][1][:118])
    assert not await run(66, prev - 3600);  print("2b ok: +2 g says nothing")
    assert not await run(76, prev + 3600);  print("2c ok: bowl handled before the meal -> no alert")
    assert not await run(76, None);         print("2d ok: monitor wasn't watching the prep -> no alert")
    assert not await run(0, prev - 3600);   print("2e ok: no bowl reading on the record -> no alert")
    # a failed (jammed) meal that leaves far more in the bowl than it recorded - 2026-10-12
    notes = []
    n = types.SimpleNamespace(action_needed=lambda t, b: notes.append((t, b)) or True, fyi=lambda t, b: True)
    led = ledger(83); led["feed_list"][1].update(status=2, feed_weight=0, water_weight=27, fail_reason='{"reason":1,"resParam":1}')
    class F:
        async def ledger(self, *a, **k): return led
    mon = M.Monitor(None, M.Store(Path(tempfile.mkdtemp()) / "m.db"), n, M.Policy(killswitch=Path("/nonexistent")), 0, False, neakasa=F(), device_name="dev")
    mon.store.put_setting("ledger_last_feed_time", str(prev)); mon._scale_at_prep = prev - 3600
    await mon.check_ledger_failures(None)
    titles = [t for t, _ in notes]
    assert any(t.endswith("about 56 g too much in the bowl") for t in titles) and any(t.startswith("Feed failed") for t in titles), titles
    print("2f ok: failed meal ->", titles)
asyncio.run(main())
