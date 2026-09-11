#!/usr/bin/env python3
"""
config.py — configuration for the Riko tools.

Resolution order (later wins):
  1. built-in defaults
  2. config file:  ./riko.toml, then ~/.config/riko/config.toml
     (or the path in $RIKO_CONFIG)
  3. environment:  RIKO_EMAIL / RIKO_PASSWORD / RIKO_DEVICE_NAME / ...
     (legacy NEAKASA_EMAIL / NEAKASA_PASSWORD still honoured)

Credentials never belong in the config file if you can avoid it — keep them
in the environment or a .env loaded by systemd. If they ARE in the file, it
must not be group/world readable; load() refuses otherwise.

Usage:
    from config import load
    cfg = load()
    async with Riko(cfg.email, cfg.password, device_name=cfg.device_name) as r:
        ...
"""

from __future__ import annotations

import os
import stat
import sys
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

CONFIG_ENV = "RIKO_CONFIG"
SEARCH_PATHS = [Path("riko.toml"), Path.home() / ".config" / "riko" / "config.toml"]

# env var -> (section, key). Legacy names map to the same targets.
ENV_MAP: dict[str, tuple[str, str]] = {
    "RIKO_EMAIL": ("account", "email"),
    "RIKO_PASSWORD": ("account", "password"),
    "RIKO_FEEDER_OWNER_ID": ("feeder", "feeder_owner_id"),
    "NEAKASA_EMAIL": ("account", "email"),
    "NEAKASA_PASSWORD": ("account", "password"),
    "RIKO_REGION": ("account", "region"),
    "RIKO_DEVICE_NAME": ("device", "device_name"),
    "RIKO_BOWL_GRAMS": ("device", "bowl_grams"),
    "RIKO_TZ_OFFSET": ("device", "tz_offset"),
    "RIKO_SESSION_FILE": ("paths", "session_file"),
    "RIKO_CAPTURE_DIR": ("paths", "capture_dir"),
    "RIKO_STATE_DIR": ("paths", "state_dir"),
}

INT_KEYS = {"bowl_grams", "tz_offset", "poll_seconds", "feeder_owner_id"}
SECRET_KEYS = {"password"}


class ConfigError(RuntimeError):
    pass


@dataclass
class Config:
    # account
    email: str = ""
    password: str = ""
    region: str = "US"

    # device — device_name is optional; with one Riko on the account we find it
    device_name: str | None = None
    bowl_grams: int = 68          # true empty-bowl weight; factory default is a wrong 65
    tz_offset: int = -5           # standard-time base offset; fallback only. FW 1.0.0-0023
                                  # applies DST itself, so this is used only to correct a
                                  # device whose own DST handling is broken (older firmware).

    # paths
    session_file: Path = Path(".riko-session.json")
    capture_dir: Path = Path("riko_capture")
    state_dir: Path = Path("riko_state")

    # runtime
    poll_seconds: int = 15

    # feeder owner's user_id (NOT a secret — an opaque account number). The intake
    # ledger keys off this param, so the automation account can read the owner's
    # records by passing it. Read by neakasa.py. Find it once via a login as the
    # owner (ali_user_id), then hardcode it here.
    feeder_owner_id: int = 0

    # notify config passthrough ([notify] table), read by notify.py
    _notify: dict = field(default_factory=dict, repr=False)

    # provenance, for debugging
    source_file: Path | None = field(default=None, repr=False)

    def require_feeder_owner(self) -> None:
        if not self.feeder_owner_id:
            raise ConfigError(
                "No feeder_owner_id set. Put the feeder owner's user_id under [feeder] "
                "feeder_owner_id in your config, or pass --owner-id to neakasa.py.")

    def require_credentials(self) -> None:
        if not self.email or not self.password:
            raise ConfigError(
                "No credentials. Set RIKO_EMAIL and RIKO_PASSWORD in the environment, "
                "or put them in [account] in your config file."
            )

    def redacted(self) -> dict[str, Any]:
        out = {}
        for f in fields(self):
            if f.name == "source_file":
                continue
            v = getattr(self, f.name)
            out[f.name] = "********" if (f.name in SECRET_KEYS and v) else (str(v) if isinstance(v, Path) else v)
        return out


def _find_config_file() -> Path | None:
    if env := os.environ.get(CONFIG_ENV):
        p = Path(env).expanduser()
        if not p.exists():
            raise ConfigError(f"{CONFIG_ENV} points at {p}, which does not exist")
        return p
    for p in SEARCH_PATHS:
        if p.expanduser().exists():
            return p.expanduser()
    return None


def _check_permissions(path: Path, data: dict[str, Any]) -> None:
    """A config file holding a password must not be readable by group or others."""
    if not data.get("account", {}).get("password"):
        return
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError(
            f"{path} contains a password but is group/world accessible. "
            f"Run: chmod 600 {path}"
        )


def load(path: str | Path | None = None) -> Config:
    """Build a Config from defaults + file + environment."""
    merged: dict[str, dict[str, Any]] = {}
    cfg_path = Path(path).expanduser() if path else _find_config_file()

    if cfg_path:
        try:
            with cfg_path.open("rb") as fh:
                merged = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{cfg_path} is not valid TOML: {exc}") from exc
        _check_permissions(cfg_path, merged)

    for env_name, (section, key) in ENV_MAP.items():
        if (val := os.environ.get(env_name)) is not None:
            merged.setdefault(section, {})[key] = val

    notify_tbl = merged.get("notify", {}) if isinstance(merged.get("notify"), dict) else {}

    flat: dict[str, Any] = {}
    for section in ("account", "device", "paths", "runtime", "feeder"):
        flat.update(merged.get(section, {}))
    # tolerate top-level scalar keys too (skip known section tables)
    _sections = {"account", "device", "paths", "runtime", "notify", "feeder"}
    flat.update({k: v for k, v in merged.items()
                 if not isinstance(v, dict) and k not in _sections})

    known = {f.name for f in fields(Config)}
    if unknown := set(flat) - known - {"source_file"}:
        raise ConfigError(f"Unknown config key(s): {', '.join(sorted(unknown))}")

    kwargs: dict[str, Any] = {}
    for key, val in flat.items():
        if key in INT_KEYS:
            try:
                kwargs[key] = int(val)
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"{key} must be an integer, got {val!r}") from exc
        elif key.endswith(("_file", "_dir")):
            kwargs[key] = Path(str(val)).expanduser()
        else:
            kwargs[key] = val

    cfg = Config(**kwargs, source_file=cfg_path, _notify=notify_tbl)

    if not 0 <= cfg.bowl_grams <= 200:
        raise ConfigError(f"bowl_grams must be 0-200 (the firmware's range), got {cfg.bowl_grams}")
    if not -12 <= cfg.tz_offset <= 14:
        raise ConfigError(f"tz_offset out of range: {cfg.tz_offset}")
    return cfg


EXAMPLE = """\
# riko.toml — copy to riko.toml (or ~/.config/riko/config.toml) and edit.
# Prefer keeping credentials in the environment; if you put them here,
# chmod 600 the file.

[account]
# email = "you@example.com"
# password = "..."
region = "US"

[device]
# Leave device_name unset to auto-detect the only Riko on the account.
# device_name = "WL0300..."

# True weight of your empty bowl in grams. Weigh it: the factory default of 65
# is wrong for the bowls that ship with the unit (~68), which makes every
# food-in-bowl reading off by that difference.
bowl_grams = 68

# Standard-time base UTC offset (fallback only). FW 1.0.0-0023 applies the DST flag
# correctly, so the monitor validates the device clock against real time and this is
# used only to correct a device whose own DST handling is broken. -5 = US Eastern base.
tz_offset = -5

[paths]
session_file = ".riko-session.json"
capture_dir = "riko_capture"
state_dir = "riko_state"

[runtime]
poll_seconds = 15

# The intake ledger keys off the feeder OWNER's user_id (an account number, not a
# secret). The automation [account] above can read the owner's history by passing it.
# Find it once by logging in as the owner and printing ali_user_id.
[feeder]
# feeder_owner_id = 400133257
"""


if __name__ == "__main__":
    if "--example" in sys.argv:
        print(EXAMPLE)
        raise SystemExit(0)
    try:
        cfg = load()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    src = cfg.source_file or "(no config file found — defaults + environment)"
    print(f"source: {src}")
    for k, v in cfg.redacted().items():
        print(f"  {k:<14} {v}")
    print("\ncredentials:", "present" if cfg.email and cfg.password else "MISSING")
