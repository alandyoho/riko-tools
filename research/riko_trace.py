#!/usr/bin/env python3
"""
riko_trace.py — poll the bowl weight rapidly through a feed cycle.

Answers two questions the event stream can't:
  1. Does curWeight update live during a dispense, or only at checkpoints?
  2. Can we see food and water land as separate steps?

Whatever the answer, the CSV is the raw material for smarter recovery: if we
know how much is already in the bowl when a feed stalls, we can add only the
missing part instead of re-issuing a whole meal.

Usage (from the Pi, or via the `riko` alias pattern):
    python3 riko_trace.py                    # wait for the next feed, then trace it
    python3 riko_trace.py --feed 0 24        # trigger a water-only feed and trace it
    python3 riko_trace.py --feed 8 24 --interval 0.5

Output: riko_capture/trace_<timestamp>.csv with columns
    elapsed_s, wall_clock, state, state_name, param, bowl_in, scale_g, food_g, delta_g
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
import time
from pathlib import Path

from config import ConfigError, load as load_config
from riko import ERROR_CODES, FeederState, Riko


async def trace(r: Riko, out: Path, interval: float, idle_stop_s: float,
                start_timeout_s: float, feed: tuple[float, float] | None) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    fh = out.open("w", newline="")
    w = csv.writer(fh)
    w.writerow(["elapsed_s", "wall_clock", "state", "state_name", "param",
                "bowl_in", "scale_g", "food_g", "delta_g"])

    t0 = time.monotonic()
    started = False
    last_active = t0
    prev_scale: int | None = None
    baseline: int | None = None
    peak = 0
    errors: list[str] = []

    if feed:
        st = await r.status()
        if st.state != FeederState.IDLE:
            print(f"device is {st.state.name}, not idle — aborting", file=sys.stderr)
            return
        baseline = st.scale_g
        print(f"baseline scale reading: {baseline} g (tare {st.bowl_tare_g})")
        await r.feed(feed[0], feed[1])
        print(f"requested {feed[0]}g food / {feed[1]}g water")
    else:
        print(f"waiting up to {start_timeout_s/60:.0f} min for a feed to start…")

    while True:
        now = time.monotonic()
        try:
            st = await r.status()
        except Exception as exc:
            print(f"poll error: {exc}", file=sys.stderr)
            await asyncio.sleep(interval)
            continue

        if baseline is None and st.bowl_in:
            baseline = st.scale_g

        active = st.state != FeederState.IDLE
        if active and not started:
            started = True
            t0 = now
            print(f"--- feed started (state {st.state.name}) ---")
        if active:
            last_active = now

        if started:
            delta = "" if prev_scale is None else st.scale_g - prev_scale
            food = st.scale_g - st.bowl_tare_g if st.bowl_in else ""
            w.writerow([f"{now - t0:.1f}", time.strftime("%H:%M:%S"), int(st.state),
                        st.state.name, st.state_param, int(st.bowl_in),
                        st.scale_g, food, delta])
            fh.flush()
            peak = max(peak, st.scale_g)
            if delta not in ("", 0):
                print(f"  {now - t0:6.1f}s  {st.state.name:<12} scale={st.scale_g:>4}g  "
                      f"({delta:+d})")
            if st.state == FeederState.SUSPENDED and st.state_param:
                msg = ERROR_CODES.get(st.state_param, f"code {st.state_param}")
                if msg not in errors:
                    errors.append(msg)
                    print(f"  !! suspended: {msg}")
            prev_scale = st.scale_g

        if started and not active and (now - last_active) > idle_stop_s:
            break
        if not started and (now - t0) > start_timeout_s:
            print("no feed started within the timeout")
            fh.close()
            return
        await asyncio.sleep(interval)

    fh.close()
    st = await r.status()
    print(f"\n--- done --- {out}")
    if baseline is not None:
        print(f"baseline {baseline} g -> peak {peak} g  = {peak - baseline} g delivered")
    if feed:
        print(f"requested {feed[0] + feed[1]} g total")
    if errors:
        print("errors seen: " + ", ".join(errors))
    print(f"final: scale={st.scale_g}g tare={st.bowl_tare_g}g bowl_in={st.bowl_in}")


async def main() -> int:
    ap = argparse.ArgumentParser(description="Trace bowl weight through a feed")
    ap.add_argument("--feed", nargs=2, type=float, metavar=("FOOD_G", "WATER_G"),
                    help="trigger this feed instead of waiting for a scheduled one")
    ap.add_argument("--interval", type=float, default=1.0, help="poll seconds (default 1)")
    ap.add_argument("--idle-stop", type=float, default=20.0,
                    help="stop after this many idle seconds (default 20)")
    ap.add_argument("--wait", type=float, default=900.0,
                    help="seconds to wait for a feed to start (default 900)")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    try:
        cfg = load_config()
        cfg.require_credentials()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    out = args.out or cfg.capture_dir / f"trace_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    async with Riko.from_config(cfg) as r:
        await trace(r, out, args.interval, args.idle_stop, args.wait,
                    tuple(args.feed) if args.feed else None)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\ninterrupted")
