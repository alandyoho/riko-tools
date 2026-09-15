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
from riko import ERROR_CODES, FeedCtrl, FeederState, FoodLevel, Riko, RikoStatus, WaterLevel
from neakasa import Feeder as NeakasaFeeder

log = logging.getLogger("riko.monitor")

# error codes we will NEVER auto-remediate — mechanical, a retry could make it worse
NEVER_RETRY = {10, 11, 13}          # grinder head missing / stalled / jammed
PUMP_STALL = 70                     # the one we do handle
BOWL_MISSING = 20                   # the other one we do handle: auto-resume once refilled
BOWL_MISSING_DEBOUNCE_POLLS = 1     # consecutive "bowl is in" reads required before resuming


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


def _near_a_feed_slot(st: RikoStatus, window_min: int = _LEDGER_WINDOW_MIN) -> bool:
    plan = st._p("fdPlanStr")
    if not plan:
        return True  # can't read the schedule -> fail open, check anyway
    try:
        plan = json.loads(plan) if isinstance(plan, str) else plan
    except (ValueError, TypeError):
        return True
    now = time.localtime()
    now_sec = now.tm_hour * 3600 + now.tm_min * 60 + now.tm_sec
    for slot_sec, enabled in zip(plan.get("time", []), plan.get("bEn", [])):
        if not enabled:
            continue
        delta = abs(now_sec - slot_sec)
        delta = min(delta, 86400 - delta)  # wrap across midnight
        if delta <= window_min * 60:
            return True
    return False


class Monitor:
    def __init__(self, riko: Riko, store: Store, notifier: Notifier,
                 policy: Policy, tz_offset: int, dry_run: bool,
                 neakasa: NeakasaFeeder | None = None,
                 feeder_owner_id: int | None = None,
                 device_name: str | None = None,
                 bowl_grams_target: int = 68) -> None:
        self.r = riko
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
        self._warned_food = False
        self._warned_water = False
        self._bowl_back_count = 0    # consecutive polls with bowl detected, while suspended-for-bowl

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
        await self._check_config_drift(st, self.bowl_grams_target)
        if self.neakasa is not None:
            await self.check_ledger_failures(st)

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
            self.n.action_needed(f"Feeder stuck: {name}",
                                 "Mechanical fault — not auto-retrying. Please check the grinder.")
            return
        if code == BOWL_MISSING:
            await self._handle_bowl_missing_suspension(st, name, slot)
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
        remediation policy — if it reverts more than max_bowl_tare_fixes_per_day times,
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
            if old == new:
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

        if done_today >= self.p.max_bowl_tare_fixes_per_day:
            if already_notified != "cap":
                self.n.action_needed(
                    "Bowl tare keeps reverting — not auto-fixing again",
                    f"Reverted and been auto-corrected {done_today} time(s) today already. "
                    f"Something is actively resetting it (likely the Neakasa app syncing). "
                    f"Fix it yourself with fix_bowl_weight.sh once you're done poking at the "
                    f"app today, or it'll probably just revert again.\n\n"
                    f"(You won't be re-notified about this again today unless it's fixed "
                    f"and reverts yet again.)"
                )
                self.store.put_setting(notified_key, "cap")
            return

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
                f"({done_today + 1}/{self.p.max_bowl_tare_fixes_per_day} today)."
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
            device = self.device_name or await self.neakasa.find_device()
            ledger = await self.neakasa.ledger(device, days=1,
                                               owner_user_id=self.feeder_owner_id)
        except Exception as exc:
            log.warning("ledger check failed: %s", exc)
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

    neakasa_client = None
    if getattr(cfg, "feeder_owner_id", 0):
        # optional: only enabled when a feeder_owner_id is configured. Uses the
        # same [account] credentials as everything else — no second login needed.
        neakasa_client = NeakasaFeeder(cfg.email, cfg.password)
        await neakasa_client.__aenter__()

    async with Riko.from_config(cfg) as r:
        mon = Monitor(r, store, notifier, policy, cfg.tz_offset, args.dry_run,
                      neakasa=neakasa_client,
                      feeder_owner_id=getattr(cfg, "feeder_owner_id", None) or None,
                      bowl_grams_target=cfg.bowl_grams)
        if args.dry_run:
            log.info("DRY RUN — detection and notification only, no remediation")
        if args.once:
            await mon.check()
            return 0
        log.info("monitor running, %.0fs interval", args.interval)
        try:
            while True:
                try:
                    await mon.check()
                except Exception:
                    log.exception("check pass failed")
                await asyncio.sleep(args.interval)
        finally:
            if neakasa_client is not None:
                await neakasa_client.__aexit__(None, None, None)


if __name__ == "__main__":
    import sys
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
