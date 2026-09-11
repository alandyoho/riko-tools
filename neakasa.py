#!/usr/bin/env python3
"""
neakasa.py — read the Riko intake ledger from Neakasa's feeder backend,
self-sufficiently (login only, no captured token).

THE KEY THING: the ledger is authorized by the `user_id` PARAMETER plus the
device share — NOT by which account you authenticate as. So you can log in with
your normal automation account and read the feeder owner's records by passing the
owner's user_id (see ledger(..., owner_user_id=...) / --owner-id / [feeder]
feeder_owner_id in config). You only need to "be" the owner if you aren't a shared
user of the device at all. (We learned this the hard way — earlier notes said you
had to log in as the owner; that was wrong.)

Auth (all reproducible from public values in the neakasa-litterbox-sdk):
  appid  = 32715650
  sign   = base64(HMAC-SHA256(SECRET, appid+timestamp)).UPPER()   in sign + request-id
  uid    = aes_encrypt_with_boot_key(str(ali_user_id))
  token  = aes_encrypt("userToken@<seconds>.<millis>", session_aes_key, session_aes_iv)
where SECRET, the boot key, and the session aes_key/iv all come from the SDK login.

Endpoints (host usapi.neakasapet.com):
  /api/feeder/record             — per-meal records + eat sessions
  /api/feeder/record/statistics  — aggregates

USAGE
  python3 neakasa.py ledger  --email feederowner@example.com --days 7
  python3 neakasa.py intake  --email feederowner@example.com --days 7
  python3 neakasa.py raw     --email feederowner@example.com --days 2
  # password prompted if not given; --device defaults to the only Riko found
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import getpass
import hashlib
import hmac
import json
import sys
import time
import urllib.parse

try:
    import aiohttp
    from neakasa_litterbox_sdk import NeakasaClient, Region
    from neakasa_litterbox_sdk.crypto import aes_encrypt, aes_encrypt_with_boot_key
except ImportError as exc:
    sys.exit(f"missing dependency ({exc}); try: pip install neakasa-litterbox-sdk aiohttp")

APPID  = "32715650"
SECRET = "698ee0ef531c3df2ddded87563643860"
BASE   = "https://usapi.neakasapet.com"
UA     = "okhttp/4.12.0"
FAIL_REASON = {0: "ok", 1: "pump stall (nozzle)", 2: "dispense failure", 5: "grinder"}


def _sign(ts: str) -> str:
    return base64.b64encode(
        hmac.new(SECRET.encode(), (APPID + ts).encode(), hashlib.sha256).digest()
    ).decode().upper()


def _token(user_token: str, aes_key: str, aes_iv: str) -> str:
    now = time.time()
    plain = f"{user_token}@{int(now)}.{int((now % 1) * 1000):03d}"
    return aes_encrypt(plain, aes_key.encode(), aes_iv.encode())


class Feeder:
    def __init__(self, email: str, password: str, region: str = "US"):
        self._c = NeakasaClient(email=email, password=password, region=Region[region])
        self._s = None

    async def __aenter__(self):
        await self._c.__aenter__()
        self._s = await self._c.login()
        return self

    async def __aexit__(self, *a):
        await self._c.__aexit__(*a)

    @property
    def user_id(self) -> int:
        return self._s.user_info.ali_user_id

    def _headers(self) -> dict:
        ts = str(int(time.time()))
        sg = _sign(ts)
        return {
            "appid": APPID, "request-id": sg, "sign": sg,
            "uid": aes_encrypt_with_boot_key(str(self.user_id)),
            "token": _token(self._s.user_token, self._s.aes_key, self._s.aes_iv),
            "timestamp": ts, "version": "203030001", "accept": "*/*",
            "Accept-Language": "en", "brand": "google", "Charset": "UTF-8",
            "model": "sdk_gphone64_arm64", "user-agent": UA,
            "Content-Type": "application/x-www-form-urlencoded",
        }

    async def _get(self, path: str, params: dict) -> dict:
        url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=self._headers(), timeout=20) as r:
                data = json.loads(await r.text())
        if data.get("code") != 0:
            raise RuntimeError(f"{path}: code={data.get('code')} {data.get('message')!r}")
        return data.get("data", {})

    async def find_device(self) -> str:
        devs = await self._c.list_devices()
        for d in devs:
            if "riko" in d.product_name.lower():
                return d.device_name
        if devs:
            return devs[0].device_name
        raise RuntimeError("no devices on this account")

    async def ledger(self, device: str, days: int = 1,
                     owner_user_id: int | None = None) -> dict:
        """Read the feeder ledger.

        KEY FACT: the ledger is keyed off the `user_id` PARAMETER, not the
        authenticated account. A shared account (device shared to it) can read the
        owning account's records by passing the owner's user_id here — which is what
        the app does when logged into a shared account. So you can run everything on
        the automation account and just pass the feeder owner's id.
        """
        now = int(time.time())
        return await self._get("/api/feeder/record", {
            "data_type": 0, "device_name": device,
            "start_time": now - days * 86400, "end_time": now,
            "user_id": owner_user_id if owner_user_id is not None else self.user_id,
            "bind_status": 1,
        })


def print_ledger(d: dict) -> None:
    feeds = d.get("feed_list", [])
    eats = d.get("eat_list", [])
    print(f"\n{len(feeds)} feed record(s)")
    print(f"  {'time':<17}{'way':<8}{'food':>10}{'water':>11}  result")
    for f in feeds:
        t = time.strftime("%m-%d %H:%M", time.localtime(f["feed_time"]))
        food = f"{f['feed_weight']}/{f['plan_feed_weight']}g"
        water = f"{f['water_weight']}/{f['plan_water_weight']}g"
        if f["status"] == 1:
            res = "ok"
        else:
            try:
                rc = json.loads(f.get("fail_reason", "{}")).get("reason", 0)
            except ValueError:
                rc = 0
            res = f"FAILED ({FAIL_REASON.get(rc, rc)})"
        print(f"  {t:<17}{f['way']:<8}{food:>10}{water:>11}  {res}")
    if eats:
        print(f"\n{len(eats)} eat session(s)")
        for e in eats:
            s = time.strftime("%m-%d %H:%M", time.localtime(e["start_time"]))
            mins = (e["end_time"] - e["start_time"]) / 60
            print(f"  {s}  {mins:>4.0f}min  ate {e['eat_weight']}g, {e['left_weight']}g left")
    print_intake(d)


def print_intake(d: dict) -> None:
    served = [f for f in d.get("feed_list", []) if f["status"] == 1]
    failed = [f for f in d.get("feed_list", []) if f["status"] != 1]
    if served:
        fd = sum(f["feed_weight"] for f in served); fp = sum(f["plan_feed_weight"] for f in served)
        wd = sum(f["water_weight"] for f in served); wp = sum(f["plan_water_weight"] for f in served)
        print(f"\ndelivered vs planned: food {fd}/{fp}g, water {wd}/{wp}g "
              f"({len(served)} feeds)")
        if fp: print(f"  food delivery: {100*fd/fp:.0f}% of plan")
    if failed:
        from collections import Counter
        reasons = Counter()
        for f in failed:
            try: reasons[json.loads(f.get('fail_reason','{}')).get('reason',0)] += 1
            except ValueError: reasons[0]+=1
        print(f"failed feeds: {len(failed)} — " +
              ", ".join(f"{FAIL_REASON.get(k,k)}×{v}" for k,v in reasons.items()))
    eats = d.get("eat_list", [])
    if eats:
        print(f"measured intake: {sum(e['eat_weight'] for e in eats)}g over {len(eats)} session(s)")


async def _main() -> int:
    ap = argparse.ArgumentParser(description="Read the Riko intake ledger (self-sufficient).")
    ap.add_argument("cmd", choices=["ledger", "intake", "raw"])
    ap.add_argument("--email", help="account to authenticate as "
                    "(default: [account] in riko.toml — your automation account)")
    ap.add_argument("--password")
    ap.add_argument("--device", help="device_name; default: the only Riko on the account")
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--owner-id", type=int,
                    help="feeder owner's user_id, if reading as a shared account "
                         "(default: the authenticated account's own id)")
    ap.add_argument("--region", default="US", choices=["US", "EU", "AP"])
    args = ap.parse_args()

    email, pw, owner = args.email, args.password, args.owner_id
    # default to the main automation [account]; owner id from [feeder] if not passed
    try:
        from config import load as load_config
        cfg = load_config()
        email = email or cfg.email
        pw = pw or cfg.password
        if owner is None and cfg.feeder_owner_id:
            owner = cfg.feeder_owner_id
    except Exception:
        pass
    if not email:
        print("no account email (set [account] or pass --email)", file=sys.stderr); return 2
    if not pw:
        pw = getpass.getpass(f"Neakasa password for {email}: ")

    async with Feeder(email, pw, args.region) as f:
        dev = args.device or await f.find_device()
        d = await f.ledger(dev, days=args.days, owner_user_id=owner)
        if args.cmd == "raw":
            print(json.dumps(d, indent=2))
        elif args.cmd == "intake":
            print_intake(d)
        else:
            print_ledger(d)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_main()))
    except RuntimeError as e:
        print(e, file=sys.stderr); sys.exit(1)
    except KeyboardInterrupt:
        pass
