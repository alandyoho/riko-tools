#!/usr/bin/env python3
"""
power_guard.py — protect the Pi when its battery backup is about to run out.

The UPS keeps the Pi up through a power cut, but only for an hour or two. A normal
shutdown is the wrong answer: the UPS keeps feeding the halted Pi, so when wall
power returns nothing wakes it and it stays off until someone power-cycles it.

So at a critically low battery this puts the Pi into SAFE IDLE instead:
  * stops the riko services and the cellular link (less draw, nothing mid-write);
  * syncs and remounts every filesystem read-only, so the battery cutting out
    can't corrupt the SD card;
  * keeps running, watching the UPS. When the charger is back for 30 seconds it
    reboots the Pi, and everything starts normally.
If the battery does cut out first, the Pi simply boots when power returns.

Runs as root (stops services, uses /proc/sysrq-trigger). Install with
power-setup.sh. The monitor (monitor.py) sends the "power is out" and "battery
low" alerts; this only acts at the last step.

  python3 power_guard.py            # run forever (systemd)
  python3 power_guard.py --check    # print a reading and the thresholds, change nothing

RIKO_POWER_CRITICAL_V overrides the safe-idle voltage (set it high to rehearse).
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

import power
from config import load as load_config
from notify import Notifier, NotifyConfig

log = logging.getLogger("riko.power")

CRITICAL_V = float(os.environ.get("RIKO_POWER_CRITICAL_V", "3.40"))   # safe idle at or below this
POLL_S = 10
IDLE_POLL_S = 5
READS_TO_TRIGGER = 3          # consecutive low readings (30 s) before acting
CHARGING_READS_TO_REBOOT = 6  # consecutive charging readings (30 s) before rebooting
STOP_SERVICES = ("riko-monitor", "riko-watch", "riko-failover")
CELL_CON = os.environ.get("RIKO_CELL_CON", "cellular-1nce")


def run(*cmd: str, timeout: int = 60) -> None:
    try:
        subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.warning("%s: %r", " ".join(cmd), exc)


def sysrq(key: str) -> None:
    Path("/proc/sysrq-trigger").write_text(key)


def enter_safe_idle(flag: Path, notifier: Notifier, ups: power.Ups) -> None:
    log.warning("battery at %.2f V on battery power — entering safe idle", ups.volts)
    notifier.action_needed(
        "Pi battery nearly empty — going quiet",
        f"The backup battery is at {ups.volts:.2f} V, so the Pi has stopped monitoring and made "
        f"its disk read-only. It restarts by itself when power returns. The feeder's own "
        f"schedule is not affected.")
    try:
        flag.touch()                       # the display reads this
    except OSError:
        pass
    for svc in STOP_SERVICES:
        run("systemctl", "stop", svc)
    run("nmcli", "connection", "down", CELL_CON)
    os.sync()
    sysrq("u")                             # remount every filesystem read-only


def reboot() -> None:
    log.warning("charger is back — rebooting")
    os.sync()
    sysrq("b")


class Guard:
    def __init__(self, flag: Path, notifier: Notifier, read=power.read_ups) -> None:
        self.flag, self.n, self.read = flag, notifier, read
        self.idle = False
        self.low = deque(maxlen=READS_TO_TRIGGER)
        self.charging_reads = 0

    def tick(self) -> None:
        ups = self.read()
        if ups is None:
            self.low.clear()
            self.charging_reads = 0
            return
        if self.idle:
            self.charging_reads = self.charging_reads + 1 if ups.charging else 0
            if self.charging_reads >= CHARGING_READS_TO_REBOOT:
                reboot()
            return
        self.low.append(ups.on_battery and ups.volts <= CRITICAL_V)
        if len(self.low) == READS_TO_TRIGGER and all(self.low):
            enter_safe_idle(self.flag, self.n, ups)
            self.idle = True


def main() -> int:
    ap = argparse.ArgumentParser(description="Riko Pi battery guard")
    ap.add_argument("--check", action="store_true", help="print a reading, change nothing")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if args.check:
        ups = power.read_ups()
        if ups is None:
            print("no UPS found"); return 1
        print(f"UPS: {ups.volts:.3f} V, {ups.ma:+.0f} mA, ~{ups.percent}% "
              f"({'on battery' if ups.on_battery else 'on wall power'}); safe idle at {CRITICAL_V:.2f} V")
        return 0
    cfg = load_config()
    flag = cfg.state_dir / "SAFE_IDLE"
    flag.unlink(missing_ok=True)           # a fresh boot is never in safe idle
    guard = Guard(flag, Notifier(NotifyConfig.from_sources(cfg)))
    log.info("watching the UPS; safe idle at %.2f V on battery", CRITICAL_V)
    while True:
        try:
            guard.tick()
        except Exception:
            log.exception("tick failed")
        time.sleep(IDLE_POLL_S if guard.idle else POLL_S)


if __name__ == "__main__":
    sys.exit(main())
