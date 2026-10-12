"""Battery backup: power.py decoding, monitor alerts, power_guard state machine, display lines."""
import asyncio, json, struct, sys, tempfile, time as real, types
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import power as P

# 1. the real UPS, right now
ups = P.read_ups()
if ups is None:
    print("1 skipped: no UPS on this machine")
else:
    assert 3.0 < ups.volts < 4.3, ups
    print("1 ok: live reading %.3f V, %+.0f mA, ~%d%%, on_battery=%s" % (ups.volts, ups.ma, ups.percent, ups.on_battery))

# 2. decoding and the derived values
d = P.decode(struct.pack(">H", int(3.96 / 0.004) << 3), struct.pack(">h", -500))     # -5.00 mV
assert abs(d.volts - 3.96) < 0.005 and abs(d.ma + 500) < 1 and d.on_battery and not d.charging and d.percent == 80
assert d.minutes_left == 96, d.minutes_left
c = P.decode(struct.pack(">H", int(4.156 / 0.004) << 3), struct.pack(">h", 440))
assert c.charging and not c.on_battery and c.minutes_left is None and c.percent == 96
full = P.Ups(4.19, 3.0); assert not full.on_battery and not full.charging
print("2 ok: decode; 3.96 V at -500 mA -> %d%%, ~%d min" % (d.percent, d.minutes_left))

# 3. monitor alerts: wait 2 min, retry until sent, low battery once, power back
import monitor as M
clock = {"now": 1000.0}; reading = {"v": P.Ups(4.15, 440.0)}; net = {"up": False}; notes = []
M.time = types.SimpleNamespace(time=lambda: clock["now"], localtime=real.localtime, strftime=real.strftime, gmtime=real.gmtime)
M.power = types.SimpleNamespace(read_ups=lambda: reading["v"])
def send(kind):
    def f(title, body):
        notes.append((kind, title, net["up"])); return net["up"]
    return f
n = types.SimpleNamespace(action_needed=send("ACT"), fyi=send("FYI"))
mon = M.Monitor(None, None, n, M.Policy(killswitch=Path("/nonexistent")), 0, False)
def tick(s=30): clock["now"] += s; mon._check_power()
tick(); assert not notes
reading["v"] = P.Ups(3.96, -500.0)
tick(); tick(); assert not notes, "must wait a minute on battery"
tick(); assert notes[-1][:2] == ("ACT", "Power is out — Pi running on battery") and not mon._power_alerted
tick(); assert len(notes) == 2, "retries while the network is down"
net["up"] = True; tick(); assert mon._power_alerted and len(notes) == 3
tick(); tick(); assert len(notes) == 3, "sent once"
reading["v"] = P.Ups(3.54, -520.0); tick(); assert notes[-1][1] == "Pi battery low"
tick(); assert len(notes) == 4
reading["v"] = P.Ups(3.60, 450.0); tick()
assert notes[-1][:2] == ("FYI", "Power is back — Pi on wall power again") and not mon._power_alerted and mon._on_battery_since is None
tick(); assert len(notes) == 5
reading["v"] = P.Ups(3.96, -500.0); tick(30); reading["v"] = P.Ups(4.0, 400.0); tick()
assert len(notes) == 5, "a blip under a minute says nothing"
M.power = types.SimpleNamespace(read_ups=lambda: None); tick(); assert len(notes) == 5
print("3 ok: alerts ->", [t for _, t, sent in notes if sent])

# 4. power guard: safe idle after 3 low readings on battery, reboot after 30 s of charging
import power_guard as G
acts = []
G.enter_safe_idle = lambda flag, n, u: acts.append("IDLE %.2f" % u.volts)
G.reboot = lambda: acts.append("REBOOT")
seq = []
g = G.Guard(Path(tempfile.mkdtemp()) / "SAFE_IDLE", n, read=lambda: seq.pop(0))
def feed(*readings):
    for r in readings: seq.append(r); g.tick()
feed(P.Ups(4.1, 440), P.Ups(3.9, -500), P.Ups(3.5, -500), P.Ups(3.41, -500)); assert not acts
feed(P.Ups(3.39, -500), P.Ups(3.38, -500)); assert not acts, "needs three in a row"
feed(P.Ups(3.45, -500), P.Ups(3.39, -500), P.Ups(3.39, -500)); assert not acts, "a bounce resets it"
feed(P.Ups(3.38, -520)); assert acts == ["IDLE 3.38"] and g.idle
feed(P.Ups(3.36, -200), P.Ups(3.37, 400), P.Ups(3.4, 400), P.Ups(3.4, -100), *[P.Ups(3.45, 420)] * 5); assert acts == ["IDLE 3.38"]
feed(P.Ups(3.46, 420)); assert acts == ["IDLE 3.38", "REBOOT"]
g2 = G.Guard(Path("/tmp/x"), n, read=lambda: P.Ups(3.2, 440)); [g2.tick() for _ in range(5)]; assert not g2.idle, "low but charging is fine"
print("4 ok: guard ->", acts)

# 5. display: battery lines come first, safe-idle message replaces everything
if len(sys.argv) > 1:      # optional: a captured properties file (riko_capture/props_*.json)
    for m in ("board", "busio", "adafruit_ssd1305"):
        sys.modules[m] = types.SimpleNamespace(SCL=0, SDA=0, I2C=lambda *a: None, SSD1305_I2C=lambda *a: None)
    import oled_status as O
    d = Path(tempfile.mkdtemp()); sp = d / "status.json"
    raw = json.loads(Path(sys.argv[1]).read_text())
    sp.write_text(json.dumps({"ts": real.time(), "raw": raw, "error": None}))
    O.power = types.SimpleNamespace(read_ups=lambda: P.Ups(4.15, 440.0)); normal = O.get_status_messages(sp)
    assert "Power" not in normal[0]
    O.power = types.SimpleNamespace(read_ups=lambda: P.Ups(3.96, -500.0)); batt = O.get_status_messages(sp)
    assert batt[:2] == ["Power's out! I'm on", "battery 80%, ~96min"] and batt[2:] == normal, batt
    sp.write_text(json.dumps({"ts": real.time(), "raw": None, "error": "TransportError('x')"}))
    assert O.get_status_messages(sp)[:3] == ["Power's out! I'm on", "battery 80%, ~96min", "Uh oh..."]
    (d / "SAFE_IDLE").touch(); assert O.get_status_messages(sp)[0] == "Battery's nearly out."
    O.power = types.SimpleNamespace(read_ups=lambda: None); (d / "SAFE_IDLE").unlink(); assert O.get_status_messages(sp)[0] == "Uh oh..."
    print("5 ok: display on battery ->", batt[:2])
else:
    print("5 skipped: pass a captured properties file to test the display lines")
