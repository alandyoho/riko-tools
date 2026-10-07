#!/usr/bin/env python3
"""
topup.py — work out how much to serve when the last meal wasn't finished.

The feeder serves every scheduled meal in full, on top of whatever is still in
the bowl. When the cat sleeps through the midnight or 4 am meal, the next one
stacks on it (91 g in the bowl on 2026-10-07). Instead, monitor.py shrinks the
next meal so the bowl ends up holding one full meal: it rewrites that slot's
amounts in the schedule shortly before the feeder starts preparing, and puts
the original amounts back once the meal has been served.

This file is the arithmetic only — no network, no device. monitor.py owns the
schedule writes and the restore.

Where the numbers come from (Neakasa's intake ledger, see neakasa.py):
  * every feed record carries `left_weight`: grams in the bowl right after
    serving (from firmware 1.0.0-0033; 0 on older records and on failed feeds);
  * eating is logged in 10-minute windows as "ate X, left Y".
So the bowl's contents now are simply the most recent of those readings.
Checked against 70 meals: the leftover computed this way matched what the
feeder weighed at the next serving to within 5 g in 63 of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

EMPTY_G = 10          # at or below this the bowl counts as empty (readings are good to ~5 g)
MIN_FOOD_G = 1        # smallest food amount to schedule; below it the meal is skipped


@dataclass(frozen=True)
class Bowl:
    grams: float              # in the bowl now
    as_of: int                # unix time of the reading it comes from
    emptied_at: int | None    # last time the bowl was (near) empty; None if not seen in the ledger


def bowl_now(ledger: dict[str, Any]) -> Bowl | None:
    """Current bowl contents from a ledger dict (feed_list + eat_list). None if the
    ledger holds no usable reading."""
    events: list[tuple[int, int, dict]] = []
    for f in ledger.get("feed_list", []):
        events.append((int(f.get("feed_time", 0)), 0, f))
    for e in ledger.get("eat_list", []):
        # the "left" figure is measured at the END of the eating window
        events.append((int(e.get("end_time") or e.get("start_time", 0)), 1, e))
    events.sort(key=lambda x: (x[0], x[1]))

    grams: float | None = None
    as_of = 0
    emptied_at: int | None = None
    for ts, kind, rec in events:
        if kind == 1:
            grams = float(rec.get("left_weight", 0))
        elif rec.get("left_weight", 0) > 0:
            if grams is not None and grams <= EMPTY_G:
                emptied_at = ts            # it was empty right up to this serving
            grams = float(rec["left_weight"])
        elif grams is not None:
            # no bowl reading on this record (a failed or water-only feed): add what it delivered
            grams += float(rec.get("feed_weight", 0)) + float(rec.get("water_weight", 0))
        else:
            continue
        as_of = ts
        if grams <= EMPTY_G:
            emptied_at = ts
    if grams is None:
        return None
    return Bowl(grams=grams, as_of=as_of, emptied_at=emptied_at)


@dataclass(frozen=True)
class Plan:
    action: str        # "full" (serve the meal as scheduled), "topup", or "skip"
    food_g: int = 0    # for "topup": what to schedule instead
    water_g: int = 0
    why: str = ""


def plan_topup(food_g: float, water_g: float, leftover_g: float) -> Plan:
    """What to serve so the bowl ends up holding one full meal (food_g + water_g),
    keeping the meal's own food:water ratio. Food is whole grams, so the result is
    the nearest step and never more than the scheduled meal."""
    target = food_g + water_g
    if food_g <= 0 or target <= 0:
        return Plan("full", why="not a food meal")
    if leftover_g <= EMPTY_G:
        return Plan("full", why=f"bowl is empty ({leftover_g:.0f} g)")
    need = target - leftover_g
    ratio = water_g / food_g
    food = round(need / (1 + ratio))
    if food >= food_g:
        return Plan("full", why=f"{leftover_g:.0f} g left is less than one step")
    if food < MIN_FOOD_G:
        return Plan("skip", why=f"{leftover_g:.0f} g left — the bowl already holds a meal")
    return Plan("topup", food_g=int(food), water_g=int(round(food * ratio)),
                why=f"{leftover_g:.0f} g left of a {target:.0f} g meal")
