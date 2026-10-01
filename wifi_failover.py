#!/usr/bin/env python3
"""
wifi_failover.py — keep the feeder online when the home router disappears.

The feeder only knows one Wi-Fi network. When that network vanishes, this turns
the Pi's Wi-Fi into an access point with the SAME name and password, so the
feeder reconnects on its own, and routes it out over the cellular modem. When
the real router is back, it hands the feeder back and turns cellular off.

The access point is hostapd with a MAC allowlist (/etc/riko-failover/accept):
every other device that knows the password is refused at the Wi-Fi level. While
it runs, wlan0 is taken away from NetworkManager; a private dnsmasq hands out
addresses and nftables does the NAT to the modem.

  normal    Pi is a Wi-Fi client; cellular is down (uses no data).
  takeover  home SSID gone for DOWN_BEFORE_TAKEOVER_S -> cellular up, AP up.
  return    real router seen again in RETURN_CONFIRMATIONS scans -> AP down,
            rejoin home Wi-Fi, cellular down.

The SIM has a small lifetime data allowance, so during a takeover:
  * only allowlisted devices can join, and only the feeder's MAC is forwarded;
  * the Pi's own traffic over cellular is limited to the monitor, DNS/NTP and
    this daemon's alerts (nftables table riko_failover);
  * riko-watch is stopped and the monitor slows down (riko_state/FAILOVER flag);
  * an episode that uses more than EPISODE_CAP_MB cuts cellular and alerts.

Does NOT cover "router up but internet down" (the feeder stays on the router),
or a power cut (the Pi has no battery yet).

Runs as root (nmcli/nft). Set up with failover-setup.sh.
  python3 wifi_failover.py            # run forever (systemd)
  python3 wifi_failover.py --check    # verify prerequisites, change nothing
  python3 wifi_failover.py --cleanup  # undo a takeover (AP/cellular/nft/flag)

Kill switch: create riko_state/DISABLE_FAILOVER and it will never take over
(and backs out of a takeover in progress).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pwd
import subprocess
import sys
import time
from pathlib import Path

from config import load as load_config
from notify import Notifier, NotifyConfig

log = logging.getLogger("riko.failover")

HOME_CON = os.environ.get("RIKO_HOME_WIFI_CON", "netplan-wlan0-LFYTT-iot")
AP_DIR = Path("/etc/riko-failover")      # hostapd.conf, dnsmasq.conf, accept (failover-setup.sh)
AP_ADDR = "10.42.0.1/24"
CELL_CON = os.environ.get("RIKO_CELL_CON", "cellular-1nce")
WLAN = "wlan0"
WWAN = "wwan0"
FEEDER_MAC = os.environ.get("RIKO_FEEDER_MAC", "").lower()
MONITOR_USER = os.environ.get("RIKO_MONITOR_USER", "yoho")

CHECK_S = 15                  # main loop tick
DOWN_BEFORE_TAKEOVER_S = 90   # home SSID must be gone this long before taking over
RETURN_SCAN_S = 60            # how often to look for the real router during a takeover
RETURN_CONFIRMATIONS = 2      # consecutive scans that must see it before handing back
PEEK_S = 300                  # if scanning in AP mode doesn't work: drop the AP this often to look
EPISODE_CAP_MB = 50           # cut cellular if one takeover uses more than this
USAGE_ALERT_MB = (100, 250, 400)   # lifetime-usage alerts (SIM allowance is 500 MB)
NFT_TABLE = "riko_failover"


def run(*cmd: str, timeout: int = 60, stdin: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=stdin)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timed out")
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: not installed")


# ---- observing ---------------------------------------------------------------
def home_ssid() -> str:
    return run("nmcli", "-g", "802-11-wireless.ssid", "connection", "show", HOME_CON).stdout.strip()


def wlan_connection() -> str | None:
    """Name of the active connection on wlan0, or None if not connected."""
    out = run("nmcli", "-t", "-f", "GENERAL.STATE,GENERAL.CONNECTION", "device", "show", WLAN).stdout
    fields = dict(line.split(":", 1) for line in out.splitlines() if ":" in line)
    if not fields.get("GENERAL.STATE", "").startswith("100"):
        return None
    return fields.get("GENERAL.CONNECTION") or None


def ssid_visible_as_client(ssid: str) -> bool:
    out = run("nmcli", "-t", "-f", "SSID", "device", "wifi", "list", "ifname", WLAN,
              "--rescan", "yes", timeout=45).stdout
    return any(line.replace("\\:", ":") == ssid for line in out.splitlines())


def parse_iw_scan(text: str) -> list[tuple[str, str]]:
    """[(bssid, ssid)] from `iw dev X scan` output."""
    found, bssid = [], None
    for line in text.splitlines():
        if line.startswith("BSS "):
            bssid = line[4:21].lower()
        elif bssid and line.strip().startswith("SSID:"):
            found.append((bssid, line.split("SSID:", 1)[1].strip()))
            bssid = None
    return found


def ssid_visible_as_ap(ssid: str) -> bool | None:
    """Scan while we are the AP. None = this chip/driver won't scan in AP mode."""
    res = run("iw", "dev", WLAN, "scan", "ap-force", timeout=45)
    if res.returncode != 0:
        log.info("AP-mode scan failed (%s)", res.stderr.strip()[:80])
        return None
    own = Path(f"/sys/class/net/{WLAN}/address").read_text().strip().lower()
    return any(s == ssid and b != own for b, s in parse_iw_scan(res.stdout))


def wwan_bytes() -> int:
    total = 0
    for name in ("rx_bytes", "tx_bytes"):
        try:
            total += int(Path(f"/sys/class/net/{WWAN}/statistics/{name}").read_text())
        except (OSError, ValueError):
            pass
    return total


# ---- acting ------------------------------------------------------------------
def nft_ruleset() -> str:
    uid = pwd.getpwnam(MONITOR_USER).pw_uid
    return f"""
table inet {NFT_TABLE} {{
    chain forward {{
        type filter hook forward priority -5; policy accept;
        iifname "{WLAN}" ether saddr != {FEEDER_MAC} drop
    }}
    chain postrouting {{
        type nat hook postrouting priority srcnat; policy accept;
        oifname "{WWAN}" ip saddr 10.42.0.0/24 masquerade
    }}
    chain output {{
        type filter hook output priority 0; policy accept;
        oifname "{WWAN}" ct state established,related accept
        oifname "{WWAN}" meta skuid {uid} accept
        oifname "{WWAN}" meta skuid 0 tcp dport 443 accept
        oifname "{WWAN}" meta l4proto icmp accept
        oifname "{WWAN}" udp dport {{ 53, 123 }} accept
        oifname "{WWAN}" tcp dport 53 accept
        oifname "{WWAN}" drop
    }}
}}
"""


def nft_apply() -> bool:
    nft_clear()
    res = run("nft", "-f", "-", stdin=nft_ruleset())
    if res.returncode != 0:
        log.error("nft rules failed: %s", res.stderr.strip())
    return res.returncode == 0


def nft_clear() -> None:
    run("nft", "delete", "table", "inet", NFT_TABLE)


def ap_up() -> bool:
    """Take wlan0 from NetworkManager and host the cloned network on it."""
    run("nmcli", "device", "set", WLAN, "managed", "no")
    time.sleep(2)
    run("ip", "addr", "flush", "dev", WLAN)
    run("ip", "link", "set", WLAN, "up")
    run("ip", "addr", "add", AP_ADDR, "dev", WLAN)
    res = run("hostapd", "-B", str(AP_DIR / "hostapd.conf"), timeout=30)
    if res.returncode != 0:
        log.error("hostapd did not start: %s", (res.stdout + res.stderr).strip()[-300:])
        ap_down()
        return False
    run("sysctl", "-qw", "net.ipv4.ip_forward=1")
    res = run("dnsmasq", f"--conf-file={AP_DIR / 'dnsmasq.conf'}")
    if res.returncode != 0:
        log.error("dnsmasq did not start: %s", res.stderr.strip()[-300:])
        ap_down()
        return False
    return True


def ap_down() -> None:
    """Stop hosting and give wlan0 back to NetworkManager. Safe to call any time."""
    run("pkill", "-f", str(AP_DIR / "hostapd.conf"))
    run("pkill", "-f", str(AP_DIR / "dnsmasq.conf"))
    if run("nmcli", "-g", "GENERAL.STATE", "device", "show", WLAN).stdout.startswith("10 "):  # unmanaged
        run("ip", "addr", "flush", "dev", WLAN)
        run("nmcli", "device", "set", WLAN, "managed", "yes")


def rejoin_home(wait_s: int = 90) -> bool:
    """After ap_down(): wait for NetworkManager to get back on the home network."""
    start = time.time()
    nudged = False
    while time.time() - start < wait_s:
        if wlan_connection() == HOME_CON:
            return True
        time.sleep(5)
        if not nudged and time.time() - start > 20:   # autoconnect hasn't done it; ask
            run("nmcli", "connection", "up", HOME_CON, timeout=45)
            nudged = True
    return wlan_connection() == HOME_CON


def cleanup(flag: Path) -> None:
    """Back to normal: AP off, cellular off, rules gone. Safe to call any time."""
    ap_down()
    run("nmcli", "connection", "down", CELL_CON)
    nft_clear()
    flag.unlink(missing_ok=True)
    run("systemctl", "start", "riko-watch")


class Failover:
    def __init__(self, state_dir: Path, notifier: Notifier) -> None:
        self.flag = state_dir / "FAILOVER"
        self.disable = state_dir / "DISABLE_FAILOVER"
        self.usage_file = state_dir / "cellular_usage.json"
        self.n = notifier
        self.ssid = home_ssid()
        self.active = False
        self.capped = False
        self.down_since: float | None = None
        self.started = 0.0
        self.start_bytes = 0
        self.last_scan = 0.0
        self.seen = 0
        self.can_scan_as_ap = True

    # -- takeover
    def enter(self) -> bool:
        log.warning("home Wi-Fi %r gone for %ds — taking over", self.ssid, DOWN_BEFORE_TAKEOVER_S)
        if run("nmcli", "connection", "up", CELL_CON, timeout=120).returncode != 0:
            log.error("cellular did not come up; staying put")
            return False
        if run("ping", "-I", WWAN, "-c", "2", "-W", "5", "1.1.1.1").returncode != 0:
            log.error("cellular is up but has no internet; staying put")
            run("nmcli", "connection", "down", CELL_CON)
            return False
        if not nft_apply():
            run("nmcli", "connection", "down", CELL_CON)
            return False
        if not ap_up():
            cleanup(self.flag)
            return False
        self.flag.touch()
        run("systemctl", "stop", "riko-watch")
        self.active, self.capped, self.seen = True, False, 0
        self.started, self.start_bytes, self.last_scan = time.time(), wwan_bytes(), time.time()
        self.n.action_needed(
            "Home Wi-Fi down — feeder on cellular backup",
            f"{self.ssid} disappeared, so the Pi is now its hotspot and the feeder is "
            f"routed over cellular. It switches back by itself when the router returns.")
        return True

    def leave(self, why: str) -> None:
        used_mb = self._record_usage()
        mins = (time.time() - self.started) / 60
        log.warning("handing back (%s) after %.0f min, %.1f MB", why, mins, used_mb)
        ap_down()
        rejoined = rejoin_home()
        cleanup(self.flag)
        self.active, self.down_since = False, None
        if rejoined:
            self.n.fyi("Home Wi-Fi back — cellular backup off",
                       f"{why}. The takeover lasted {mins:.0f} min and used {used_mb:.1f} MB "
                       f"of cellular data.")
        else:
            log.warning("could not rejoin %s yet", HOME_CON)

    def _record_usage(self) -> float:
        used = max(0, wwan_bytes() - self.start_bytes)
        try:
            total = json.loads(self.usage_file.read_text())["total_bytes"]
        except (OSError, ValueError, KeyError):
            total = 0
        new_total = total + used
        self.usage_file.write_text(json.dumps({"total_bytes": new_total, "updated": time.time()}))
        for mb in USAGE_ALERT_MB:
            if total < mb * 1e6 <= new_total:
                self.n.action_needed("Cellular data used: over %d MB" % mb,
                                     f"Lifetime failover usage is now {new_total / 1e6:.0f} MB "
                                     f"of the SIM's 500 MB.")
        return used / 1e6

    # -- one tick
    def tick(self) -> None:
        now = time.time()
        if self.disable.exists():
            if self.active:
                self.leave("failover disabled by kill switch")
            self.down_since = None
            return
        if not self.active:
            if wlan_connection() == HOME_CON or ssid_visible_as_client(self.ssid):
                self.down_since = None      # connected, or the router is there and NM will rejoin
                return
            self.down_since = self.down_since or now
            if now - self.down_since >= DOWN_BEFORE_TAKEOVER_S and not self.enter():
                self.down_since = now       # failed to take over; wait a full window, try again
            return

        used_mb = max(0, wwan_bytes() - self.start_bytes) / 1e6
        if used_mb > EPISODE_CAP_MB and not self.capped:
            self.capped = True
            run("nmcli", "connection", "down", CELL_CON)
            self.n.action_needed(
                "Cellular backup cut off — data cap hit",
                f"This takeover used {used_mb:.0f} MB (cap {EPISODE_CAP_MB} MB), so cellular is "
                f"off to protect the SIM's allowance. The feeder is offline until the router "
                f"returns; its schedule still runs on the device.")

        interval = RETURN_SCAN_S if self.can_scan_as_ap else PEEK_S
        if now - self.last_scan < interval:
            return
        self.last_scan = now
        if self.can_scan_as_ap:
            visible = ssid_visible_as_ap(self.ssid)
            if visible is None:
                self.can_scan_as_ap = False
                log.warning("can't scan while hosting; will drop the hotspot every %ds to look", PEEK_S)
                return
            self.seen = self.seen + 1 if visible else 0
            if self.seen >= RETURN_CONFIRMATIONS:
                self.leave("the router is back")
        else:
            ap_down()
            time.sleep(8)
            if ssid_visible_as_client(self.ssid):
                self.leave("the router is back")
            elif not ap_up():
                self.leave("the hotspot would not restart")


def check() -> int:
    """Verify prerequisites without changing anything."""
    ok = True
    def need(label: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and good
        print(f"  [{'ok' if good else 'MISSING'}] {label}{' — ' + detail if detail else ''}")
    cons = run("nmcli", "-t", "-f", "NAME", "connection", "show").stdout.splitlines()
    for con in (HOME_CON, CELL_CON):
        need(f"connection {con}", con in cons)
    for name in ("hostapd.conf", "dnsmasq.conf", "accept"):
        need(f"{AP_DIR / name}", (AP_DIR / name).exists())
    need("hostapd", run("hostapd", "-v").returncode in (0, 1))
    need("dnsmasq", run("dnsmasq", "--version").returncode == 0)
    need("home SSID", bool(home_ssid()), home_ssid())
    need("feeder MAC (RIKO_FEEDER_MAC)", len(FEEDER_MAC) == 17, FEEDER_MAC)
    need("iw", run("iw", "--version").returncode == 0)
    if len(FEEDER_MAC) == 17:
        res = run("nft", "-c", "-f", "-", stdin=nft_ruleset())
        need("nftables rules parse", res.returncode == 0, res.stderr.strip()[:120])
    need(f"modem interface {WWAN}", Path(f"/sys/class/net/{WWAN}").exists())
    print("ready" if ok else "not ready")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Riko Wi-Fi → cellular failover")
    ap.add_argument("--check", action="store_true", help="verify prerequisites, change nothing")
    ap.add_argument("--cleanup", action="store_true", help="undo a takeover and exit")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if args.check:
        return check()
    cfg = load_config()
    flag = cfg.state_dir / "FAILOVER"
    if args.cleanup:
        cleanup(flag)
        return 0
    if len(FEEDER_MAC) != 17:
        log.error("RIKO_FEEDER_MAC is not set"); return 2
    cleanup(flag)   # never start half inside a takeover left by a crash or reboot
    fo = Failover(cfg.state_dir, Notifier(NotifyConfig.from_sources(cfg)))
    log.info("watching %r; feeder %s; takeover after %ds down", fo.ssid, FEEDER_MAC,
             DOWN_BEFORE_TAKEOVER_S)
    while True:
        try:
            fo.tick()
        except Exception:
            log.exception("tick failed")
        time.sleep(CHECK_S)


if __name__ == "__main__":
    sys.exit(main())
