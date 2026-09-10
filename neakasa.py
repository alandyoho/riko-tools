#!/usr/bin/env python3
"""
neakasa.py — read the Riko's intake ledger from Neakasa's feeder backend.

Two Neakasa clouds exist. The litter-box backend (us.neakasa.com) is fully accessible
via the open-source SDK. The FEEDER backend (usapi.neakasapet.com) — which holds the
intake ledger, eat sessions, and per-meal actual-vs-planned grams — is not: its
requests are signed with an HMAC secret specific to the pet-feeder app (app key
32711645), and that secret is NOT extractable from the app binary. Confirmed across
two APK versions (2.3.2, 2.3.3) and ~84,000 candidate strings against a known-good
oracle: it isn't stored as a plain string. It's assembled at runtime or held as raw
bytes, reachable only by hooking the live HMAC call (Frida, an Android emulator).

WHAT THIS MEANS FOR USE
This module therefore CANNOT log in to the feeder backend on its own. It reads the
ledger using a `token` / `uid` / `sign` triple captured from the phone app (via a
proxy like mitmproxy). That triple works until the token expires — typically hours —
after which you re-capture. It's a manual step, but the ledger is read-only and
nothing operational depends on it; this is for occasional analysis (confirming the
food overshoot, pulling intake history) rather than live monitoring.

The device-control side (feeding, schedule, tare, errors) is fully self-sufficient
and lives in riko.py — none of that needs this.

CAPTURING THE TRIPLE
Run mitmproxy, open the app's feeding-history screen, find the GET to
usapi.neakasapet.com/api/feeder/record, and copy its `token`, `uid`, and `sign`
headers plus the `user_id` query param.

USAGE
    python3 neakasa.py ledger \
        --token 'Bzjf...==' --uid 'TMe8...==' --sign '69gi...=' \
        --device WL0300... --user 400133257 --days 2

    python3 neakasa.py intake  --token ... --uid ... --sign ... \
        --device WL0300... --user 400133257 --days 7
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from typing import Any

BASE = "https://usapi.neakasapet.com"
APPID = "32711645"
APP_VERSION = "203060001"
UA = "Neakasa/203060001 CFNetwork/3860.700.1 Darwin/25.6.0"

FAIL_REASON = {0: "ok", 1: "pump stall (nozzle)"}


def _get(path: str, headers: dict[str, str], query: dict[str, Any]) -> dict[str, Any]:
    url = f"{BASE}{path}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
    data = json.loads(raw)
    if data.get("code") != 0:
        msg = data.get("message")
        raise RuntimeError(
            f"{path}: code={data.get('code')} {msg!r}. "
            f"The token has most likely expired — re-capture it from the app.")
    return data.get("data", {})


def ledger(*, token: str, uid: str, sign: str, device: str, user: int,
           days: int = 1, start: int | None = None, end: int | None = None) -> dict[str, Any]:
    ts = str(int(time.time()))
    headers = {
        "content-type": "application/x-www-form-urlencoded;charset=utf-8",
        "appid": APPID, "request-id": ts, "uid": uid,
        "version": APP_VERSION, "timestamp": ts, "accept": "*/*",
        "brand": "iPhone", "accept-language": "en", "token": token,
        "user-agent": UA, "model": "iPhone18,1", "sign": sign,
    }
    now = int(time.time())
    query = {
        "data_type": 0, "device_name": device,
        "start_time": start if start is not None else now - days * 86400,
        "end_time": end if end is not None else now,
        "user_id": user, "bind_status": 1,
    }
    return _get("/api/feeder/record", headers, query)


def print_ledger(data: dict[str, Any]) -> None:
    feeds = data.get("feed_list", [])
    eats = data.get("eat_list", [])
    print(f"\n{len(feeds)} feed record(s)")
    print(f"  {'time':<15} {'way':<7} {'food':>11} {'water':>12}  result")
    for f in feeds:
        t = time.strftime("%m-%d %H:%M", time.localtime(f["feed_time"]))
        food = f"{f['feed_weight']}/{f['plan_feed_weight']}g"
        water = f"{f['water_weight']}/{f['plan_water_weight']}g"
        if f["status"] == 1:
            res = "ok"
        else:
            try:
                reason = json.loads(f.get("fail_reason", "{}")).get("reason", 0)
            except ValueError:
                reason = 0
            res = f"FAILED ({FAIL_REASON.get(reason, reason)})"
        print(f"  {t:<15} {f['way']:<7} {food:>11} {water:>12}  {res}")
    if eats:
        print(f"\n{len(eats)} eat session(s)")
        for e in eats:
            s = time.strftime("%m-%d %H:%M", time.localtime(e["start_time"]))
            mins = (e["end_time"] - e["start_time"]) / 60
            print(f"  {s}  {mins:>4.0f} min   ate {e['eat_weight']}g, {e['left_weight']}g left")
    else:
        print("\nNo eat sessions recorded — the bowl was moved after serving, or none happened.")
    print_intake(data)


def print_intake(data: dict[str, Any]) -> None:
    served = [f for f in data.get("feed_list", []) if f["status"] == 1]
    failed = [f for f in data.get("feed_list", []) if f["status"] != 1]
    if served:
        food = sum(f["feed_weight"] for f in served)
        plan = sum(f["plan_feed_weight"] for f in served)
        water = sum(f["water_weight"] for f in served)
        wplan = sum(f["plan_water_weight"] for f in served)
        print(f"\nDelivered (device's own figures): food {food}g of {plan}g planned, "
              f"water {water}g of {wplan}g planned")
    if failed:
        print(f"Failed feeds: {len(failed)}")
    eats = data.get("eat_list", [])
    if eats:
        print(f"Measured intake: {sum(e['eat_weight'] for e in eats)}g "
              f"across {len(eats)} session(s) — each a single ~10-min-post-serve sample.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Read the Riko intake ledger (captured token).")
    ap.add_argument("cmd", choices=["ledger", "intake", "raw"])
    ap.add_argument("--token", required=True, help="captured `token` header from the app")
    ap.add_argument("--uid", required=True, help="captured `uid` header")
    ap.add_argument("--sign", required=True, help="captured `sign` header")
    ap.add_argument("--device", required=True, help="device_name, e.g. WL0300...")
    ap.add_argument("--user", type=int, required=True, help="your user_id")
    ap.add_argument("--days", type=int, default=1)
    args = ap.parse_args()

    try:
        data = ledger(token=args.token, uid=args.uid, sign=args.sign,
                      device=args.device, user=args.user, days=args.days)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    if args.cmd == "raw":
        print(json.dumps(data, indent=2))
    elif args.cmd == "intake":
        print_intake(data)
    else:
        print_ledger(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
