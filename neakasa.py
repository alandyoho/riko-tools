#!/usr/bin/env python3
"""
neakasa.py — talk to Neakasa's OWN backend (usapi.neakasapet.com), separate from
the Aliyun IoT channel that riko.py uses.

This is where the intake ledger lives: per-meal planned vs actual grams, failure
records with reason codes, and the cat's eat sessions — none of which crosses the
Aliyun channel. See the captured `/api/feeder/record` response, 2026-09-07.

WHAT'S KNOWN (from intercepting the iOS app, 2026-09-07):
  * Login is POST /api/login/user with {account, password, ...} where password is
    a plain md5 hex of the real password. Returns login_token + user_info.
  * The ledger is GET /api/feeder/record with a token/uid/sign header triple.
  * The `sign` header does NOT cover the query params on /feeder/record: a captured
    sign kept working after start_time was changed. So sign is either fixed or
    covers only some stable field.

WHAT'S UNKNOWN / THE OPEN TEST:
  * The login request also carries `sign` and a pre-login `token`, computed by the
    app with a key we can't extract (iOS). This script tests whether login enforces
    them: run with --login-sign / --login-token captured from the app, and also
    without, and see which succeeds. If login works WITHOUT a valid sign, we can
    authenticate from scratch with just email + md5(password) and the whole ledger
    API becomes self-sufficient. If it doesn't, we fall back to pasting a captured
    ledger token into config (still read-only-useful, but the token expires).

Nothing here writes to the device. Read-only.

USAGE
    # test login (needs captured sign/token headers to try the "with" case)
    python3 neakasa.py login --sign 'ZgmN...=' --token '45hd...='
    python3 neakasa.py login            # no sign/token — does the bare login work?

    # read the ledger with a captured header triple (from mitmproxy)
    python3 neakasa.py ledger --token 'Bzjf...==' --uid 'TMe8...==' --sign '69gi...=' \\
        --device WL03002G26270010566 --user 400133257 --days 7

Credentials for login come from RIKO config ([account] email/password) or
--account / --password.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.parse
import urllib.request
from typing import Any

BASE = "https://usapi.neakasapet.com"
APPID = "32711645"
APP_VERSION = "203060001"
PRODUCT_ID = "a123nCqsrQm3vEbt"   # from the captured login body
UA = "Neakasa/203060001 CFNetwork/3860.700.1 Darwin/25.6.0"


def md5_hex(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def _request(method: str, path: str, *, headers: dict[str, str],
             body: bytes | None = None, query: dict[str, Any] | None = None) -> dict[str, Any]:
    url = BASE + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        print(f"HTTP {exc.code}", file=sys.stderr)
    try:
        return json.loads(raw)
    except ValueError:
        return {"_raw": raw.decode("utf-8", "replace")}


def login(account: str, password: str, *, sign: str | None,
          token: str | None) -> dict[str, Any]:
    """POST /api/login/user. sign/token are the app-computed headers; pass them to
    test the 'with valid headers' case, omit to test whether they're enforced."""
    body_obj = {
        "system_number": "iPhone18,1",
        "app_version": "2.3.6",
        "product_id": PRODUCT_ID,
        "password": md5_hex(password),
        "system": 1,
        "account": account,
        "type": 3,
        "system_version": "26.6.1",
    }
    body = json.dumps(body_obj).encode()
    ts = str(int(time.time()))
    headers = {
        "content-type": "application/json",
        "appid": APPID,
        "request-id": ts,
        "uid": "",
        "version": APP_VERSION,
        "timestamp": ts,
        "accept": "*/*",
        "brand": "iPhone",
        "accept-language": "en",
        "user-agent": UA,
        "model": "iPhone18,1",
    }
    if token is not None:
        headers["token"] = token
    if sign is not None:
        headers["sign"] = sign
    return _request("POST", "/api/login/user", headers=headers, body=body)


def ledger(*, token: str, uid: str, sign: str, device: str, user: int,
           start: int, end: int) -> dict[str, Any]:
    ts = str(int(time.time()))
    headers = {
        "content-type": "application/x-www-form-urlencoded;charset=utf-8",
        "appid": APPID,
        "request-id": ts,
        "uid": uid,
        "version": APP_VERSION,
        "timestamp": ts,
        "accept": "*/*",
        "brand": "iPhone",
        "accept-language": "en",
        "token": token,
        "user-agent": UA,
        "model": "iPhone18,1",
        "sign": sign,
    }
    query = {
        "data_type": 0, "device_name": device, "start_time": start,
        "user_id": user, "bind_status": 1, "end_time": end,
    }
    return _request("GET", "/api/feeder/record", headers=headers, query=query)


REASON = {0: "ok", 1: "pump stall / nozzle"}  # extend as we see more


def print_ledger(data: dict[str, Any]) -> None:
    d = data.get("data", {})
    feeds = d.get("feed_list", [])
    eats = d.get("eat_list", [])
    print(f"\n{len(feeds)} feed record(s):")
    print(f"  {'time':<20} {'way':<7} {'food':>10} {'water':>11} {'status':<8}")
    for f in feeds:
        t = time.strftime("%Y-%m-%d %H:%M", time.localtime(f["feed_time"]))
        food = f"{f['feed_weight']}/{f['plan_feed_weight']}g"
        water = f"{f['water_weight']}/{f['plan_water_weight']}g"
        st = "ok" if f["status"] == 1 else "FAILED"
        try:
            reason = json.loads(f.get("fail_reason", "{}")).get("reason", 0)
        except ValueError:
            reason = 0
        note = "" if st == "ok" else f" ({REASON.get(reason, reason)})"
        print(f"  {t:<20} {f['way']:<7} {food:>10} {water:>11} {st}{note}")
    if eats:
        print(f"\n{len(eats)} eat session(s):")
        for e in eats:
            s = time.strftime("%H:%M", time.localtime(e["start_time"]))
            en = time.strftime("%H:%M", time.localtime(e["end_time"]))
            dur = (e["end_time"] - e["start_time"]) / 60
            print(f"  {s}-{en} ({dur:.0f} min): ate {e['eat_weight']}g, {e['left_weight']}g left")
    # over-delivery check
    served = [f for f in feeds if f["status"] == 1]
    if served:
        tot = sum(f["feed_weight"] + f["water_weight"] for f in served)
        plan = sum(f["plan_feed_weight"] + f["plan_water_weight"] for f in served)
        print(f"\nledger totals (device's own figures): {tot}g delivered vs {plan}g planned")


def main() -> int:
    ap = argparse.ArgumentParser(description="Neakasa backend client (read-only)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    lg = sub.add_parser("login", help="test whether login works, with or without app headers")
    lg.add_argument("--account"); lg.add_argument("--password")
    lg.add_argument("--sign", default=None, help="app-computed sign header (omit to test enforcement)")
    lg.add_argument("--token", default=None, help="app-computed pre-login token (omit to test enforcement)")

    ld = sub.add_parser("ledger", help="read the feeding ledger with a captured token triple")
    ld.add_argument("--token", required=True)
    ld.add_argument("--uid", required=True)
    ld.add_argument("--sign", required=True)
    ld.add_argument("--device", required=True)
    ld.add_argument("--user", type=int, required=True)
    ld.add_argument("--days", type=int, default=1, help="how many days back (default 1)")
    ld.add_argument("--raw", action="store_true", help="dump raw JSON")

    args = ap.parse_args()

    if args.cmd == "login":
        account, password = args.account, args.password
        if not account or not password:
            try:
                from config import load as load_config
                cfg = load_config(); cfg.require_credentials()
                account = account or cfg.email
                password = password or cfg.password
            except Exception:
                pass
        if not account or not password:
            print("need --account and --password (or RIKO config)", file=sys.stderr)
            return 2
        print(f"login as {account}, md5(pw)={md5_hex(password)[:8]}…, "
              f"sign={'yes' if args.sign else 'OMITTED'}, token={'yes' if args.token else 'OMITTED'}")
        resp = login(account, password, sign=args.sign, token=args.token)
        code = resp.get("code")
        if code == 0:
            info = resp.get("data", {}).get("user_info", {})
            print(f"  SUCCESS — code 0, ali_user_id={info.get('ali_user_id')}, "
                  f"login_count={info.get('login_count')}")
            print("  (if sign was OMITTED and this succeeded, login doesn't enforce it)")
        else:
            print(f"  code={code} message={resp.get('message')!r}")
            print("  (rejected — login likely enforces the sign/token headers)")
        return 0

    if args.cmd == "ledger":
        now = int(time.time())
        start = now - args.days * 86400
        resp = ledger(token=args.token, uid=args.uid, sign=args.sign,
                      device=args.device, user=args.user, start=start, end=now)
        if args.raw:
            print(json.dumps(resp, indent=2))
            return 0
        if resp.get("code") != 0:
            print(f"code={resp.get('code')} message={resp.get('message')!r}", file=sys.stderr)
            print("token may have expired — re-capture from the app", file=sys.stderr)
            return 1
        print_ledger(resp)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
