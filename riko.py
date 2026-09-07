#!/usr/bin/env python3
"""
riko.py — unofficial async client for the Neakasa Riko wet-meal feeder.

Built on neakasa-litterbox-sdk for auth/transport; everything Riko-specific
(properties, services, events) is mapped from the device's own thing model
(TSL) pulled via /thing/tsl/get on 2026-09-06, FW 1.0.0-0020.

    from riko import Riko, FeederState
    async with Riko(email, password) as r:
        st = await r.status()
        print(st.state, st.food_in_bowl_g, st.food_level, st.water_level)
        await r.feed(food_g=8, water_g=24)

Unofficial. The vendor can change the cloud or firmware at any time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any, Awaitable, Callable

from neakasa_litterbox_sdk import (
    Device,
    LoginResult,
    NeakasaClient,
    Region,
    SessionExpiredError,
)

log = logging.getLogger("riko")

try:
    from config import Config, ConfigError, load as load_config
except ImportError:  # config.py is optional; env vars still work without it
    Config = None  # type: ignore[assignment]
    load_config = None  # type: ignore[assignment]

# ----------------------------------------------------------------- enums
class FeederState(IntEnum):
    IDLE = 0
    PREPARING = 1          # grinding / soaking
    SERVING = 2            # bowl out, meal delivered
    SUSPENDED = 3          # app shows this as "Expired"?  (to confirm)
    FAULT = 4
    SLEEPING = 5
    FRESHNESS_PROTECT = 6  # freshness manager retracted the bowl
    DEEP_CLEANING = 7


class FoodLevel(IntEnum):
    EMPTY = 0
    INSUFFICIENT = 1
    SUFFICIENT = 2


class WaterLevel(IntEnum):
    EMPTY = 0
    INSUFFICIENT = 1
    LOW = 2
    MEDIUM = 3
    FULL = 4


class FeedCtrl(IntEnum):
    PAUSE = 0
    RESUME = 1
    END = 2


class BowlCtrl(IntEnum):
    RETRACT = 0
    EXTEND = 1


class DeepWash(IntEnum):
    STOP = 0
    CLEAN = 1
    RINSE = 2


class VoltageChannel(IntEnum):
    BATTERY = 0
    FOOD_BIN = 1
    WATER_LEVEL = 2
    GRINDER_MOTOR = 3
    NTC = 4
    WATER_PUMP = 5
    GRINDER_BUTTON = 6


# ModuleErr.errCode → meaning (full table from the TSL enum specs, translated).
ERROR_CODES: dict[int, str] = {
    0: "machine tipped over",
    1: "freshness management",
    10: "grinder head missing",
    11: "grinder stalled",
    12: "cover missing",
    13: "grinder jammed with food",
    14: "retract-bowl button pressed",
    20: "bowl missing",
    21: "retract-bowl timeout",
    22: "extend-bowl timeout",
    30: "battery not present",
    31: "power lost",
    32: "battery low",
    33: "battery critical",
    40: "water insufficient",
    41: "water empty",
    50: "food insufficient",
    51: "food empty",
    60: "overweight",
    61: "weighing chip fault",
    62: "load cell fault",
    70: "nozzle clogged",
    71: "water pump open circuit",
}


# ----------------------------------------------------------------- models
@dataclass(frozen=True)
class FeedConfig:
    food_g: float
    water_g: float
    soak_min: int

    @property
    def ratio(self) -> float:
        return self.water_g / self.food_g if self.food_g else 0.0


@dataclass(frozen=True)
class FeedSlot:
    enabled: bool                 # bEn   — slot on/off
    day_enabled: bool             # bDayEn — enabled for today? (suspected: cleared once fired/expired)
    seconds_from_midnight: int    # time
    food_g: float = 0.0           # food
    water_g: float = 0.0          # water
    days_mask: int = 127          # rept — bit0..bit6 = day-of-week, 127 = every day
    audio: int = 0                # aud
    slot_type: int = 1            # type — 1 observed; meaning TBD

    @property
    def hhmm(self) -> str:
        return f"{self.seconds_from_midnight // 3600:02d}:{self.seconds_from_midnight % 3600 // 60:02d}"


def build_schedule(slots: list[FeedSlot]) -> dict[str, Any]:
    """Build an fdPlanStr dict in the exact array-per-field shape the device uses."""
    return {
        "bEn": [int(s.enabled) for s in slots],
        "bDayEn": [int(s.day_enabled) for s in slots],
        "time": [int(s.seconds_from_midnight) for s in slots],
        "rept": [int(s.days_mask) for s in slots],
        "food": [s.food_g for s in slots],
        "water": [s.water_g for s in slots],
        "aud": [int(s.audio) for s in slots],
        "type": [int(s.slot_type) for s in slots],
    }


def parse_schedule(plan_str: str) -> list[FeedSlot]:
    try:
        plan = json.loads(plan_str or "{}")
    except ValueError:
        return []
    n = len(plan.get("time", []))

    def col(key: str, default: Any) -> list[Any]:
        v = plan.get(key, [])
        return [v[i] if i < len(v) else default for i in range(n)]

    return [
        FeedSlot(bool(en), bool(den), t, float(f), float(w), int(r), int(a), int(ty))
        for en, den, t, f, w, r, a, ty in zip(
            col("bEn", 0), col("bDayEn", 0), col("time", 0), col("food", 0),
            col("water", 0), col("rept", 127), col("aud", 0), col("type", 1))
    ]


@dataclass(frozen=True)
class RikoStatus:
    raw: dict[str, Any]

    # --- derived accessors --------------------------------------------
    def _p(self, key: str, default: Any = None) -> Any:
        v = self.raw.get(key, default)
        return v.get("value", v) if isinstance(v, dict) and "value" in v else v

    @property
    def state(self) -> FeederState:
        return FeederState(self._p("deviceState", {}).get("state", 0))

    @property
    def state_param(self) -> int:
        return self._p("deviceState", {}).get("param", 0)

    @property
    def bowl_in(self) -> bool:
        return bool(self._p("bowlStatus", {}).get("bBowlIn", 0))

    @property
    def bowl_tare_g(self) -> int:
        return self._p("bowlStatus", {}).get("bowlGram", 0)

    @property
    def scale_g(self) -> int:
        return self._p("bowlStatus", {}).get("curWeight", 0)

    @property
    def food_in_bowl_g(self) -> int | None:
        return self.scale_g - self.bowl_tare_g if self.bowl_in else None

    @property
    def food_level(self) -> FoodLevel:
        return FoodLevel(self._p("foodLvl", 0))

    @property
    def water_level(self) -> WaterLevel:
        return WaterLevel(self._p("waterLvl", 0))

    @property
    def default_feed(self) -> FeedConfig:
        c = self._p("defFdCfg", {})
        return FeedConfig(c.get("food", 0.0), c.get("water", 0.0), c.get("mixMin", 0))

    @property
    def schedule(self) -> list[FeedSlot]:
        return parse_schedule(self._p("fdPlanStr", "{}"))

    @property
    def freshness(self) -> dict[str, Any]:
        return self._p("freshMgrCfg", {})

    @property
    def battery_pct(self) -> int | None:
        return self._p("pwrStatus", {}).get("batPercentage")

    @property
    def desiccant_days_left(self) -> int | None:
        return self._p("leftDrierDays")

    @property
    def firmware(self) -> str | None:
        return self._p("DeviceVer", {}).get("FW_Version")


@dataclass(frozen=True)
class RikoEvent:
    kind: str                    # "property" | "error" | "service_reply" | "other"
    topic: str
    body: dict[str, Any]
    received_at: float

    @property
    def error(self) -> dict[str, Any] | None:
        """For ModuleErr events: {module, error, errCode, param, meaning}."""
        if self.kind != "error":
            return None
        v = self.body.get("params", {}).get("value", {})
        code = v.get("errCode")
        return {**v, "meaning": ERROR_CODES.get(code, "unknown")}

    @property
    def changes(self) -> dict[str, Any]:
        """For property pushes: {key: value} flattened."""
        if self.kind != "property":
            return {}
        items = self.body.get("params", {}).get("items", {})
        return {k: (v.get("value") if isinstance(v, dict) else v) for k, v in items.items()}


EventHandler = Callable[[RikoEvent], Awaitable[None] | None]


# ----------------------------------------------------------------- client
class Riko:
    """One Riko on one Neakasa account. Use as `async with`."""

    def __init__(self, email: str, password: str, *, region: Region = Region.US,
                 session_file: str | Path | None = ".riko-session.json",
                 device_name: str | None = None) -> None:
        self._client = NeakasaClient(email=email, password=password, region=region)
        self._session_file = Path(session_file) if session_file else None
        self._device_name = device_name
        self._device: Device | None = None

    @classmethod
    def from_config(cls, cfg: "Config") -> "Riko":
        """Build a client from a Config (see config.py)."""
        cfg.require_credentials()
        return cls(cfg.email, cfg.password, region=Region[cfg.region],
                   session_file=cfg.session_file, device_name=cfg.device_name)

    # --- lifecycle ------------------------------------------------------
    async def __aenter__(self) -> Riko:
        await self._client.__aenter__()
        await self.login()
        await self._resolve()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._client.__aexit__(*exc)

    async def login(self) -> None:
        cached = None
        if self._session_file and self._session_file.exists():
            try:
                cached = LoginResult.from_dict(json.loads(self._session_file.read_text()))
            except Exception:
                cached = None
        try:
            result = await self._client.login(cached=cached)
        except SessionExpiredError:
            result = await self._client.login()
        if self._session_file and result is not cached:
            self._session_file.write_text(json.dumps(result.to_dict()))

    async def _resolve(self) -> Device:
        if self._device:
            return self._device
        devices = await self._client.list_devices()
        if self._device_name:
            match = [d for d in devices if d.device_name == self._device_name]
        else:
            match = [d for d in devices if d.product_name.lower().startswith("riko")]
        if not match:
            raise RuntimeError(f"No Riko found on account (devices: {[d.product_name for d in devices]})")
        self._device = match[0]
        self._device_name = self._device.device_name
        return self._device

    @property
    def device(self) -> Device:
        assert self._device, "not connected"
        return self._device

    # --- low-level -------------------------------------------------------
    async def _with_relogin(self, fn: Callable[[], Awaitable[Any]]) -> Any:
        try:
            return await fn()
        except SessionExpiredError:
            await self.login()
            return await fn()
        except Exception as exc:
            # Aliyun 29003 = iotToken invalid (e.g. another login on the same
            # account replaced ours). The SDK only refreshes on HTTP 401, so
            # handle it here with a full re-login and one retry.
            if "29003" in str(exc):
                log.info("iotToken invalidated (29003); re-logging in")
                await self._client.login()
                if self._session_file and self._client.login_result:
                    self._session_file.write_text(json.dumps(self._client.login_result.to_dict()))
                return await fn()
            raise

    async def get_properties(self) -> dict[str, Any]:
        return await self._with_relogin(
            lambda: self._client._get_properties(self._device_name))  # noqa: SLF001

    async def set_property(self, key: str, value: Any) -> None:
        await self._with_relogin(
            lambda: self._client._set_property(self._device_name, key, value, context=f"set {key}"))  # noqa: SLF001

    async def invoke(self, identifier: str, **args: Any) -> Any:
        """Call a TSL service and return the gateway's response data (if any)."""
        dev = await self._resolve()

        async def call() -> Any:
            return await self._client._aliyun_call_authed(  # noqa: SLF001
                "/thing/service/invoke",
                api_version="1.0.5",
                payload={"iotId": dev.iot_id, "identifier": identifier, "args": args},
                language="en-US",
                context=f"invoke {identifier}",
            )
        log.info("invoke %s %s", identifier, args)
        return await self._with_relogin(call)

    # --- status ----------------------------------------------------------
    async def status(self) -> RikoStatus:
        return RikoStatus(await self.get_properties())

    # --- feeding ---------------------------------------------------------
    async def feed(self, food_g: float, water_g: float | None = None, *,
                   save_as_default: bool = False) -> Any:
        """Dispense one meal now. water_g defaults to the current default ratio."""
        if water_g is None:
            cfg = (await self.status()).default_feed
            water_g = round(food_g * cfg.ratio, 1) if cfg.ratio else 0.0
        if not 0 <= food_g <= 60 or not 0 <= water_g <= 600:
            raise ValueError("food 0-60 g, water 0-600 g")
        return await self.invoke("feedOnce", food=float(food_g), water=float(water_g),
                                 bSaveAsDef=1 if save_as_default else 0)

    async def feed_control(self, action: FeedCtrl) -> Any:
        return await self.invoke("feedCtrl", ctrlType=int(action))

    async def dispense_food(self, on: bool) -> Any:
        return await self.invoke("foodProvide", bOnOff=1 if on else 0)

    async def dispense_water(self, on: bool) -> Any:
        return await self.invoke("waterProvide", bOnOff=1 if on else 0)

    async def set_default_feed(self, food_g: float, water_g: float, soak_min: int) -> None:
        await self.set_property("defFdCfg", {"food": float(food_g), "water": float(water_g),
                                             "mixMin": int(soak_min)})

    async def set_schedule_raw(self, plan: dict[str, Any]) -> None:
        """Write fdPlanStr. Pass the same shape the device reports (bEn/bDayEn/time/rept...)."""
        await self.set_property("fdPlanStr", json.dumps(plan, separators=(",", ":")))

    async def set_schedule(self, slots: list[FeedSlot]) -> None:
        """Write a full schedule (all slots). Read status().schedule first, modify, write back."""
        await self.set_schedule_raw(build_schedule(slots))

    # --- bowl / scale ----------------------------------------------------
    async def bowl(self, action: BowlCtrl) -> Any:
        return await self.invoke("bowlCtrl", ctrlType=int(action))

    async def tare(self, bowl_g: int | None = None) -> Any:
        """Zero the platform. With bowl_g, also store the empty-bowl weight the
        firmware subtracts to compute food-in-bowl (factory default is 65)."""
        if bowl_g is None:
            return await self.invoke("resetScaleZero")
        if not 0 <= bowl_g <= 200:
            raise ValueError("bowl_g 0-200")
        return await self.invoke("resetScaleZero", bowlGram=int(bowl_g))

    async def set_bowl_weight_property(self, bowl_g: int) -> None:
        """Fallback: write bowlStatus.bowlGram directly (bowlStatus is rw in the TSL)."""
        st = await self.status()
        await self.set_property("bowlStatus", {"bBowlIn": int(st.bowl_in),
                                               "bowlGram": int(bowl_g),
                                               "curWeight": st.scale_g})

    async def set_freshness(self, on: bool, leftover_threshold_g: int | None = None,
                            monitor_hours: int | None = None) -> None:
        cur = (await self.status()).freshness
        await self.set_property("freshMgrCfg", {
            "bOnOff": 1 if on else 0,
            "leftOverTTH": int(leftover_threshold_g if leftover_threshold_g is not None
                               else cur.get("leftOverTTH", 20)),
            "monitorTime": int(monitor_hours if monitor_hours is not None
                               else cur.get("monitorTime", 4)),
        })

    # --- maintenance / diagnostics --------------------------------------
    async def reboot(self) -> Any:
        return await self.invoke("RmReboot")

    async def deep_wash(self, mode: DeepWash) -> Any:
        return await self.invoke("deepWashing", type=int(mode))

    async def reset_desiccant(self, days: int = 40) -> Any:
        return await self.invoke("resetDrierTime", drierEffeDays=int(days))

    async def read_voltage(self, channel: VoltageChannel) -> Any:
        """Raw ADC reading (0-10000) for a sensor channel. Useful for checking
        whether foodLvl/waterLvl 'Insufficient' is a sensor problem or a threshold one."""
        return await self.invoke("getVoltage", chanIdx=int(channel))

    async def udp_channel(self, on: bool) -> Any:
        """Toggle the device's UDP command channel. Undocumented; with it off the
        device has no open TCP or UDP ports at all (nmap, 2026-09-07). The guess is
        that this opens a local listener, which would give us a cloud-free path."""
        return await self.invoke("udpCmd", bOnOff=1 if on else 0)

    async def read_config(self, cfg_file: str) -> Any:
        return await self.invoke("cfgRead", cfgFile=cfg_file)

    async def set_child_lock(self, on: bool) -> None:
        await self.set_property("childLockOnOff", 1 if on else 0)

    # --- clock ----------------------------------------------------------
    async def unclog(self, *, pulses: int = 2, pulse_s: float = 2.0,
                     resume: bool = True, settle_s: float = 3.0,
                     verify_s: float = 25.0,
                     journal: list[dict[str, Any]] | None = None) -> RikoStatus:
        """Recover from a 'pumper: Stuck' stall (code 70 / the app's "Nozzle clogged").

        The pump isn't clogged, it has lost prime: water drains back toward the tank
        while the feeder is idle, and small pumps can't pull air. Short pump runs
        push the air out.

        Deliberately does NOT call feedCtrl END. Observed 2026-09-07: END puts the
        device into SERVING with param=1 -- it dumps whatever was already ground into
        the bowl and extends the tray. Re-feeding after that double-doses the cat
        (94 g delivered against a 32 g target), and the tray bouncing in and out is
        the gap the cat gets under. Priming while the device is still suspended keeps
        the tray retracted, and RESUME lets the firmware finish the meal it started --
        it knows what it has and hasn't dispensed, and we can't ask (curWeight is
        cached, not live; see riko_trace.py).

        WHAT'S UNPROVEN: waterProvide has never been observed to move water. Every
        test so far ran with the tray EXTENDED, where the firmware accepts the call
        (ReturnCode 0) and silently declines to pump -- including the 11:52 recovery,
        where END had already extended the tray before the pulses fired. It may work
        from a real stall, where the tray is retracted and the device is not in
        freshness protection, but that state has proved impossible to reach
        deliberately. The pulses are kept because the app's own retry (a plain
        RESUME) has failed repeatedly in the past, so resume alone is not sufficient.

        Every step records a full status snapshot into `journal` (and the log) so the
        next real stall settles this instead of producing more ambiguity.
        """
        j = journal if journal is not None else []

        async def note(step: str) -> RikoStatus:
            st = await self.status()
            entry = {
                "step": step,
                "t": time.strftime("%Y-%m-%d %H:%M:%S"),
                "state": st.state.name,
                "param": st.state_param,
                "bowl_in": st.bowl_in,
                "scale_g": st.scale_g,
                "food_in_bowl_g": st.food_in_bowl_g,
                "water_level": st.water_level.name,
            }
            j.append(entry)
            log.info("unclog[%s] state=%s param=%s bowl_in=%s scale=%sg",
                     step, entry["state"], entry["param"], entry["bowl_in"], entry["scale_g"])
            return st

        st = await note("before")

        if st.state == FeederState.IDLE:
            log.info("device already idle; priming only")
        elif st.state == FeederState.SUSPENDED:
            code = ERROR_CODES.get(st.state_param, f"code {st.state_param}")
            if st.state_param not in (0, 70):
                raise RuntimeError(
                    f"suspended for '{code}', not a pump stall -- not touching it")
            log.info("suspended on '%s'; priming with the tray where it is", code)
        else:
            raise RuntimeError(
                f"device is {st.state.name}; unclog only handles IDLE or SUSPENDED")

        was_suspended = st.state == FeederState.SUSPENDED

        for i in range(pulses):
            log.info("pump prime %d/%d (%.1fs)", i + 1, pulses, pulse_s)
            await self.dispense_water(True)
            await asyncio.sleep(pulse_s)
            await self.dispense_water(False)
            await asyncio.sleep(1.5)
            await note(f"after_pulse_{i + 1}")
        await asyncio.sleep(settle_s)
        await note("after_priming")

        if was_suspended and resume:
            log.info("resuming the suspended meal")
            await self.feed_control(FeedCtrl.RESUME)
            deadline = time.monotonic() + verify_s
            while time.monotonic() < deadline:
                await asyncio.sleep(3)
                st = await note("after_resume")
                if st.state in (FeederState.PREPARING, FeederState.SERVING):
                    log.info("meal resumed (state=%s)", st.state.name)
                    return st
                if st.state == FeederState.SUSPENDED:
                    code = ERROR_CODES.get(st.state_param, f"code {st.state_param}")
                    raise RuntimeError(
                        f"still suspended after priming ('{code}'). The pump may "
                        f"genuinely be failing, or the tank needs reseating.")
            log.warning("resume sent but no state change within %.0fs", verify_s)

        return await note("final")

    async def sync_time(self, source: int = 1) -> Any:
        """Ask the device to re-sync its clock. 0 = from Neakasa backend, 1 = from Aliyun."""
        return await self.invoke("updateTime", type=int(source))

    async def set_timezone(self, zone_hours: int, dst: bool, *, keep_bounds: bool = True) -> None:
        """Override timeZoneMsg. FW 1.0.0-0020 ignores the dst flag, so to get EDT
        pass zone_hours=-4, dst=False (and -5 again after DST ends in November).
        Other fields (timeZoneID, DST bounds, year) are carried over unchanged."""
        cur = (await self.status())._p("timeZoneMsg", {}) or {}
        msg = dict(cur) if keep_bounds else {}
        msg.update({"zone": int(zone_hours), "dst": 1 if dst else 0})
        msg.setdefault("timeZoneID", "America/New_York")
        msg.setdefault("year", time.localtime().tm_year)
        await self.set_property("timeZoneMsg", msg)

    # --- events ----------------------------------------------------------
    async def watch(self, handler: EventHandler, *, forever: bool = True) -> None:
        """Stream every push for this device to `handler` (sync or async)."""
        stream = self._client.watch_status()
        original = stream._handle_message  # noqa: SLF001
        me = self._device_name

        async def tapped(topic: str, payload: bytes) -> None:
            try:
                body = json.loads(payload)
            except ValueError:
                return
            params = body.get("params", {}) if isinstance(body, dict) else {}
            if params.get("deviceName") not in (None, me):
                return
            if "/thing/properties" in topic:
                kind = "property"
            elif "/thing/events" in topic and params.get("type") == "error":
                kind = "error"
            elif "/service/invoke/reply" in topic:
                kind = "service_reply"
            else:
                kind = "other"
            ev = RikoEvent(kind=kind, topic=topic, body=body, received_at=time.time())
            try:
                res = handler(ev)
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                log.exception("event handler raised")
            await original(topic, payload)

        stream._handle_message = tapped  # type: ignore[method-assign]  # noqa: SLF001
        async with stream:
            if forever:
                await stream.run_forever()


# ----------------------------------------------------------------- CLI
async def _cli() -> int:
    import argparse
    import os
    import sys

    ap = argparse.ArgumentParser(description="Neakasa Riko (unofficial)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("raw")
    f = sub.add_parser("feed"); f.add_argument("food", type=float); f.add_argument("water", type=float, nargs="?")
    t = sub.add_parser("tare"); t.add_argument("bowl_g", type=int, nargs="?",
        help="empty-bowl weight; defaults to bowl_grams from config")
    b = sub.add_parser("bowl"); b.add_argument("action", choices=["retract", "extend"])
    fr = sub.add_parser("freshness"); fr.add_argument("onoff", choices=["on", "off"])
    v = sub.add_parser("voltage"); v.add_argument("channel", choices=[c.name.lower() for c in VoltageChannel])
    sub.add_parser("watch")
    fc = sub.add_parser("feedctrl"); fc.add_argument("action", choices=["pause", "resume", "end"])
    pm = sub.add_parser("pump"); pm.add_argument("onoff", choices=["on", "off"])
    gr = sub.add_parser("grinder"); gr.add_argument("onoff", choices=["on", "off"])
    ud = sub.add_parser("udp", help="toggle the device's UDP command channel")
    ud.add_argument("onoff", choices=["on", "off"])
    uc = sub.add_parser("unclog", help="recover from a code-70 pump stall")
    uc.add_argument("--no-resume", action="store_true",
                    help="prime the pump only; don't resume the suspended meal")
    uc.add_argument("--pulses", type=int, default=2, help="pump priming runs (default 2)")
    sub.add_parser("synctime")
    tz = sub.add_parser("tz"); tz.add_argument("zone", type=int, nargs="?",
        help="e.g. -4 for EDT, -5 for EST; defaults to tz_offset from config")
    sub.add_parser("config", help="show resolved configuration")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if load_config is None:
        print("config.py not found next to riko.py", file=sys.stderr); return 2
    try:
        cfg = load_config()
        if args.cmd == "config":
            print(f"source: {cfg.source_file or '(defaults + environment)'}")
            for k, v in cfg.redacted().items():
                print(f"  {k:<14} {v}")
            return 0
        cfg.require_credentials()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr); return 2

    async with Riko.from_config(cfg) as r:
        if args.cmd == "status":
            s = await r.status()
            print(f"state={s.state.name} param={s.state_param}")
            print(f"bowl_in={s.bowl_in} scale={s.scale_g}g tare={s.bowl_tare_g}g food_in_bowl={s.food_in_bowl_g}g")
            print(f"food={s.food_level.name} water={s.water_level.name} battery={s.battery_pct}% desiccant={s.desiccant_days_left}d fw={s.firmware}")
            d = s.default_feed
            print(f"default: {d.food_g}g food / {d.water_g}g water (1:{d.ratio:.1f}) soak {d.soak_min}min")
            print("schedule: " + ", ".join(
                f"{sl.hhmm} {sl.food_g:g}g/{sl.water_g:g}g"
                + ("" if sl.enabled else " (off)") + ("" if sl.day_enabled else " [today:off]")
                for sl in s.schedule))
            print(f"freshness: {s.freshness}")
            print(f"timezone: {s._p('timeZoneMsg', {})}")
        elif args.cmd == "raw":
            print(json.dumps(await r.get_properties(), indent=2, default=str))
        elif args.cmd == "feed":
            print(await r.feed(args.food, args.water))
        elif args.cmd == "tare":
            print(await r.tare(args.bowl_g if args.bowl_g is not None else cfg.bowl_grams))
        elif args.cmd == "bowl":
            print(await r.bowl(BowlCtrl.RETRACT if args.action == "retract" else BowlCtrl.EXTEND))
        elif args.cmd == "freshness":
            await r.set_freshness(args.onoff == "on"); print("ok")
        elif args.cmd == "voltage":
            print(await r.read_voltage(VoltageChannel[args.channel.upper()]))
        elif args.cmd == "feedctrl":
            print(await r.feed_control({"pause": FeedCtrl.PAUSE, "resume": FeedCtrl.RESUME, "end": FeedCtrl.END}[args.action]))
        elif args.cmd == "pump":
            print(await r.dispense_water(args.onoff == "on"))
        elif args.cmd == "grinder":
            print(await r.dispense_food(args.onoff == "on"))
        elif args.cmd == "unclog":
            journal: list[dict[str, Any]] = []
            try:
                st = await r.unclog(pulses=args.pulses, resume=not args.no_resume,
                                    journal=journal)
                print(f"ok: state={st.state.name} param={st.state_param}")
            except RuntimeError as exc:
                print(f"unclog: {exc}", file=sys.stderr)
                return 1
            finally:
                if journal:
                    path = cfg.capture_dir / f"unclog_{time.strftime('%Y%m%d_%H%M%S')}.json"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(journal, indent=2))
                    print(f"\njournal ({len(journal)} snapshots) -> {path}")
                    print(f"{'step':<18} {'state':<12} {'param':>5} {'bowl':>5} {'scale':>6}")
                    for e in journal:
                        print(f"{e['step']:<18} {e['state']:<12} {e['param']:>5} "
                              f"{str(e['bowl_in']):>5} {e['scale_g']:>5}g")
        elif args.cmd == "udp":
            print(await r.udp_channel(args.onoff == "on"))
        elif args.cmd == "synctime":
            print(await r.sync_time(1))
        elif args.cmd == "tz":
            await r.set_timezone(args.zone if args.zone is not None else cfg.tz_offset, dst=False)
            print("ok — check the clock on the display")
        elif args.cmd == "watch":
            def show(ev: RikoEvent) -> None:
                ts = time.strftime("%H:%M:%S", time.localtime(ev.received_at))
                if ev.kind == "property":
                    for k, val in ev.changes.items():
                        print(f"[{ts}] {k} = {val!r}")
                elif ev.kind == "error":
                    print(f"[{ts}] ERROR {ev.error}")
                else:
                    print(f"[{ts}] {ev.kind} {json.dumps(ev.body, default=str)[:300]}")
            print("watching… Ctrl-C to stop")
            await r.watch(show)
    return 0


if __name__ == "__main__":
    import sys
    try:
        sys.exit(asyncio.run(_cli()))
    except KeyboardInterrupt:
        pass
