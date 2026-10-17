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
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import ConfigError, load as load_config
from notify import Notifier, NotifyConfig
from riko import (ERROR_CODES, FeedCtrl, FeederState, FoodLevel, Riko, RikoStatus, Schedule,
                  WaterLevel)
from neakasa import Feeder as NeakasaFeeder
import power
import topup

log = logging.getLogger("riko.monitor")

# error codes we will NEVER auto-remediate — mechanical, a retry could make it worse
NEVER_RETRY = {10, 11, 13}          # grinder head missing / stalled / jammed
PUMP_STALL = 70                     # the one we do handle
BOWL_MISSING = 20                   # the other one we do handle: auto-resume once refilled
BOWL_MISSING_DEBOUNCE_POLLS = 1     # consecutive "bowl is in" reads required before resuming
FAILOVER_INTERVAL_S = 300           # poll this slowly while on cellular backup (wifi_failover.py)


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
    max_bowl_tare_fixes_per_day: int = 10  # beyond this, stop auto-fixing and escalate
    missed_feed_grace_min: float = 6.0      # minutes past the expected serve before "missed"
    offline_after_min: float = 15.0         # no device report for this long -> offline
    clock_check_every_s: float = 120.0      # how often to compare the feeder's clock with ours
    clock_tolerance_s: float = 10.0         # closer than this counts as in step
    clock_late_ok_s: float = 240.0          # a clock jump may land this far past a slot and still serve it
    # top-up (see topup.py): "on" rewrites the next meal, "observe" only logs what it
    # would do, "off" does nothing
    topup_mode: str = "observe"
    topup_lead_min: tuple[float, float] = (12.0, 16.0)   # decide this long before the slot
    topup_prep_lead_min: float = 10.5       # the feeder starts preparing 10 min before the slot
    topup_backstop_min: float = 30.0        # restore this long after the slot if no serving was seen
    stale_food_hours: float = 12.0          # alert when the bowl hasn't been emptied for this long
    # battery backup (power.py); power_guard.py takes over at its own critical voltage
    power_alert_after_s: float = 60.0       # on battery this long before "power is out"
    battery_low_v: float = 3.55             # "battery low" alert at or below this, on battery
    overdispense_alert_g: float = 8.0       # alert when a meal leaves this much more in the bowl than it should
    killswitch: Path = field(default_factory=lambda: Path("riko_state/DISABLE_REMEDIATION"))


# --------------------------------------------------------------------------- monitor
# --- ledger fail_reason decoders --------------------------------------------
# Pulled from the Android app's own source (FoodBinError.java,
# DeliveryFoodResultAndPlanAdapter.java, FoodBinWorkState.java), not guessed.
_ERR_CODE = {   # reason=2's resParam space (device errCode stream)
    3: "leftover over threshold", 5: "stopped manually", 11: "grinder abnormal",
    12: "cover not placed", 13: "food jammed", 20: "bowl not placed",
    21: "bowl retrieval failed", 22: "bowl dispensing blocked", 30: "battery absent",
    31: "DC power lost", 32: "battery low", 33: "battery temp too high",
    40: "water low", 41: "water empty", 50: "freeze-dried food low",
    51: "food empty", 60: "bowl overweight", 62: "weight calibration abnormal",
    70: "water pump abnormal", 71: "water pump abnormal",
}
_WORK_STATE = {  # reason=4's resParam space
    0: "Dormant", 1: "FoodPreparing", 2: "FoodDelivering", 3: "Pausing",
    4: "Faulting", 5: "Sleeping", 6: "Protecting", 7: "Cleaning",
}
_FIXED_REASON = {1: "stopped manually", 3: "leftover food over threshold",
                 5: "insufficient power"}


def decode_fail_reason(fail_reason_json: str) -> str:
    """Turn a raw ledger fail_reason JSON string into a human-readable line."""
    try:
        d = json.loads(fail_reason_json or "{}")
    except (ValueError, TypeError):
        return "unknown (unparseable fail_reason)"
    reason, param = d.get("reason"), d.get("resParam")
    if reason in _FIXED_REASON:
        return _FIXED_REASON[reason]
    if reason == 2:
        return f"device fault: {_ERR_CODE.get(param, f'errCode {param}')}"
    if reason == 4:
        state = _WORK_STATE.get(param, f"state {param}")
        return f"device busy (internal state stuck at '{state}')"
    return f"unrecognized (reason={reason}, resParam={param})"


# Feed slots are fixed; a ledger result only ever appears within minutes of one,
# so there's nothing to learn the rest of the day. Gate the (comparatively
# expensive) ledger poll to a window around each enabled slot, read live from the
# device schedule so it tracks edits automatically.
_LEDGER_WINDOW_MIN = 20  # +/- minutes around a scheduled slot to consider "near"


def _tod(t: float | None = None) -> int:
    """Seconds since local midnight, now or at unix time t."""
    lt = time.localtime(t)
    return lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec


def _near_a_feed_slot(st: RikoStatus, window_min: int = _LEDGER_WINDOW_MIN) -> bool:
    sched = Schedule.from_status(st)
    if sched is None:
        return True  # can't read the schedule -> fail open, check anyway
    now_sec = _tod()
    return any(abs(Schedule.seconds_since(sl.seconds_from_midnight, now_sec)) <= window_min * 60
               for sl in sched.slots if sl.enabled)


class Monitor:
    def __init__(self, riko: Riko, store: Store, notifier: Notifier,
                 policy: Policy, tz_offset: int, dry_run: bool,
                 neakasa: NeakasaFeeder | None = None,
                 feeder_owner_id: int | None = None,
                 device_name: str | None = None,
                 bowl_grams_target: int = 68,
                 status_path: Path | None = None) -> None:
        self.r = riko
        self.status_path = status_path        # optional: snapshot for oled_status.py
        self.store = store
        self.n = notifier
        self.p = policy
        self.tz_offset = tz_offset
        self.dry_run = dry_run
        self.neakasa = neakasa                # optional: enables check_ledger_failures
        self.feeder_owner_id = feeder_owner_id
        self.device_name = device_name
        self.bowl_grams_target = bowl_grams_target
        self._last_state: FeederState | None = None
        self._offline_since: float | None = None
        self._status_failures = 0    # consecutive failed status() calls
        self._on_battery_since: float | None = None
        self._power_alerted = False  # "power is out" has gone out for this outage
        self._battery_low_alerted = False
        self._scale_at_prep: float | None = None   # the scale's last report time, as of the latest prep
        self._clock_next_check = 0.0
        self._clock_syncs = 0              # syncs sent since the clock was last seen in step
        self._clock_stuck_alerted = False
        self._clock_held_for: str | None = None
        self._jam: dict | None = None      # the grinder fault in progress, if any
        self._in_pass = False              # inside check(): the ledger is fetched at most once
        self._pass_ledger: dict | None = None
        self._pass_ledger_failed = False
        self._warned_food = False
        self._warned_water = False
        self._bowl_back_count = 0    # consecutive polls with bowl detected, while suspended-for-bowl

    # ---- helpers
    def _remediation_allowed(self, need_grams: float, planned_today: float) -> tuple[bool, str]:
        if self.dry_run:
            return False, "dry-run"
        if self.p.killswitch.exists():
            return False, "kill switch set"
        if planned_today + need_grams > self.p.daily_gram_ceiling:
            return False, f"would exceed daily gram ceiling ({self.p.daily_gram_ceiling}g)"
        return True, ""

    # ---- the pass
    def _check_power(self) -> None:
        """Alert when the Pi itself is on its backup battery, when that battery gets
        low, and when wall power is back. Runs before anything that needs the network:
        in a power cut the router is usually down too, so a failed send is retried on
        every pass until it gets out (over cellular once wifi_failover.py has taken over)."""
        ups = power.read_ups()
        if ups is None:
            return                              # no UPS fitted
        now = time.time()
        if not ups.on_battery:
            if self._power_alerted:
                mins = (now - self._on_battery_since) / 60 if self._on_battery_since else 0
                if not self.n.fyi("Power is back — Pi on wall power again",
                                  f"The Pi ran on its battery for about {mins:.0f} min. The battery "
                                  f"is at {ups.volts:.2f} V (~{ups.percent}%) and recharging."):
                    return                      # couldn't send; say so next pass
            self._on_battery_since, self._power_alerted, self._battery_low_alerted = None, False, False
            return
        self._on_battery_since = self._on_battery_since or now
        if not self._power_alerted and now - self._on_battery_since >= self.p.power_alert_after_s:
            left = f" Roughly {ups.minutes_left} min of battery at the present draw." if ups.minutes_left else ""
            self._power_alerted = self.n.action_needed(
                "Power is out — Pi running on battery",
                f"The Pi lost wall power and is on its backup battery ({ups.volts:.2f} V, "
                f"~{ups.percent}%).{left} The feeder has its own battery, but it sleeps on "
                f"battery and may miss scheduled meals.")
        if self._power_alerted and not self._battery_low_alerted and ups.volts <= self.p.battery_low_v:
            self._battery_low_alerted = self.n.action_needed(
                "Pi battery low",
                f"The backup battery is down to {ups.volts:.2f} V (~{ups.percent}%). The Pi will "
                f"stop monitoring and protect its disk soon; it restarts when power returns.")

    async def check(self) -> None:
        self._in_pass, self._pass_ledger, self._pass_ledger_failed = True, None, False
        try:
            await self._run_checks()
        finally:
            self._in_pass = False

    async def _get_ledger(self) -> dict:
        """The intake ledger for the last two days. Within one check() pass it is fetched
        once and shared by every check that needs it; a failure is remembered for the
        rest of the pass too. Raises if it can't be read."""
        if self._in_pass and self._pass_ledger is not None:
            return self._pass_ledger
        if self._in_pass and self._pass_ledger_failed:
            raise RuntimeError("the ledger could not be read this pass")
        try:
            device = self.device_name or await self.neakasa.find_device()
            try:
                ledger = await self.neakasa.ledger(device, days=2, owner_user_id=self.feeder_owner_id)
            except RuntimeError as exc:
                # 1007 TokenInvalid = the shared session's REST token is stale
                # (e.g. resumed from an old session file): fresh login, one retry
                if "code=1007" not in str(exc):
                    raise
                log.info("ledger token rejected (1007); re-logging in")
                await self.r.relogin()
                ledger = await self.neakasa.ledger(device, days=2, owner_user_id=self.feeder_owner_id)
        except Exception:
            self._pass_ledger_failed = True
            raise
        if self._in_pass:
            self._pass_ledger = ledger
        return ledger

    async def _run_checks(self) -> None:
        try:
            self._check_power()
        except Exception as exc:
            log.warning("power check failed: %r", exc)
        try:
            st = await self.r.status()
        except Exception as exc:
            self._write_status_snapshot(error=repr(exc))
            await self._handle_unreachable(exc)
            return
        self._write_status_snapshot(raw=st.raw)
        self._offline_since = None
        self._status_failures = 0
        if st.state == FeederState.PREPARING:
            self._scale_at_prep = st.reported_at("bowlStatus") or 0.0

        self._check_levels(st)
        await self._check_state(st)
        await self._check_clock(st)
        try:
            await self._keep_clock(st)
        except Exception as exc:
            log.warning("clock upkeep failed: %r", exc)
        await self._check_missed_meal(st)
        await self._check_config_drift(st, self.bowl_grams_target)
        if self.neakasa is not None:
            await self.check_ledger_failures(st)
            await self._check_topup(st)

    # ---- top-up: shrink the next meal by what is still in the bowl ---------------
    def _topup_pending(self) -> dict | None:
        raw = self.store.get_setting("topup_pending")
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None

    def _topup_set_pending(self, pending: dict | None) -> None:
        self.store.put_setting("topup_pending", json.dumps(pending) if pending else "")

    async def _write_schedule(self, sched: Schedule) -> None:
        """Write the schedule and move the config-drift baseline with it, so our own
        change doesn't raise a "Setting changed: schedule" alert."""
        await self.r.set_schedule_raw(sched.raw)
        self.store.put_setting("schedule", _fmt([(s.hhmm, s.food_g, s.water_g, s.enabled) for s in sched.slots]))

    async def _bowl_now(self) -> float | None:
        """Grams of food in the bowl right now, from the live scale. None if the bowl is
        off the tray or the scale didn't answer — then the meal is left alone."""
        try:
            gram, bowl = await self.r.live_scale()
        except Exception as exc:
            log.info("top-up: couldn't read the scale: %r", exc)
            return None
        if gram < bowl / 2:
            return None                               # the bowl is off the tray
        return max(0.0, gram - bowl)

    async def _check_topup(self, st: RikoStatus) -> None:
        """Before each meal, serve only what the bowl is missing (see topup.py).

        About 15 minutes before a slot — the feeder starts preparing 10 minutes
        before — weigh the bowl with the live scale and rewrite that slot's food and
        water to the difference, or mark it skipped for today if the bowl already holds
        nearly a meal. Until the feeder starts preparing, keep weighing: if the cat eats
        in the meantime, change or undo the decision. Once the meal has been served (or
        30 minutes after the slot) the original amounts go back. The originals are saved
        before anything is written, so a restart mid-way still restores them.

        This used to read the bowl from the intake ledger, which logs eating in
        10-minute windows. On 2026-10-17 the cat ate between 07:35 and 07:45, but at
        07:39 the ledger still said 30 g and the 07:55 meal was skipped: he went two
        hours with nothing. The live scale sees eating as it happens.
        """
        if self.p.topup_mode == "off":
            return
        sched = Schedule.from_status(st)
        if sched is None:
            return
        pending = self._topup_pending()
        try:
            if pending:
                await self._topup_follow_up(st, sched, pending)
            else:
                await self._topup_decide(st, sched)
        except Exception as exc:
            log.warning("top-up check failed: %r", exc)

    @staticmethod
    def _topup_summary(grams: float, food: float, water: float, decision: "topup.Plan") -> str:
        return (f"{grams:.0f} g in the bowl, meal is {food:g}+{water:g} g -> " +
                (f"serve {decision.food_g}+{decision.water_g} g" if decision.action == "topup"
                 else decision.action))

    def _topup_notify(self, label: str, grams: float, food: float, water: float, decision: "topup.Plan") -> None:
        if decision.action == "skip":
            self.n.fyi(f"{label} meal skipped — bowl still full",
                       f"{grams:.0f} g is still in the bowl, nearly a full {food + water:g} g meal, "
                       f"so the {label} meal is skipped for today.")
        else:
            self.n.fyi(f"{label} meal topped up",
                       f"{grams:.0f} g is still in the bowl, so the {label} meal will be "
                       f"{decision.food_g} g food + {decision.water_g} g water instead of "
                       f"{food:g} + {water:g}. The schedule goes back to normal after it is served.")

    async def _topup_decide(self, st: RikoStatus, sched: Schedule) -> None:
        now_sec = _tod()
        lo, hi = (m * 60 for m in self.p.topup_lead_min)
        due = [(i, sl) for i, sl in sched.active_today()
               if lo <= (sl.seconds_from_midnight - now_sec) % 86400 <= hi]
        if not due:
            return
        i, slot = due[0]
        slot_sec, label = slot.seconds_from_midnight, slot.hhmm
        slot_epoch = int(time.time()) + (slot_sec - now_sec) % 86400
        if self.store.get_setting("topup_last_eval") == f"{_today()} {label}":
            return                                    # already decided for this slot
        if st.state != FeederState.IDLE:
            return                                    # asleep on battery, suspended, mid-meal
        grams = await self._bowl_now()
        self.store.put_setting("topup_last_eval", f"{_today()} {label}")
        if grams is None:
            msg = "no live reading (bowl off the tray, or the scale didn't answer); leaving the meal alone"
            self.store.record_remediation(label, "topup_observe", msg)
            log.info("top-up %s: %s", label, msg)
            return
        await self._stale_food_check(grams)
        food, water = sched.amounts(i)
        decision = topup.plan_topup(food, water, grams)
        summary = self._topup_summary(grams, food, water, decision)
        observe = self.p.topup_mode != "on" or self.dry_run or self.p.killswitch.exists()
        self.store.record_remediation(label, "topup_observe" if observe else "topup_decision", summary)
        if decision.action == "full":
            log.info("top-up %s: %s", label, summary)
            return
        if observe:
            log.info("top-up %s (observe only): %s", label, summary)
            return
        changed = (sched.with_skipped(i) if decision.action == "skip"
                   else sched.with_amounts(i, decision.food_g, decision.water_g))
        pending = {"slot_sec": slot_sec, "slot_epoch": slot_epoch, "label": label,
                   "action": decision.action, "orig": [food, water],
                   "new": [decision.food_g, decision.water_g], "leftover": grams,
                   "saw_cycle": False}
        self._topup_set_pending(pending)              # saved first: a crash after the write still restores
        try:
            await self._write_schedule(changed)
        except Exception:
            self._topup_set_pending(None)
            raise
        log.warning("top-up %s: %s", label, summary)
        self._topup_notify(label, grams, food, water, decision)

    async def _topup_revise(self, sched: Schedule, pending: dict, grams: float) -> None:
        """Before prep starts: the cat may have eaten since the decision. Undo it if the
        bowl is now empty enough for a full meal; change it if it is now a different
        top-up. A change needs the bowl to have moved by a few grams, so scale noise
        can't flip a decision back and forth."""
        orig = pending["orig"]
        want = topup.plan_topup(*orig, grams)
        if want.action == "full":
            await self._topup_restore(sched, pending, f"the bowl now has {grams:.0f} g")
            return
        same = want.action == pending["action"] and (want.action == "skip" or [want.food_g, want.water_g] == pending["new"])
        if same or abs(grams - pending["leftover"]) < 4:
            return
        i = sched.index_of(pending["slot_sec"])
        if i is None:
            return
        changed = sched.with_amounts(i, *orig).with_skipped(i, False)
        changed = (changed.with_skipped(i) if want.action == "skip"
                   else changed.with_amounts(i, want.food_g, want.water_g))
        before = self._topup_summary(pending["leftover"], *orig, topup.Plan(pending["action"], *pending["new"]))
        pending.update(action=want.action, new=[want.food_g, want.water_g], leftover=grams)
        self._topup_set_pending(pending)
        await self._write_schedule(changed)
        label = pending["label"]
        log.warning("top-up %s: changed (%s) to (%s)", label, before, self._topup_summary(grams, *orig, want))
        self.store.record_remediation(label, "topup_decision", "revised: " + self._topup_summary(grams, *orig, want))
        self._topup_notify(label, grams, *orig, want)

    async def _topup_follow_up(self, st: RikoStatus, sched: Schedule, pending: dict) -> None:
        now = time.time()
        slot, label = pending["slot_epoch"], pending["label"]
        if st.state in (FeederState.PREPARING, FeederState.SERVING):
            if not pending["saw_cycle"]:
                pending["saw_cycle"] = True
                self._topup_set_pending(pending)
            return                                    # never touch the schedule mid-meal
        if now < slot - self.p.topup_prep_lead_min * 60:
            grams = await self._bowl_now()
            if grams is None:                         # bowl off (likely swapped) or no reading: serve the meal
                await self._topup_restore(sched, pending, "no scale reading (bowl off the tray?)")
                return
            await self._topup_revise(sched, pending, grams)
            return
        served = pending["saw_cycle"] and now >= slot + 60
        if pending["action"] == "skip":
            served = now >= slot + 120                # nothing is served; the flag clears itself at midnight
        if not served and now < slot + self.p.topup_backstop_min * 60:
            return
        if st.state != FeederState.IDLE and now < slot + 2 * 3600:
            return                                    # suspended or faulted: wait for it to settle
        await self._topup_restore(sched, pending, "meal served" if pending["saw_cycle"] else "slot has passed")
        if pending["action"] == "skip":
            grams = await self._bowl_now()
            if grams is not None and grams <= topup.EMPTY_G:
                self.n.action_needed(
                    f"{label} meal was skipped, but the bowl is empty now",
                    f"The bowl held {pending['leftover']:.0f} g when the {label} meal was skipped and "
                    f"has {grams:.0f} g now. Consider a manual feed.")

    async def _topup_restore(self, sched: Schedule, pending: dict, why: str) -> None:
        label = pending["label"]
        i = sched.index_of(pending["slot_sec"])       # None: the slot was moved or deleted in the app
        result = "nothing to restore"
        try:
            if i is not None and pending["action"] == "topup":
                if sched.amounts(i) == pending["new"]:
                    await self._write_schedule(sched.with_amounts(i, *pending["orig"]))
                    result = "restored"
                else:
                    result = "left alone (edited in the app since)"
            elif i is not None and time.time() < pending["slot_epoch"]:
                if not sched.slots[i].day_enabled:    # un-skip: only before the slot, never after
                    await self._write_schedule(sched.with_skipped(i, False))
                    result = "un-skipped"
        except Exception as exc:
            log.warning("top-up %s: restore failed (%r); will retry", label, exc)
            if time.time() > pending["slot_epoch"] + 3 * 3600 and not pending.get("alerted"):
                pending["alerted"] = True
                self._topup_set_pending(pending)
                self.n.action_needed(
                    f"Couldn't restore the {label} meal",
                    f"It was changed to {pending['new'][0]}+{pending['new'][1]} g for a top-up and "
                    f"should be {pending['orig'][0]:g}+{pending['orig'][1]:g} g. Fix it in the app.")
            return
        self._topup_set_pending(None)
        self.store.record_remediation(label, "topup", "ok")
        log.info("top-up %s: %s — %s", label, result, why)

    async def _stale_food_check(self, grams: float) -> None:
        """Alert when food has sat in the bowl for stale_food_hours. "When was the bowl
        last empty" comes from the ledger; today's weight from the live scale."""
        if grams <= topup.EMPTY_G:
            return
        try:
            bowl = topup.bowl_now(await self._get_ledger())
        except Exception as exc:
            log.info("stale-food check: no ledger: %r", exc)
            return
        if bowl is None:
            return
        since = bowl.emptied_at
        hours = None if since is None else (time.time() - since) / 3600
        if hours is not None and hours < self.p.stale_food_hours:
            return
        key = str(since) if since is not None else _today()
        if self.store.get_setting("topup_stale_alerted") == key:
            return
        self.store.put_setting("topup_stale_alerted", key)
        age = "more than 2 days" if hours is None else f"{hours:.0f} hours"
        self.n.action_needed(
            "Food has been sitting in the bowl",
            f"The bowl has not been empty for {age} and holds {grams:.0f} g. "
            f"The feeder can't clear it — dump and rinse the bowl.")

    def _write_status_snapshot(self, raw: dict | None = None, error: str | None = None) -> None:
        """Publish the latest status for oled_status.py, so the display doesn't
        need its own login (a second login on the account knocks ours out)."""
        if self.status_path is None:
            return
        snap = {"ts": time.time(), "raw": raw, "error": error}
        tmp = self.status_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(snap))
            tmp.replace(self.status_path)
        except OSError as exc:
            log.warning("status snapshot write failed: %r", exc)

    async def _handle_unreachable(self, exc: Exception) -> None:
        now = time.time()
        self._status_failures += 1
        # %r, not %s: some SDK errors have an empty message (e.g. a timeout)
        log.warning("status failed (%d in a row): %r", self._status_failures, exc)
        # Riko._with_relogin only recovers from errors it recognizes. If failures
        # keep coming, assume the session is broken and force a fresh login: on the
        # 2nd failure in a row, then every 10th (~5 min at the default interval).
        n = self._status_failures
        if n == 2 or (n > 2 and n % 10 == 0):
            try:
                await self.r.relogin()
                log.info("forced re-login after %d status failures", n)
            except Exception as relogin_exc:
                log.warning("forced re-login failed: %r", relogin_exc)
        if self._offline_since is None:
            self._offline_since = now
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

        if st.state != FeederState.SUSPENDED or st.state_param not in NEVER_RETRY:
            self._jam = None               # that grinder fault is over
        if st.state == FeederState.SUSPENDED:
            await self._handle_suspension(st)
        elif st.state == FeederState.FAULT:
            self.n.action_needed("Feeder fault",
                                 f"Device in FAULT (param {st.state_param}). Needs a look.")

    async def _handle_grinder_fault(self, code: int, name: str) -> None:
        """Alert once per grinder fault (then every 10 minutes), never retry it, and for
        a jam watch the bowl with the live scale.

        The feeder grinds until its scale sees the target weight. When food backs up
        behind a jam the scale sees nothing, the grinder keeps going, and everything
        that built up can drop into the bowl at once - uncounted and dry. On
        2026-10-12 a jammed meal recorded as 0 g of food left about 56 g extra in the
        bowl, most likely when the meal was resumed. So: tell the owner to take the
        bowl off first, and say so if the bowl's weight jumps while it is stuck.
        """
        food = None
        try:
            gram, bowl = await self.r.live_scale()
            if gram > bowl / 2:                    # otherwise the bowl is off the tray
                food = gram - bowl
        except Exception as exc:
            log.warning("grinder fault: couldn't read the scale: %r", exc)
        now = time.time()
        if self._jam is None or self._jam["code"] != code:
            self._jam = {"code": code, "alerted_at": now, "base": food, "dumped": False}
            if code == 13:
                in_bowl = (f" The bowl holds about {max(0, food):.0f} g right now." if food is not None
                           else " The bowl is off the tray right now.")
                self.n.action_needed(
                    f"Feeder stuck: {name}",
                    "The meal is on hold and won't be retried automatically. Take the bowl off "
                    "BEFORE you resume or clear it: food that has built up behind a jam can drop "
                    f"all at once, dry and uncounted (about 56 g did on Oct 12).{in_bowl}")
            else:
                self.n.action_needed(f"Feeder stuck: {name}",
                                     "Mechanical fault — not auto-retrying. Please check the grinder.")
            return
        jam = self._jam
        if food is not None and jam["base"] is None:
            jam["base"] = food                     # the bowl is back on; measure from here
        if (food is not None and jam["base"] is not None and not jam["dumped"]
                and food - jam["base"] >= self.p.overdispense_alert_g):
            jam["dumped"] = True
            self.n.action_needed(
                f"About {food - jam['base']:.0f} g just dropped into the bowl",
                f"While the feeder was stuck ({name}) the bowl went from about {max(0, jam['base']):.0f} g "
                f"to {food:.0f} g. That is food from behind the jam, dry and not counted in the "
                f"meal record. Take the bowl off and remove it.")
        if now - jam["alerted_at"] >= 600:
            jam["alerted_at"] = now
            self.n.action_needed(f"Feeder still stuck: {name}",
                                 "It has been stuck for over 10 minutes. Take the bowl off before you "
                                 "resume or clear it.")

    async def _handle_bowl_missing_suspension(self, st: RikoStatus, name: str, slot: str) -> None:
        """Auto-resume a feed that's only paused because the bowl was missing.

        Unlike other suspensions, "bowl is now present" is unambiguous confirmation
        the blocking condition is gone — there's no real fault to diagnose, just a
        forgotten bowl. Debounced: requires BOWL_MISSING_DEBOUNCE_POLLS consecutive
        polls showing bowl_in=True before resuming, so a flickering sensor reading
        during placement doesn't trigger a premature/false resume. Still respects
        dry-run / kill switch / daily cap like every other auto-action.
        """
        if not st.bowl_in:
            if self._bowl_back_count:
                log.info("bowl missing again before debounce completed; resetting")
            self._bowl_back_count = 0
            self.n.action_needed(
                f"Feeder waiting on bowl: {name}",
                f"Slot {slot} is paused with no bowl on the tray. Put one back and "
                f"I'll resume it automatically — or resume it yourself in the app."
            )
            return

        self._bowl_back_count += 1
        if self._bowl_back_count < BOWL_MISSING_DEBOUNCE_POLLS:
            log.info("bowl detected (%d/%d polls), waiting to confirm before resuming",
                     self._bowl_back_count, BOWL_MISSING_DEBOUNCE_POLLS)
            return

        allowed, why = self._remediation_allowed(0.0, 0.0)  # resuming, not dispensing anew
        if not allowed:
            self.n.action_needed(
                "Bowl back, but not auto-resuming",
                f"Slot {slot} still paused ({name}). Bowl detected, but auto-resume is "
                f"held ({why}). Resume it yourself in the app."
            )
            return

        try:
            result = await self.r.feed_control(FeedCtrl.RESUME)
            self.store.record_remediation(slot, "bowl_resume", "ok")
            self.n.fyi("Feed auto-resumed",
                      f"Bowl was back on the tray for slot {slot} — resumed automatically. "
                      f"Device now {result.state.name if hasattr(result,'state') else result}.")
        except Exception as exc:
            self.store.record_remediation(slot, "bowl_resume", "failed")
            self.n.action_needed("Auto-resume failed",
                                 f"Bowl's back for slot {slot} but resuming it didn't take: "
                                 f"{exc}. Resume it yourself in the app.")
        finally:
            self._bowl_back_count = 0

    async def _handle_suspension(self, st: RikoStatus) -> None:
        code = st.state_param
        name = ERROR_CODES.get(code, f"code {code}")
        slot = _current_slot_label(st, self.tz_offset)

        if code in NEVER_RETRY:
            await self._handle_grinder_fault(code, name)
            return
        if code == BOWL_MISSING:
            await self._handle_bowl_missing_suspension(st, name, slot)
            return

        if code != PUMP_STALL:
            log.info("suspension code=%s (%s) has no auto-fix path", code, name)
            self.n.action_needed(f"Feeder suspended: {name}",
                                 f"param {code}. Not a known auto-fixable case.")
            return

        # pump stall
        log.info("pump stall detected: slot=%s state_param=%s", slot, code)

        if self.store.remediated_slot(slot, "unclog"):
            log.warning("slot %s already auto-recovered once today; stalled again, "
                        "not retrying (once-per-slot cap)", slot)
            self.n.action_needed("Pump stalled again",
                                 f"Already auto-recovered slot {slot} once today and it "
                                 f"stalled again. Manual attention: reseat the water tank.")
            return

        planned = _planned_food_today(st, self.tz_offset)
        allowed, why = self._remediation_allowed(8.0, planned)
        if not allowed:
            log.info("auto-unclog for slot %s held off: %s", slot, why)
            self.n.action_needed("Pump stalled - not auto-fixing",
                                 f"{name}. Holding off ({why}). Run `riko unclog` yourself "
                                 f"if the cat needs the meal.")
            return

        log.info("starting auto-unclog for slot %s (planned %.0fg food today so far)",
                 slot, planned)
        t0 = time.monotonic()
        journal: list[dict[str, Any]] = []
        try:
            result = await self.r.unclog(journal=journal)
            elapsed = time.monotonic() - t0

            # riko.py's unclog() already logs each step as it happens (its own `note()`
            # helper). No need to repeat that detail here — what's missing is a single
            # summary line tying the whole sequence together, since per-step lines are
            # easy to lose in the scroll when reviewing after the fact.
            step_names = " -> ".join(s.get("step", "?") for s in journal)
            self.store.record_remediation(slot, "unclog", "ok")
            log.info("auto-unclog for slot %s SUCCEEDED in %.1fs, device now %s "
                     "(%d steps: %s)", slot, elapsed, result.state.name, len(journal), step_names)
            self.n.fyi("Pump stall auto-cleared",
                       f"Primed and resumed slot {slot}. Device now {result.state.name}.")
        except Exception as exc:
            elapsed = time.monotonic() - t0
            self.store.record_remediation(slot, "unclog", "failed")
            got_to = journal[-1].get("step", "?") if journal else "before first step"
            log.error("auto-unclog for slot %s FAILED after %.1fs, got as far as "
                     "'%s' (%d steps completed): %s",
                     slot, elapsed, got_to, len(journal), exc, exc_info=True)
            self.n.action_needed("Auto-recovery failed",
                                 f"unclog on slot {slot} didn't take (got to '{got_to}'): "
                                 f"{exc}. Reseat the tank and feed manually.")

    async def _check_config_drift(self, st: RikoStatus, bowl_grams_target: int) -> None:
        """Notify when a device setting changes; auto-correct the bowl tare specifically.

        Added after the bowl tare silently reverted from 68 g back to the factory 65 g
        on 2026-09-09, coinciding with a Neakasa app update — and nothing told us. The
        correction had been in place for two days. An owner who fixes a setting should
        find out when something puts it back.

        Every OTHER watched setting stays notify-only by design — auto-reverting a
        schedule or freshness change would mean guessing which side "wins" without
        knowing why it changed. The bowl tare is the one exception: its correct value
        is known, stable, and configured (bowl_grams_target), the write is cheap and
        low-risk, and unlike a genuine user-made schedule edit, a self-inflicted revert
        is worth correcting automatically rather than waiting on a human to notice a
        push notification and go run a script. Rate-limited on top of the normal
        remediation policy — reverts are corrected every time, no daily cap.
        something is actively fighting us and we stop auto-fixing and escalate instead.

        Changes you make yourself will also notify (for every setting including bowl
        tare). That's intentional: a confirmation that an edit landed is useful, and it
        means an unexpected change stands out against a familiar pattern.
        """
        watched = {
            "bowl tare": _fmt(st.bowl_tare_g),
            "schedule": _fmt([(s.hhmm, s.food_g, s.water_g, s.enabled) for s in st.schedule]),
            "default feed": _fmt(st._p("defFdCfg", {})),
            "freshness": _fmt(st._p("freshMgrCfg", {})),
            "child lock": _fmt(st._p("childLockOnOff")),
            "food sound": _fmt(st._p("feedAudCfg", {})),
        }
        bowl_drifted = False
        for key, new in watched.items():
            old = self.store.get_setting(key)
            if old is None:
                self.store.put_setting(key, new)   # first run: establish the baseline
                continue
            if _same_setting(old, new):
                continue
            self.store.put_setting(key, new)
            self.store.record_setting_change(key, old, new)
            log.warning("setting changed: %s: %s -> %s", key, old, new)
            if key == "bowl tare":
                bowl_drifted = True
                continue  # handled below, with its own message (auto-fix attempt)
            self.n.action_needed(
                f"Setting changed: {key}",
                f"{key} went from {old} to {new}.\n\n"
                f"If that wasn't you, something else changed it — the app has been seen "
                f"reverting the bowl tare to the factory value after an update.")

        # Fire on the LIVE value being wrong, not just on a detected transition.
        # bowl_drifted (set above) only catches the moment it CHANGES from the last
        # baseline we stored — if the wrong value persists across polls (e.g. after
        # a restart re-baselines while already drifted, or after we declined to fix
        # due to the daily cap), old==new on every subsequent poll and bowl_drifted
        # never gets set again, so the sustained-wrong-value case was silently never
        # retried. Checking the live reading against the target directly, every
        # pass, closes that gap — this is what was actually broken today, not a
        # hang or stale read as it first appeared.
        if int(st.bowl_tare_g) != bowl_grams_target:
            await self._fix_bowl_tare_drift(bowl_grams_target)

    async def _fix_bowl_tare_drift(self, target_g: int) -> None:
        """Auto-correct the bowl tare, or explain once why we're not.

        Called on every pass where the live tare != target (see _check_config_drift).
        That's deliberate — it's what fixes the "stuck at the wrong value across many
        polls" case. But it means this function can be entered every ~30s for as long
        as the value stays wrong, so BOTH the "hit the daily cap" and the "policy says
        no" outcomes need their own re-notify guard, or they'd spam identically to how
        the cap message did before this fix (one real change, then the same escalation
        text every single poll thereafter — that's the bug this fixes).

        Guard: notify once per (reason, day), not once per poll. Re-notifies only if
        the reason changes (e.g. cap -> policy-denied) or a new day starts.
        """
        today = time.strftime("%Y-%m-%d")
        count_key = f"bowl_tare_fixes_{today}"
        done_today = int(self.store.get_setting(count_key) or 0)
        notified_key = f"bowl_tare_notified_{today}"
        already_notified = self.store.get_setting(notified_key)

        allowed, why = self._remediation_allowed(0.0, 0.0)
        if not allowed:
            if already_notified != f"policy:{why}":
                self.n.action_needed(
                    "Bowl tare reverted — not auto-fixing",
                    f"Held off ({why}). Run fix_bowl_weight.sh yourself if you want it "
                    f"corrected now. (Won't repeat this notice while it stays held off "
                    f"for the same reason.)"
                )
                self.store.put_setting(notified_key, f"policy:{why}")
            return

        # about to actually fix it -- clear the notify-guard so a FUTURE cap/policy
        # hit (after this fix, if it reverts yet again) notifies fresh rather than
        # being suppressed by a stale guard from earlier today.
        self.store.put_setting(notified_key, "")

        try:
            # NOTE: deliberately r.set_bowl_weight_property(), NOT r.tare(). tare()
            # calls resetScaleZero, which re-zeros the platform and — confirmed by
            # testing — knocks the device into believing the bowl was removed
            # (bowl_in=False, scale=0, food_in_bowl=None) with NO self-recovery; it
            # needs a physical lift-and-reseat to start reporting again, same as the
            # manual fixer script requires. set_bowl_weight_property() writes
            # bowlGram directly alongside the current bBowlIn/curWeight, matching
            # what the app itself appears to do on its own reverts — bowl stays
            # detected, no disturbance needed. Confirmed via direct A/B test.
            await self.r.set_bowl_weight_property(target_g)
            self.store.put_setting(count_key, str(done_today + 1))
            self.store.record_remediation("n/a", "bowl_tare_fix", "ok")
            self.n.fyi(
                "Bowl tare auto-corrected",
                f"It had reverted; set it back to {target_g} g automatically "
                f"(fix #{done_today + 1} today)."
            )
        except Exception as exc:
            self.store.record_remediation("n/a", "bowl_tare_fix", "failed")
            self.n.action_needed(
                "Bowl tare auto-fix failed",
                f"Tried to correct it back to {target_g} g and it didn't take: {exc}. "
                f"Run fix_bowl_weight.sh yourself."
            )

    async def check_ledger_failures(self, st: RikoStatus) -> None:
        """Poll the intake ledger, but only within ~20 min of a scheduled feed slot
        (see _near_a_feed_slot). Alerts once per newly-seen failed feed, decoded to
        plain English via decode_fail_reason. Remembers the high-water mark of
        feed_time we've already processed in the settings table, so restarts don't
        cause duplicate alerts.

        This is what added detection for `reason=4` ("device busy" / internal
        work-state stuck) after we found a scheduled feed silently rejected with no
        error anywhere else in the telemetry — see reason4-finding.md.
        """
        if not _near_a_feed_slot(st):
            log.debug("ledger check: not near a feed slot, skipping")
            return
        try:
            ledger = await self._get_ledger()
        except Exception as exc:
            log.warning("ledger check failed: %r", exc)
            return

        last_seen_raw = self.store.get_setting("ledger_last_feed_time")
        last_seen = int(last_seen_raw) if last_seen_raw else 0
        newest = last_seen
        new_failures = 0

        for f in ledger.get("feed_list", []):
            ft = f.get("feed_time", 0)
            if ft <= last_seen:
                continue
            newest = max(newest, ft)
            self._check_overdispense(ledger, f)   # failed meals too: a jam can dump uncounted food
            if f.get("status") == 1:
                continue  # succeeded
            new_failures += 1
            reason_str = decode_fail_reason(f.get("fail_reason", ""))
            when = time.strftime("%H:%M", time.localtime(ft))
            way = f.get("way", "?")
            plan = f"{f.get('plan_feed_weight')}g food / {f.get('plan_water_weight')}g water"
            got = f"{f.get('feed_weight')}g / {f.get('water_weight')}g"
            self.n.action_needed(
                f"Feed failed at {when}",
                f"{way} feed at {when} failed: {reason_str}\n"
                f"Planned {plan}, delivered {got}.\n"
                f"Check on the cat / consider a manual feed if this was scheduled."
            )
            log.warning("ledger: feed failure at %s way=%s %s", when, way, reason_str)

        if new_failures:
            log.info("ledger check: %d new failure(s) found and reported", new_failures)
        else:
            log.info("ledger check: ran near feed slot, no new failures")

        if newest > last_seen:
            self.store.put_setting("ledger_last_feed_time", str(newest))

    def _check_overdispense(self, ledger: dict, feed: dict) -> None:
        """Alert when a meal leaves more in the bowl than the leftover plus what the
        feeder says it served (topup.unexplained_extra). Seen 2026-10-11: ~12 g of dry
        food on top of a meal the feeder recorded as a normal 8 g + 56 g."""
        res = topup.unexplained_extra(ledger, feed)
        if res is None:
            return
        extra, before, prev_feed_at = res
        when = time.strftime("%H:%M", time.localtime(feed["feed_time"]))
        served = f"{feed.get('feed_weight', 0)} g food + {feed.get('water_weight', 0)} g water"
        log.info("meal %s: bowl %d g after serving; %.0f g already there + %s -> %+.0f g unexplained",
                 when, feed["left_weight"], before, served, extra)
        if extra < self.p.overdispense_alert_g:
            return
        if self._scale_at_prep is None or self._scale_at_prep > prev_feed_at + topup.HANDLED_GRACE_S:
            # the bowl was taken off or put back before this meal (or we weren't watching),
            # so "already there" can't be trusted — see topup.handled_since_meal
            log.info("meal %s: not alerting on the extra %.0f g; the bowl was handled before it", when, extra)
            return
        self.n.action_needed(
            f"{when} meal: about {extra:.0f} g too much in the bowl",
            f"The bowl reads {feed['left_weight']} g after serving. It should be about "
            f"{feed['left_weight'] - extra:.0f} g: {before:.0f} g already there plus {served}, which is "
            f"what the feeder recorded. When this happened before, the extra was dry food on top "
            f"of the meal. Check the bowl.")

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


    # ---- keeping the feeder's clock right ------------------------------------------
    def _sync_would_cost_a_meal(self, device_tod: float, drift: float, lead: float,
                                slots: list[int]) -> str | None:
        """Would correcting the clock right now skip or repeat a meal? Returns that
        meal's HH:MM if so.

        A sync moves the feeder's clock by `drift` seconds in one jump. The feeder
        starts a meal at (slot - lead) by its own clock. If its clock jumps from before
        that moment to more than a few minutes past the slot, it marks the meal Expired
        instead of serving it (2026-10-12: a 22.7-minute jump that landed 6 minutes
        after the 03:55 slot; on 2026-09-28 a jump that landed 5 minutes after did
        serve). A backward jump from after a meal's start to before its expiry could
        serve it twice.
        """
        landing = device_tod + drift
        for slot in slots:
            for day in (-86400, 0, 86400):
                start, expires = slot + day - lead, slot + day + self.p.clock_late_ok_s
                skips = drift > 0 and device_tod < start and landing > expires
                repeats = drift < 0 and device_tod >= start and landing < expires
                if skips or repeats:
                    return time.strftime("%H:%M", time.gmtime(slot))
        return None

    async def _keep_clock(self, st: RikoStatus) -> None:
        """Read the feeder's own clock and correct it as soon as it is wrong.

        The feeder's clock falls 20-30 minutes behind after even a short power
        interruption (5 min unplugged on 2026-09-27 -> 28.2 min slow; 3 min on
        2026-10-11 -> 22.7 min slow) and never corrects itself; every meal then runs
        that late. The feeder reports its clock on request (Riko.device_time), so
        every couple of minutes compare it with ours and sync when they differ.

        The one time not to sync is when the jump itself would cost a meal (see
        _sync_would_cost_a_meal). Then the feeder is left to run that meal on its own
        clock, and the sync happens right after.
        """
        now = time.time()
        if now < self._clock_next_check or st.state != FeederState.IDLE:
            return                              # never mid-meal; asleep on battery it can't answer
        self._clock_next_check = now + self.p.clock_check_every_s
        try:
            device = await self.r.device_time()
        except Exception as exc:
            log.info("clock: couldn't read the feeder's clock: %r", exc)
            return
        drift = now - device                    # + = the feeder is slow
        if abs(drift) < self.p.clock_tolerance_s:
            if self._clock_syncs:
                log.info("clock: the feeder is back in step")
            self._clock_syncs, self._clock_stuck_alerted, self._clock_held_for = 0, False, None
            return
        mins = abs(drift) / 60
        how = f"{mins:.1f} min {'slow' if drift > 0 else 'fast'}"
        if abs(drift) > 2 * 3600:
            # hours out is a timezone or DST setting, which _check_clock owns - not drift
            if self.store.get_setting("clock_tz_alerted") != _today():
                self.store.put_setting("clock_tz_alerted", _today())
                self.n.action_needed(
                    "Feeder clock is hours off",
                    f"The feeder's clock is {mins / 60:.1f} hours {'behind' if drift > 0 else 'ahead'}. That "
                    f"looks like a timezone setting, not drift, so it was left alone. Check it in the app.")
            return
        sched = Schedule.from_status(st)
        slots = [sl.seconds_from_midnight for _, sl in sched.active_today()] if sched else []
        lead = max(0, st.default_feed.soak_min) * 60
        meal = self._sync_would_cost_a_meal(_tod(device), drift, lead, slots)
        if meal:
            if self._clock_held_for != meal:
                self._clock_held_for = meal
                self.n.fyi(
                    f"Feeder clock is {how} — fixing it after the {meal} meal",
                    f"Correcting it right now would make the feeder skip or repeat the {meal} meal. "
                    f"So that meal will run about {mins:.0f} min {'late' if drift > 0 else 'early'}, "
                    f"and the clock will be synced straight after.")
            return
        allowed, why = self._remediation_allowed(0.0, 0.0)
        if not allowed:
            log.info("clock: the feeder is %s; would sync but %s", how, why)
            return
        if self._clock_syncs >= 2:              # two syncs didn't take: say so, then try every 30 min
            if not self._clock_stuck_alerted:
                self._clock_stuck_alerted = True
                self.n.action_needed(
                    "Feeder clock won't sync",
                    f"The feeder's clock is {how} and two syncs haven't fixed it. Meals will run that "
                    f"far off schedule. Trying again every 30 minutes.")
            self._clock_next_check = now + 1800
            await self.r.sync_time()
            return
        self._clock_syncs += 1
        await self.r.sync_time()
        self._clock_next_check = now + 30       # confirm it took
        if self._clock_syncs == 1:
            self.n.fyi(f"Feeder clock was {how} — synced",
                       f"The feeder's clock was {how}. It falls behind after a power interruption. "
                       f"It has been synced, so meals stay on schedule.")

    async def _check_missed_meal(self, st: RikoStatus) -> None:
        """Alert, once, when a meal is overdue with no record of it.

        A meal that fails normally leaves a failed ledger entry or a suspended state,
        and other checks own those. This is for the meal that never started at all:
        a slow clock (the feeder may still serve it late), or the feeder asleep on
        battery. A meal skipped for today in the app is expected to have no record.

        It only alerts. It used to sync the clock here, and on 2026-10-12 that sync,
        6 minutes after the 03:55 slot, jumped the feeder's clock past the meal and
        made it "Expired". _keep_clock now corrects the clock ahead of time.
        """
        if self.neakasa is None:
            return                              # needs the ledger to know a meal has no record
        sched = Schedule.from_status(st)
        if sched is None:
            return
        now_sec, grace = _tod(), self.p.missed_feed_grace_min * 60
        # earlier today only: just after midnight the skip flags have reset, so a meal
        # skipped late yesterday would look missed
        overdue = next((sl for _, sl in sched.active_today()
                        if grace < now_sec - sl.seconds_from_midnight < grace + 3600), None)
        if overdue is None:
            return
        try:
            ledger = await self._get_ledger()
        except Exception as exc:
            log.warning("missed-meal check: ledger fetch failed: %r", exc)
            return
        due_at = time.time() - (now_sec - overdue.seconds_from_midnight)
        if any(abs(f.get("feed_time", 0) - due_at) < grace + 300 for f in ledger.get("feed_list", [])):
            return                              # it has a record; other checks own that
        notified_key = f"meal_overdue_notified_{_today()}"
        if self.store.get_setting(notified_key) == overdue.hhmm:
            return
        self.store.put_setting(notified_key, overdue.hhmm)
        self.n.action_needed(
            f"{overdue.hhmm} meal hasn't run",
            f"The {overdue.hhmm} meal is more than {self.p.missed_feed_grace_min:.0f} minutes overdue "
            f"with no record of it. If the feeder's clock is behind it may still serve late by "
            f"itself; if the app shows it as Expired, feed manually.")

# ---- slot math (planned intake + which slot we're in) ----------------------
def _fmt(value: Any) -> str:
    """Stable string form of a setting, so trivial dict ordering doesn't look like drift."""
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    if isinstance(value, (list, tuple)):
        return json.dumps(value, separators=(",", ":"), default=str)
    return str(value)


def _same_setting(old: str, new: str) -> bool:
    """True if two _fmt strings mean the same value. The device reports the same
    number as 8 or 8.0 depending on who wrote it last (seen 2026-10-01 after an
    edit in the app), which is not a change worth an alert."""
    if old == new:
        return True
    try:
        return json.loads(old) == json.loads(new)
    except ValueError:
        return False


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
    policy = Policy(killswitch=cfg.state_dir / "DISABLE_REMEDIATION",
                    topup_mode=os.environ.get("RIKO_TOPUP", "observe").lower())

    async with Riko.from_config(cfg) as r:
        neakasa_client = None
        if getattr(cfg, "feeder_owner_id", 0):
            # optional: only enabled when a feeder_owner_id is configured. Shares
            # Riko's client and session — a second login on the same account
            # invalidates the first, and the two clients kicked each other out
            # every poll near feed slots (2026-09-29).
            neakasa_client = NeakasaFeeder(client=r.client)
        mon = Monitor(r, store, notifier, policy, cfg.tz_offset, args.dry_run,
                      neakasa=neakasa_client,
                      feeder_owner_id=getattr(cfg, "feeder_owner_id", None) or None,
                      device_name=r.device.device_name,
                      bowl_grams_target=cfg.bowl_grams,
                      status_path=cfg.state_dir / "status.json")
        if args.dry_run:
            log.info("DRY RUN — detection and notification only, no remediation")
        if args.once:
            await mon.check()
            return 0
        log.info("monitor running, %.0fs interval; top-up %s", args.interval,
                 policy.topup_mode if neakasa_client else "off (needs the ledger)")
        failover_flag = cfg.state_dir / "FAILOVER"   # set by wifi_failover.py during a takeover
        while True:
            try:
                await mon.check()
            except Exception:
                log.exception("check pass failed")
            # every poll over cellular spends the SIM's small data allowance
            on_cellular = failover_flag.exists()
            await asyncio.sleep(max(args.interval, FAILOVER_INTERVAL_S) if on_cellular
                                else args.interval)


if __name__ == "__main__":
    import sys
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
