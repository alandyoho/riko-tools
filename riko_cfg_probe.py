#!/usr/bin/env python3
"""
riko_cfg_probe.py — probe the Riko's cfgRead service with guessed filenames.

The thing model exposes cfgRead(cfgFile: text) -> cfgMsg: text (1024 chars).
Nothing tells us what filenames exist, so this walks a wordlist and records
which ones return content. READ ONLY: cfgWrite is never called.

The device answers each call asynchronously; the gateway reply carries the
output, so a "hit" is a call whose reply contains a non-empty cfgMsg. A miss
may look like an empty string, an error, or a timeout -- all are recorded so
the pattern is visible afterwards.

USAGE
    python3 riko_cfg_probe.py --dry-run            # print the wordlist, send nothing
    python3 riko_cfg_probe.py                      # built-in wordlist
    python3 riko_cfg_probe.py --names a.cfg b.cfg  # specific names only
    python3 riko_cfg_probe.py --wordlist my.txt    # one name per line
    python3 riko_cfg_probe.py --delay 2.0          # slower, gentler on the device

SAFETY
  * Requires the device to be IDLE; refuses otherwise, and stops if it leaves idle.
  * Rate limited (default 1.5 s between calls).
  * Results go to riko_capture/cfgprobe_<timestamp>.json.
  * If the device starts erroring or goes offline, stop and leave it alone.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from config import ConfigError, load as load_config
from riko import FeederState, Riko

# Guesses, grouped by the theory behind them. Extensions matter on embedded
# filesystems, so common ones are tried for the most promising stems.
BASE_NAMES = [
    # generic
    "config", "cfg", "conf", "settings", "system", "sys", "device", "dev",
    # calibration — the bowl weight and scale zero have to live somewhere
    "calib", "cal", "calibration", "scale", "weight", "loadcell", "adc", "tare",
    # subsystem parameters
    "pump", "water", "grinder", "motor", "feed", "feeder", "bowl", "sensor",
    # manufacturing / identity
    "factory", "fac", "mfg", "product", "sn", "serial", "mac", "wifi", "net",
    # runtime / diagnostics
    "log", "logs", "debug", "err", "error", "errors", "status", "state",
    "runtime", "stat", "stats", "history", "record", "event", "events",
    # firmware / ota
    "ota", "fw", "firmware", "version", "boot", "partition",
    # schedule / user data
    "plan", "schedule", "user", "userdata", "profile", "timezone", "tz",
]

EXTENSIONS = ["", ".cfg", ".conf", ".json", ".txt", ".ini", ".dat", ".bin"]

# Stems worth trying with every extension; the rest get the bare name plus .cfg
PRIORITY = {"config", "cfg", "calib", "scale", "factory", "system", "log", "debug"}

# Path-ish variants, in case the service wants something rooted.
PREFIXES = ["", "/", "/etc/", "/data/", "/config/", "/mnt/"]


def build_wordlist() -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for stem in BASE_NAMES:
        exts = EXTENSIONS if stem in PRIORITY else ["", ".cfg"]
        for ext in exts:
            name = f"{stem}{ext}"
            if name not in seen:
                seen.add(name)
                names.append(name)
    # a few rooted variants of the most likely stems only — keeps the run short
    for prefix in PREFIXES[1:]:
        for stem in ("config", "config.json", "calib", "factory"):
            name = f"{prefix}{stem}"
            if name not in seen:
                seen.add(name)
                names.append(name)
    return names


def summarize(value: object) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = text.replace("\n", "\\n")
    return text if len(text) <= 300 else text[:297] + "..."


async def main() -> int:
    ap = argparse.ArgumentParser(description="Probe Riko cfgRead filenames (read only)")
    ap.add_argument("--names", nargs="+", help="probe only these names")
    ap.add_argument("--wordlist", type=Path, help="file with one name per line")
    ap.add_argument("--delay", type=float, default=1.5, help="seconds between calls")
    ap.add_argument("--limit", type=int, default=None, help="stop after N names")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    if args.names:
        names = args.names
    elif args.wordlist:
        names = [l.strip() for l in args.wordlist.read_text().splitlines() if l.strip()]
    else:
        names = build_wordlist()
    if args.limit:
        names = names[:args.limit]

    print(f"{len(names)} names, ~{len(names) * args.delay / 60:.1f} min at {args.delay}s apart")
    if args.dry_run:
        for n in names:
            print(f"  {n}")
        return 0

    try:
        cfg = load_config()
        cfg.require_credentials()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    out = args.out or cfg.capture_dir / f"cfgprobe_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, object]] = []
    hits = 0

    async with Riko.from_config(cfg) as r:
        st = await r.status()
        if st.state != FeederState.IDLE:
            print(f"device is {st.state.name}, not idle — aborting", file=sys.stderr)
            return 1
        print(f"device idle, fw {st.firmware}\n")

        for i, name in enumerate(names, 1):
            entry: dict[str, object] = {"name": name, "t": time.strftime("%H:%M:%S")}
            try:
                resp = await r.read_config(name)
                entry["raw"] = resp
                data = resp.get("data") if isinstance(resp, dict) else None
                params = data.get("Params") if isinstance(data, dict) else None
                msg = params.get("cfgMsg") if isinstance(params, dict) else None
                entry["cfgMsg"] = msg
                if msg:
                    hits += 1
                    entry["hit"] = True
                    print(f"[{i}/{len(names)}] HIT  {name}")
                    print(f"    {summarize(msg)}")
                else:
                    entry["hit"] = False
                    print(f"[{i}/{len(names)}] ---  {name}")
            except Exception as exc:
                entry["error"] = str(exc)
                entry["hit"] = False
                print(f"[{i}/{len(names)}] ERR  {name}: {str(exc)[:90]}")
            results.append(entry)

            if i % 10 == 0:
                st = await r.status()
                if st.state != FeederState.IDLE:
                    print(f"\ndevice left idle ({st.state.name}) — stopping", file=sys.stderr)
                    break
            await asyncio.sleep(args.delay)

    out.write_text(json.dumps(results, indent=2, default=str))
    print(f"\n{hits} hit(s) out of {len(results)} probed")
    if hits:
        print("\nfilenames that returned content:")
        for e in results:
            if e.get("hit"):
                print(f"  {e['name']}")
    print(f"\nfull results: {out}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\ninterrupted")
