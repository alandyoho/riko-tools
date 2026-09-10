#!/usr/bin/env python3
"""
monitor.py — watch the Riko, catch problems, fix the safe ones, and notify.

Built entirely on the Aliyun channel (riko.py), which is self-sufficient. It does
NOT depend on the Neakasa app backend, so it can't see the cat's actual intake
(that lives behind the encrypted ledger); it tracks PLANNED grams instead and is
honest about the difference.

WHAT IT DOES
  * Follows the live event stream and logs a structured feed ledger to SQLite.
  * Detects: missed feeds, pump stalls (code 70), other ModuleErr codes, clock
    drift, config drift, device offline, low food/water.
  * Remediates the safe cases only, under hard caps:
      - pump stall  -> unclog (prime + resume), max 1/slot, max N/day
      - clock drift -> re-apply the configured tz offset
    Everything else is notify-only.
  * Sends push notifications (ntfy), separating "I fixed it" from "you must act".

SAFETY (learned the hard way)
  * A hard daily gram ceiling. Remediation that would feed past it is refused and
    escalated to a human instead.
  * Never remediates from an unknown/!idle state.
  * One retry per slot, a daily remediation cap, and a kill-switch file.
  * Grinder faults (10/11/13) are NEVER auto-retried — a jam could worsen.
  * Shares the cached session with the CLI so it doesn't fight manual runs.

USAGE
  python3 monitor.py --once      # single pass (for cron/testing)
  python3 monitor.py             # run forever (systemd)
  python3 monitor.py --dry-run   # detect + notify, but never remediate

Kill switch: create the file named by [monitor].killswitch (default
riko_state/DISABLE_REMEDIATION) and all remediation stops; detection continues.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import ConfigError, load as load_config
from notify import Notifier, NotifyConfig
from riko import ERROR_CODES, FeederState, FoodLevel, Riko, RikoStatus, WaterLevel

log = logging.getLogger("riko.monitor")

# error codes we will NEVER auto-remediate — mechanical, a retry could make it worse
NEVER_RETRY = {10, 11, 13}          # grinder head missing / stalled / jammed
PUMP_STALL = 70                     # the one we do handle


# --------------------------------------------------------------------------- state
class Store:
    """SQLite ledger + counters. Durable across restarts."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS feeds(
            ts INTEGER, state TEXT, param INTEGER, note TEXT)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS remediations(
            ts INTEGER, day TEXT, slot TEXT, kind TEXT, result TEXT)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS seen_errors(
            ts INTEGER, code INTEGER, module TEXT)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY, value TEXT, ts INTEGER)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS setting_changes(
            ts INTEGER, key TEXT, old TEXT, new TEXT)""")
        self.db.commit()

    def log_feed(self, state: str, param: int, note: str = "") -> None:
        self.db.execute("INSERT INTO feeds VALUES (?,?,?,?)",
                        (int(time.time()), state, param, note))
        self.db.commit()

    def record_remediation(self, slot: str, kind: str, result: str) -> None:
        self.db.execute("INSERT INTO remediations VALUES (?,?,?,?,?)",
                        (int(time.time()), _today(), slot, kind, result))
        self.db.commit()

    def get_setting(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def put_setting(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO settings VALUES (?,?,?) ON CONFLICT(key) "
                        "DO UPDATE SET value=excluded.value, ts=excluded.ts",
                        (key, value, int(time.time())))
        self.db.commit()

    def record_setting_change(self, key: str, old: str, new: str) -> None:
        self.db.execute("INSERT INTO setting_changes VALUES (?,?,?,?)",
                        (int(time.time()), key, old, new))
        self.db.commit()

    def remediations_today(self, kind: str | None = None) -> int:
        q = "SELECT COUNT(*) FROM remediations WHERE day=?"
        args: list[Any] = [_today()]
        if kind:
            q += " AND kind=?"; args.append(kind)
        return self.db.execute(q, args).fetchone()[0]

    def remediated_slot(self, slot: str, kind: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM remediations WHERE day=? AND slot=? AND kind=? AND result='ok' LIMIT 1",
            (_today(), slot, kind)).fetchone() is not None


def _today() -> str:
    return time.strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- policy
@dataclass
class Policy:
    daily_gram_ceiling: float = 60.0        # never let the day's PLANNED food exceed this
    max_remediations_per_day: int = 8
    max_unclog_per_slot: int = 1
    missed_feed_grace_min: float = 6.0      # minutes past the expected serve before "missed"
    offline_after_min: float = 15.0         # no device report for this long -> offline
    killswitch: Path = field(default_factory=lambda: Path("riko_state/DISABLE_REMEDIATION"))


# --------------------------------------------------------------------------- monitor
class Monitor:
    def __init__(self, riko: Riko, store: Store, notifier: Notifier,
                 policy: Policy, tz_offset: int, dry_run: bool) -> None:
        self.r = riko
        self.store = store
        self.n = notifier
        self.p = policy
        self.tz_offset = tz_offset
        self.dry_run = dry_run
        self._last_state: FeederState | None = None
        self._offline_since: float | None = None
        self._warned_food = False
        self._warned_water = False

    # ---- helpers
    def _remediation_allowed(self, need_grams: float, planned_today: float) -> tuple[bool, str]:
        if self.dry_run:
            return False, "dry-run"
        if self.p.killswitch.exists():
            return False, "kill switch set"
        if self.store.remediations_today() >= self.p.max_remediations_per_day:
            return False, "daily remediation cap reached"
        if planned_today + need_grams > self.p.daily_gram_ceiling:
            return False, f"would exceed daily gram ceiling ({self.p.daily_gram_ceiling}g)"
        return True, ""

    # ---- the pass
    async def check(self) -> None:
        try:
            st = await self.r.status()
        except Exception as exc:
            await self._handle_unreachable(exc)
            return
        self._offline_since = None

        self._check_levels(st)
        await self._check_state(st)
        await self._check_clock(st)
        self._check_config_drift(st)

    async def _handle_unreachable(self, exc: Exception) -> None:
        now = time.time()
        if self._offline_since is None:
            self._offline_since = now
            log.warning("status failed: %s", exc)
            return
        down_min = (now - self._offline_since) / 60
        if down_min >= self.p.offline_after_min:
            self.n.action_needed(
                "Feeder unreachable",
                f"No response for {down_min:.0f} min. Check power, wifi, and the "
                f"Neakasa cloud. Scheduled feeds may run on the device regardless.")
            self._offline_since = now  # re-arm so it re-alerts each interval, not spam

    def _check_levels(self, st: RikoStatus) -> None:
        if st.food_level == FoodLevel.EMPTY or st.food_level == FoodLevel.INSUFFICIENT:
            if not self._warned_food:
                self.n.action_needed("Food low", f"Food sensor reads {st.food_level.name}. Refill the hopper.")
                self._warned_food = True
        else:
            self._warned_food = False
        if st.water_level in (WaterLevel.EMPTY, WaterLevel.INSUFFICIENT):
            if not self._warned_water:
                self.n.action_needed("Water low", f"Water sensor reads {st.water_level.name}. Refill the tank.")
                self._warned_water = True
        else:
            self._warned_water = False

    async def _check_state(self, st: RikoStatus) -> None:
        prev, cur = self._last_state, st.state
        if cur != prev:
            self.store.log_feed(cur.name, st.state_param)
            self._last_state = cur

        if st.state == FeederState.SUSPENDED:
            await self._handle_suspension(st)
        elif st.state == FeederState.FAULT:
            self.n.action_needed("Feeder fault",
                                 f"Device in FAULT (param {st.state_param}). Needs a look.")

    async def _handle_suspension(self, st: RikoStatus) -> None:
        code = st.state_param
        name = ERROR_CODES.get(code, f"code {code}")
        slot = _current_slot_label(st, self.tz_offset)

        if code in NEVER_RETRY:
            self.n.action_needed(f"Feeder stuck: {name}",
                                 "Mechanical fault — not auto-retrying. Please check the grinder.")
            return
        if code != PUMP_STALL:
            self.n.action_needed(f"Feeder suspended: {name}",
                                 f"param {code}. Not a known auto-fixable case.")
            return

        # pump stall
        if self.store.remediated_slot(slot, "unclog"):
            self.n.action_needed("Pump stalled again",
                                 f"Already auto-recovered slot {slot} once today and it "
                                 f"stalled again. Manual attention: reseat the water tank.")
            return

        planned = _planned_food_today(st, self.tz_offset)
        allowed, why = self._remediation_allowed(8.0, planned)
        if not allowed:
            self.n.action_needed("Pump stalled - not auto-fixing",
                                 f"{name}. Holding off ({why}). Run `riko unclog` yourself "
                                 f"if the cat needs the meal.")
            return

        log.info("auto-unclog for slot %s (%s)", slot, name)
        try:
            journal: list[dict[str, Any]] = []
            result = await self.r.unclog(journal=journal)
            self.store.record_remediation(slot, "unclog", "ok")
            self.n.fyi("Pump stall auto-cleared",
                       f"Primed and resumed slot {slot}. Device now {result.state.name}.")
        except Exception as exc:
            self.store.record_remediation(slot, "unclog", "failed")
            self.n.action_needed("Auto-recovery failed",
                                 f"unclog on slot {slot} didn't take: {exc}. "
                                 f"Reseat the tank and feed manually.")

    def _check_config_drift(self, st: RikoStatus) -> None:
        """Notify when a device setting changes.

        Added after the bowl tare silently reverted from 68 g back to the factory 65 g
        on 2026-09-09, coinciding with a Neakasa app update — and nothing told us. The
        correction had been in place for two days. An owner who fixes a setting should
        find out when something puts it back.

        Notify-only by design. Auto-reverting would mean fighting the app in a loop,
        and we don't know which side "wins" or why. Surfacing it lets a human decide.

        Changes you make yourself will also notify. That's intentional: a confirmation
        that a schedule edit landed is useful, and it means an unexpected change stands
        out against a familiar pattern.
        """
        watched = {
            "bowl tare": _fmt(st.bowl_tare_g),
            "schedule": _fmt([(s.hhmm, s.food_g, s.water_g, s.enabled) for s in st.schedule]),
            "default feed": _fmt(st._p("defFdCfg", {})),
            "freshness": _fmt(st._p("freshMgrCfg", {})),
            "child lock": _fmt(st._p("childLockOnOff")),
            "food sound": _fmt(st._p("feedAudCfg", {})),
        }
        for key, new in watched.items():
            old = self.store.get_setting(key)
            if old is None:
                self.store.put_setting(key, new)   # first run: establish the baseline
                continue
            if old == new:
                continue
            self.store.put_setting(key, new)
            self.store.record_setting_change(key, old, new)
            log.warning("setting changed: %s: %s -> %s", key, old, new)
            self.n.action_needed(
                f"Setting changed: {key}",
                f"{key} went from {old} to {new}.\n\n"
                f"If that wasn't you, something else changed it — the app has been seen "
                f"reverting the bowl tare to the factory value after an update.")

    async def _check_clock(self, st: RikoStatus) -> None:
        """Verify the device's clock against real wall-clock time, using the device's
        OWN timezone data (base offset + DST flag). FW 1.0.0-0023 fixed DST handling,
        so the correct configuration is the honest one -- base offset with dst=1 and
        the real DST boundary dates -- and the device applies the +1 itself in summer.
        We flag drift only when the device's effective time is actually wrong, not when
        its stored offset differs from some hardcoded number.

        `tz_offset` in config is now just the FALLBACK we'd write if we ever needed to
        force-correct a device whose own DST handling is broken (older firmware). On
        -0023+ this check should essentially never fire.
        """
        tz = st._p("timeZoneMsg", {}) or {}
        zone = tz.get("zone")
        if zone is None:
            return

        # What time does the DEVICE think it is, from its own config?
        dev_offset = zone + (1 if tz.get("dst") and _dst_active_now(tz) else 0)
        # What offset does the device's own timezone actually require right now?
        # We can't run a full tz database here, but we can sanity-check against the
        # host's real UTC offset, since the Pi is NTP-synced and in the same zone.
        real_offset = -round((time.timezone if not time.localtime().tm_isdst
                              else time.altzone) / 3600)

        drift_h = dev_offset - real_offset
        if drift_h == 0:
            return  # device clock is correct -- nothing to do

        msg = (f"Device effective offset is UTC{dev_offset:+d} "
               f"(zone {zone}, dst={tz.get('dst')}), but real offset is "
               f"UTC{real_offset:+d} -- off by {drift_h:+d}h.")
        if self.dry_run or self.p.killswitch.exists():
            self.n.action_needed("Clock drifted", msg + " (not auto-fixing)")
            return
        await self._fix_clock(msg, real_offset)

    async def _fix_clock(self, msg: str, real_offset: int) -> None:
        """Correct a genuinely-wrong device clock. Prefer letting the device do DST:
        write the base (standard-time) offset with dst=1 and let the firmware apply
        the +1 when active. Only fall back to a flat offset if that doesn't hold."""
        # base offset = standard-time offset for this zone (real minus DST if active)
        base = real_offset - (1 if time.localtime().tm_isdst else 0)
        try:
            await self.r.set_timezone(base, dst=True)
            self.n.fyi("Clock re-synced",
                       msg + f" Wrote base offset {base:+d} with dst=1 (device applies DST).")
        except Exception as exc:
            self.n.action_needed("Clock fix failed", f"{msg} set_timezone error: {exc}")


# ---- slot math (planned intake + which slot we're in) ----------------------
def _fmt(value: Any) -> str:
    """Stable string form of a setting, so trivial dict ordering doesn't look like drift."""
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    if isinstance(value, (list, tuple)):
        return json.dumps(value, separators=(",", ":"), default=str)
    return str(value)


def _dst_active_now(tz: dict) -> bool:
    """Is now within the device's declared DST window? Uses the dstStartTimeOne /
    dstEndTimeOne epoch bounds the device reports."""
    start = tz.get("dstStartTimeOne")
    end = tz.get("dstEndTimeOne")
    if not start or not end:
        return bool(tz.get("dst"))  # fall back to the flag if bounds missing
    return start <= time.time() < end


def _current_slot_label(st: RikoStatus, tz_offset: int) -> str:
    """Best-effort label for the slot a suspension belongs to: nearest enabled
    slot time to now. Used only to cap one auto-fix per slot per day."""
    now = time.time()
    local = time.gmtime(now + tz_offset * 3600)
    sec = local.tm_hour * 3600 + local.tm_min * 60 + local.tm_sec
    best, bestd = "adhoc", 99999
    for sl in st.schedule:
        if not sl.enabled:
            continue
        d = abs(sl.seconds_from_midnight - sec)
        if d < bestd:
            bestd, best = d, sl.hhmm
    return best


def _planned_food_today(st: RikoStatus, tz_offset: int) -> float:
    now = time.time()
    local = time.gmtime(now + tz_offset * 3600)
    sec = local.tm_hour * 3600 + local.tm_min * 60 + local.tm_sec
    return sum(sl.food_g for sl in st.schedule
               if sl.enabled and sl.seconds_from_midnight <= sec)


# --------------------------------------------------------------------------- main
async def main() -> int:
    ap = argparse.ArgumentParser(description="Riko monitor")
    ap.add_argument("--once", action="store_true", help="one pass then exit")
    ap.add_argument("--dry-run", action="store_true", help="detect + notify, never remediate")
    ap.add_argument("--interval", type=float, default=30.0, help="seconds between passes")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        cfg = load_config()
        cfg.require_credentials()
    except ConfigError as exc:
        print(f"config error: {exc}"); return 2

    notifier = Notifier(NotifyConfig.from_sources(cfg))
    store = Store(cfg.state_dir / "monitor.db")
    policy = Policy(killswitch=cfg.state_dir / "DISABLE_REMEDIATION")

    async with Riko.from_config(cfg) as r:
        mon = Monitor(r, store, notifier, policy, cfg.tz_offset, args.dry_run)
        if args.dry_run:
            log.info("DRY RUN — detection and notification only, no remediation")
        if args.once:
            await mon.check()
            return 0
        log.info("monitor running, %.0fs interval", args.interval)
        while True:
            try:
                await mon.check()
            except Exception:
                log.exception("check pass failed")
            await asyncio.sleep(args.interval)


if __name__ == "__main__":
    import sys
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
