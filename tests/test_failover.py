"""Wi-Fi -> cellular failover (wifi_failover.py): the takeover / hand-back state machine.

Every network command is faked; nothing touches nmcli, hostapd, nftables or the modem.
"""
import os, subprocess, sys, tempfile, types, json
from pathlib import Path
os.environ["RIKO_FEEDER_MAC"] = "98:3d:ae:7a:56:cc"
os.environ["RIKO_FAILOVER_RELAY"] = "1"          # the relay is off by default; test it switched on
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import wifi_failover as W

# 1. parsing a Wi-Fi scan
sample = ("BSS 82:f8:53:cb:74:4c(on wlan0) -- associated\n\tTSF: 1\n\tSSID: LFYTT-iot\n"
          "BSS aa:bb:cc:dd:ee:ff(on wlan0)\n\tSSID: Other Net\n")
assert W.parse_iw_scan(sample) == [("82:f8:53:cb:74:4c", "LFYTT-iot"), ("aa:bb:cc:dd:ee:ff", "Other Net")]
print("1 ok: scan parsing")

# 2. firewall rules: only the feeder is forwarded; the relay adds the DNS redirect
W.pwd = types.SimpleNamespace(getpwnam=lambda user: types.SimpleNamespace(pw_uid=1000))
rs = W.nft_ruleset()
assert "ether saddr != 98:3d:ae:7a:56:cc drop" in rs and "skuid 1000" in rs and "masquerade" in rs and "dnat" not in rs, rs
assert "dnat ip to 8.8.8.8" in W.nft_ruleset(relay=True)
print("2 ok: nft ruleset (hotspot and relay)")

# 3. the ARP frame used by the relay
f = W.arp_reply("aa:aa:aa:aa:aa:aa", "192.168.1.1", "aa:aa:aa:aa:aa:aa", "98:3d:ae:7a:56:cc", "192.168.1.77")
assert len(f) == 42 and f[:6].hex() == "983dae7a56cc" and f[12:14] == b"\x08\x06" and f[20:22] == b"\x00\x02"
assert f[22:28].hex() == "aa" * 6 and f[28:32] == bytes([192, 168, 1, 1]) and f[38:42] == bytes([192, 168, 1, 77])
print("3 ok: ARP reply frame is well-formed")

# ---- the state machine, with the outside world faked -------------------------
calls = []
world = {"wlan": W.HOME_CON, "visible": True, "ap_scan": False, "bytes": 0, "now": 1000.0, "internet": True}
ap = {"ok": True}
def fake_run(*cmd, timeout=60, stdin=None):
    calls.append(" ".join(cmd)); return subprocess.CompletedProcess(cmd, 0, "", "")
def fake_ap_up(): calls.append("AP UP"); return ap["ok"]
def fake_ap_down(): calls.append("AP DOWN")
arps = []
class FakeArp:
    def __init__(self, feeder_ip, real): self.feeder_ip, self.real = feeder_ip, real; arps.append(self)
    def start(self): calls.append("ARP START")
    def stop(self): calls.append("ARP STOP")
W.run, W.ap_up, W.ap_down, W.rejoin_home = fake_run, fake_ap_up, fake_ap_down, lambda wait_s=90: True
W.home_ssid = lambda: "LFYTT-iot"
W.wlan_connection = lambda: world["wlan"]
W.ssid_visible_as_client = lambda s: world["visible"]
W.ssid_visible_as_ap = lambda s: world["ap_scan"]
W.wwan_bytes = lambda: world["bytes"]
W.internet_via_router = lambda: world["internet"]
W.lan_info = lambda: {"gateway": "192.168.1.1", "dns": "192.168.1.151", "subnet": "192.168.1."}
W.neighbors = lambda: {"192.168.1.1": "b8:f8:53:cb:74:49", "192.168.1.151": "2c:cf:67:92:e8:65",
                       "192.168.1.77": "98:3d:ae:7a:56:cc"}
W.prefer_cellular_route = lambda: (calls.append("ROUTE VIA CELL") or True)
W.nft_apply = lambda relay=False: (calls.append(f"NFT relay={relay}") or True)
W.ArpRedirect = FakeArp
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
tick(); assert fo.mode == "hotspot" and (d / "FAILOVER").exists()
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

# 9. relay: router up, internet down
ap["ok"] = True; world.update(wlan=W.HOME_CON, visible=True, internet=True, bytes=0); calls.clear()
tick(); assert not fo.active
world["internet"] = False
for _ in range(8): tick()
assert not fo.active, "should wait the full window"
tick(); assert fo.mode == "relay", fo.mode
order = [c for c in calls if c in ("NFT relay=True", "ROUTE VIA CELL", "ARP START") or c.endswith("up cellular-1nce")]
assert [o.split()[-1] for o in order] == ["cellular-1nce", "relay=True", "CELL", "START"], order
assert arps[-1].feeder_ip == "192.168.1.77" and set(arps[-1].real) == {"192.168.1.1", "192.168.1.151"}
assert "AP UP" not in calls and "Internet down" in notes[-1][1]
world["internet"] = True
for _ in range(9): tick()
assert not fo.active and "ARP STOP" in calls and notes[-1] == ("FYI", "Internet back — cellular backup off")
print("9 ok: relay entered and left;", notes[-1][1])

# 10. the test switch drives the relay; the router dying mid-relay falls through to the hotspot
calls.clear(); (d / "SIMULATE_INTERNET_DOWN").touch()
for _ in range(10): tick()
assert fo.mode == "relay"
(d / "SIMULATE_INTERNET_DOWN").unlink()
for _ in range(9): tick()
assert not fo.active
world["internet"] = False
for _ in range(10): tick()
assert fo.mode == "relay"
world.update(wlan=None, visible=False); tick(); assert not fo.active
for _ in range(8): tick()
assert fo.mode == "hotspot"
print("10 ok: test switch works; router dying mid-relay becomes a hotspot takeover")

# 11. relay switched off (the default): an internet outage does nothing, the hotspot still works
W.RELAY_ENABLED = False
world.update(wlan=W.HOME_CON, visible=True, internet=True, bytes=0)
(d / "DISABLE_FAILOVER").touch(); tick(); (d / "DISABLE_FAILOVER").unlink()
assert not fo.active
world["internet"] = False; calls.clear()
for _ in range(20): tick()
assert not fo.active and not calls, calls
world.update(wlan=None, visible=False)
for _ in range(8): tick()
assert fo.mode == "hotspot"
print("11 ok: with the relay off, an internet outage does nothing; the hotspot takeover still works")
