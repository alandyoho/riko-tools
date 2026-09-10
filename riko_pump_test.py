#!/usr/bin/env python3
"""
riko_pump_test.py — measure how accurately the Riko delivers a requested amount
of water, using water-only feeds (feedOnce with food=0).

WHY THIS SHAPE
  1. The device's own scale can't be used mid-test: curWeight is cached and only
     refreshes when the bowl PHYSICALLY leaves and returns to the tray. A software
     bowlCtrl retract/extend does not trigger a sample (verified 2026-09-07), and
     the value sat unchanged through a 197 s dispense. So you weigh the bowl.
  2. We can't drive the pump directly either. waterProvide only runs with the tray
     retracted, but issuing bowlCtrl RETRACT outside a feed drops the device into
     state 6 "freshness protection" (errCode 1, FleshMgr) even with the freshness
     manager switched OFF -- and the pump won't run in that state. Two attempts at
     a direct pump test produced no water at all for this reason.

  A water-only feedOnce sidesteps both: the firmware handles the tray itself and
  runs its own pump sequence, exactly as it does for a real meal.

WHAT YOU LEARN
  * requested vs delivered grams, and whether the error is proportional (a rate
    problem) or a fixed offset (line fill / spin-up)
  * from the fit, how much a short priming pulse would actually deliver
  * whether the pump stalls on the first feed after an idle gap (the code-70
    drain-back theory) -- the script reports it if that happens

USAGE
    python3 riko_pump_test.py --dry-run
    python3 riko_pump_test.py --bowl-g 68.40
    python3 riko_pump_test.py --bowl-g 68.40 --amounts 12 24 36 24

BEFORE RUNNING
  * Clean, DRY bowl in the tray, weighed on your scale -> --bowl-g
  * Device IDLE. Note the water level; flow may vary with tank head, so a repeat
    at a lower level is worth doing later.
  * No food is dispensed. Empty the bowl when you're done.
  * Run it with `ssh -t` so the prompts work over SSH.
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


def ask_weight(prompt: str) -> float | None:
    while True:
        try:
            raw = input(f"{prompt} (g, blank to stop): ").strip()
        except EOFError:
            return None
        if raw == "" or raw.lower() in {"q", "quit", "stop"}:
            return None
        try:
            return float(raw.rstrip("g").strip())
        except ValueError:
            print("  not a number — try again, e.g. 80.15")


def fit(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    n = len(points)
    if n < 2:
        return None
    sx = sum(p[0] for p in points)
    sy = sum(p[1] for p in points)
    sxx = sum(p[0] * p[0] for p in points)
    sxy = sum(p[0] * p[1] for p in points)
    denom = n * sxx - sx * sx
    if denom == 0:
        return None
    slope = (n * sxy - sx * sy) / denom
    return slope, (sy - slope * sx) / n


async def water_feed(r: Riko, grams: float, timeout_s: float = 420.0) -> str:
    """Issue a water-only feed and wait for it to finish.

    Returns 'ok', or a description of the failure. Never calls feedCtrl END --
    that serves a partial meal and extends the tray (observed 2026-09-07).
    """
    await r.feed(0, grams)
    deadline = time.monotonic() + timeout_s
    seen_active = False
    while time.monotonic() < deadline:
        await asyncio.sleep(3)
        st = await r.status()
        if st.state in (FeederState.PREPARING, FeederState.SERVING):
            seen_active = True
        elif st.state == FeederState.SUSPENDED:
            code = ERROR_CODES.get(st.state_param, f"code {st.state_param}")
            return f"suspended: {code}"
        elif st.state == FeederState.FRESHNESS_PROTECT:
            return "device entered freshness protection"
        elif st.state == FeederState.IDLE and seen_active:
            await asyncio.sleep(5)
            return "ok"
    return "timed out waiting for the feed to finish"


async def main() -> int:
    ap = argparse.ArgumentParser(
        description="Measure Riko water delivery accuracy via water-only feeds")
    ap.add_argument("--amounts", type=float, nargs="+", default=[12, 24, 36, 24],
                    help="water grams to request per run (default: 12 24 36 24)")
    ap.add_argument("--bowl-g", type=float, default=None,
                    help="weight of the empty dry bowl, from your scale")
    ap.add_argument("--max-total", type=float, default=200.0,
                    help="abort if cumulative water exceeds this many grams")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    print("Plan: water-only feeds of " + ", ".join(f"{a:g}g" for a in args.amounts)
          + f"   ({sum(args.amounts):g}g requested in total)")
    print("Each run takes a couple of minutes. After each one: take the bowl out,")
    print("weigh it, put it back, and type the number.\n")
    if args.dry_run:
        print("dry run — nothing sent")
        return 0

    try:
        cfg = load_config()
        cfg.require_credentials()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    out = args.out or cfg.capture_dir / f"pumptest_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)

    async with Riko.from_config(cfg) as r:
        st = await r.status()
        if st.state != FeederState.IDLE:
            print(f"device is {st.state.name}, not idle — aborting", file=sys.stderr)
            return 1
        if not st.bowl_in:
            print("no bowl in the tray — put a clean dry bowl in first", file=sys.stderr)
            return 1
        print(f"water level: {st.water_level.name}   device reads {st.scale_g} g\n")

        baseline = args.bowl_g
        if baseline is None:
            baseline = ask_weight("Weigh the empty bowl now")
            if baseline is None:
                return 1
        print(f"baseline: {baseline:.2f} g\n")

        rows: list[tuple[float, float, float, float]] = []
        points: list[tuple[float, float]] = []
        prev = baseline
        stalls = 0

        for i, want in enumerate(args.amounts, 1):
            if prev - baseline > args.max_total:
                print(f"cumulative {prev - baseline:.1f} g exceeds --max-total; stopping")
                break
            print(f"[{i}/{len(args.amounts)}] requesting {want:g} g of water…", flush=True)
            result = await water_feed(r, want)
            if result != "ok":
                stalls += 1
                print(f"  FEED FAILED: {result}")
                print("  (this is a data point too — note how long the pump had been idle)")
                cont = input("  continue with the next run? [y/N]: ").strip().lower()
                if cont != "y":
                    break
                continue

            now = ask_weight(f"  Bowl weight after requesting {want:g} g")
            if now is None:
                print("  stopping early")
                break
            delta = now - prev
            err = delta - want
            print(f"  {prev:.2f} -> {now:.2f}  = {delta:+.2f} g delivered "
                  f"({err:+.2f} g vs requested, {100*delta/want:.0f}%)\n")
            rows.append((want, prev, now, delta))
            points.append((want, delta))
            prev = now

        if not rows:
            print("no successful measurements")
            return 1

        with out.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["requested_g", "before_g", "after_g", "delivered_g", "error_g", "pct"])
            for want, before, after, delta in rows:
                w.writerow([want, f"{before:.2f}", f"{after:.2f}", f"{delta:.2f}",
                            f"{delta-want:.2f}", f"{100*delta/want:.1f}"])

        print("--- results ---")
        print(f"{'requested':>10} {'delivered':>10} {'error':>8} {'pct':>6}")
        for want, _b, _a, delta in rows:
            print(f"{want:>10g} {delta:>10.2f} {delta-want:>+8.2f} {100*delta/want:>5.0f}%")
        total_req = sum(w for w, *_ in rows)
        total_del = prev - baseline
        print(f"\ntotal: {total_del:.2f} g delivered for {total_req:g} g requested "
              f"({100*total_del/total_req:.0f}%)")
        if stalls:
            print(f"stalled feeds: {stalls}")
        if (f := fit(points)):
            slope, intercept = f
            print(f"\nlinear fit: delivered = {slope:.3f} * requested {intercept:+.2f}")
            if abs(slope - 1) > 0.05:
                print(f"  -> proportional error: delivers {100*slope:.0f}% of what's asked")
            if abs(intercept) > 1:
                print(f"  -> fixed offset of {intercept:+.1f} g per feed "
                      f"(line fill / spin-up / residue)")
            if abs(slope - 1) <= 0.05 and abs(intercept) <= 1:
                print("  -> water delivery is accurate")
        print(f"\nCSV: {out}")
        print("Empty and dry the bowl when you're done.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\ninterrupted — check the device state and empty the bowl")
