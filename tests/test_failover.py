"""Wi-Fi -> cellular failover (wifi_failover.py): the takeover / hand-back state machine.

Every network command is faked; nothing touches nmcli, hostapd, nftables or the modem.
"""
import os, subprocess, sys, tempfile, types, json
from pathlib import Path
os.environ["RIKO_FEEDER_MAC"] = "98:3d:ae:7a:56:cc"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import wifi_failover as W

# 1. parsing a Wi-Fi scan
sample = ("BSS 82:f8:53:cb:74:4c(on wlan0) -- associated\n\tTSF: 1\n\tSSID: LFYTT-iot\n"
          "BSS aa:bb:cc:dd:ee:ff(on wlan0)\n\tSSID: Other Net\n")
assert W.parse_iw_scan(sample) == [("82:f8:53:cb:74:4c", "LFYTT-iot"), ("aa:bb:cc:dd:ee:ff", "Other Net")]
print("1 ok: scan parsing")

# 2. firewall rules: only the feeder is forwarded
W.pwd = types.SimpleNamespace(getpwnam=lambda user: types.SimpleNamespace(pw_uid=1000))
rs = W.nft_ruleset()
assert "ether saddr != 98:3d:ae:7a:56:cc drop" in rs and "skuid 1000" in rs and "masquerade" in rs, rs
print("2 ok: nft ruleset")

# ---- the state machine, with the outside world faked -------------------------
calls = []
world = {"wlan": W.HOME_CON, "visible": True, "ap_scan": False, "bytes": 0, "now": 1000.0}
ap = {"ok": True}
def fake_run(*cmd, timeout=60, stdin=None):
    calls.append(" ".join(cmd)); return subprocess.CompletedProcess(cmd, 0, "", "")
def fake_ap_up(): calls.append("AP UP"); return ap["ok"]
def fake_ap_down(): calls.append("AP DOWN")
W.run, W.ap_up, W.ap_down, W.rejoin_home = fake_run, fake_ap_up, fake_ap_down, lambda wait_s=90: True
W.home_ssid = lambda: "LFYTT-iot"
W.wlan_connection = lambda: world["wlan"]
W.ssid_visible_as_client = lambda s: world["visible"]
W.ssid_visible_as_ap = lambda s: world["ap_scan"]
W.wwan_bytes = lambda: world["bytes"]
W.nft_apply = lambda: (calls.append("NFT") or True)
W.time = types.SimpleNamespace(time=lambda: world["now"], sleep=lambda s: None)
notes = []
n = types.SimpleNamespace(action_needed=lambda t, b: notes.append(("ACT", t)), fyi=lambda t, b: notes.append(("FYI", t)))
d = Path(tempfile.mkdtemp())
fo = W.Failover(d, n)
def tick(dt=15): world["now"] += dt; fo.tick()

# 4. hotspot takeover: wait the full window, cellular first, then the access point
tick(); assert not fo.active and not calls
world.update(wlan=None, visible=True); tick(); assert fo.down_since is None      # router visible, just reconnecting
world.update(visible=False)
for _ in range(6): tick()
assert not fo.active, "should wait the full window"
tick(); assert fo.active and (d / "FAILOVER").exists()
up = [c for c in calls if "connection up" in c or c == "AP UP"]
assert up[0].endswith("cellular-1nce") and up[1] == "AP UP", up
assert any("systemctl stop riko-watch" in c for c in calls) and notes[-1][0] == "ACT"
print("4a ok: takeover ->", notes[-1][1])
world["bytes"] = 3_000_000
for _ in range(4): tick()
assert fo.active and fo.seen == 0
world["ap_scan"] = True
for _ in range(4): tick()
assert fo.active and fo.seen == 1, "one sighting of the router is not enough"
for _ in range(4): tick()
assert not fo.active and not (d / "FAILOVER").exists() and notes[-1][0] == "FYI"
assert json.loads((d / "cellular_usage.json").read_text())["total_bytes"] == 3_000_000
assert "AP DOWN" in calls and any("start riko-watch" in c for c in calls)
print("4b ok: handed back after two sightings; 3.0 MB recorded;", notes[-1][1])

# 5. data cap cuts cellular
world.update(wlan=None, visible=False, ap_scan=False, bytes=0); calls.clear()
for _ in range(8): tick()
assert fo.active
world["bytes"] = 60_000_000; tick()
assert fo.capped and "nmcli connection down cellular-1nce" in calls and "cap" in notes[-1][1]
print("5 ok:", notes[-1][1])

# 6. kill switch backs out and blocks a takeover
(d / "DISABLE_FAILOVER").touch(); tick(); assert not fo.active
for _ in range(10): tick()
assert not fo.active
print("6 ok: kill switch backs out and blocks takeover")

# 7. no scanning while hosting -> drop the hotspot briefly to look
(d / "DISABLE_FAILOVER").unlink(); world.update(bytes=0)
for _ in range(8): tick()
assert fo.active
W.ssid_visible_as_ap = lambda s: None
for _ in range(5): tick()
assert fo.can_scan_as_ap is False and fo.active
world["visible"] = True
for _ in range(21): tick()
assert not fo.active
print("7 ok: falls back to peeking when scanning while hosting is unsupported")

# 8. hotspot fails to start -> back out, cellular off again
W.ssid_visible_as_ap = lambda s: world["ap_scan"]; fo.can_scan_as_ap = True
world.update(wlan=None, visible=False, bytes=0); calls.clear(); ap["ok"] = False
for _ in range(8): tick()
assert not fo.active and not (d / "FAILOVER").exists() and "nmcli connection down cellular-1nce" in calls
print("8 ok: failed hotspot start backs out cleanly")

# 9. while connected to the home Wi-Fi nothing happens, whatever the internet is doing
ap["ok"] = True; world.update(wlan=W.HOME_CON, visible=True, bytes=0); calls.clear()
for _ in range(20): tick()
assert not fo.active and not calls, calls
print("9 ok: connected to the home Wi-Fi -> no action")
