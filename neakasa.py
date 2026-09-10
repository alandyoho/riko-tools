#!/usr/bin/env python3
"""
neakasa.py — client for Neakasa's OWN backend, where the intake ledger lives.

This is the second of the two clouds the Riko uses. riko.py covers the Aliyun IoT
channel (device control and state). This covers usapi.neakasapet.com / us.neakasa.com,
which holds the data the device channel never carries:

  * per-meal planned vs actual grams
  * failure records with reason codes
  * eat sessions ("ate 34 g, 0 g left") — the only measure of what the cat consumed
  * the cat profile and food database

PREVIOUSLY BLOCKED, NOW NOT
Every request here needs three app-computed headers: `sign`, `uid` and `token`. I spent
a long time trying to extract the keys from the iOS app (Flutter, AES ciphertext, no
luck) before realising the open-source neakasa-litterbox-sdk already ships them,
extracted from the Android build. The schemes:

    sign  = base64(HMAC-SHA256(app_secret, app_key + timestamp)).upper()
    uid   = base64(AES-CBC-NoPadding(user_id, BOOT_KEY, BOOT_IV))
    token = base64(AES-CBC-NoPadding("<userToken>@<epoch_ms>", aesKey, aesIv))

That last one is why the server once told us "token does not contain the @ sign" — the
plaintext really is `userToken@timestamp`. Password is md5(md5(plaintext)), double.

Rather than copy those constants here, this module imports them from the installed
SDK. It's the SDK's work, it stays current if the SDK updates, and it keeps this file
honest about where the values came from.

USAGE
    from neakasa import NeakasaBackend
    async with NeakasaBackend(email, password) as nb:
        led = await nb.ledger(device_name, days=2)
        nb.print_ledger(led)

    python3 neakasa.py ledger --device WL0300... --days 2
    python3 neakasa.py intake --device WL0300... --days 7
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any

try:
    from neakasa_litterbox_sdk import NeakasaClient, Region
except ImportError as exc:  # pragma: no cover
    sys.exit(f"Missing dependency ({exc}). Try: pip install neakasa-litterbox-sdk")

# fail_reason.reason values seen in the wild
FAIL_REASON = {0: "ok", 1: "pump stall (nozzle)"}


class NeakasaBackend:
    """Authenticated access to Neakasa's app backend."""

    def __init__(self, email: str, password: str, *, region: str = "US") -> None:
        self._email = email
        self._password = password
        self._region = region
        self._client = NeakasaClient(email=email, password=password,
                                     region=Region[region])
        self._login: Any = None

    async def __aenter__(self) -> "NeakasaBackend":
        await self._client.__aenter__()
        self._login = await self._client.login()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._client.__aexit__(*exc)

    # --- auth -------------------------------------------------------------
    @property
    def user_id(self) -> int:
        return self._login.user_info.ali_user_id

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """Issue an authenticated GET via the SDK's own transport.

        Delegating rather than hand-rolling the headers. The SDK implements two
        distinct schemes and they must not be mixed: pre-login requests carry
        `appId` + `sign`, while authenticated ones replace those with `uid` + `token`
        and put the HMAC in `request-id`. Sending both at once gets a bare
        `code=1001 SystemError` with no hint as to why. The server also derives the
        user from the `uid` header, so no `user_id` parameter is sent, and every
        value goes as a string.
        """
        return await self._client._authenticated_get(          # noqa: SLF001
            path,
            {k: str(v) for k, v in params.items()},
            context=path,
        )

    # --- endpoints --------------------------------------------------------
    async def ledger(self, device_name: str, *, days: int = 1,
                     start: int | None = None, end: int | None = None) -> dict[str, Any]:
        """Feeding records + eat sessions + cat profile for a time range."""
        now = int(time.time())
        return await self._get("/feeder/record", {
            "data_type": 0,
            "device_name": device_name,
            "start_time": start if start is not None else now - days * 86400,
            "end_time": end if end is not None else now,
            "bind_status": 1,
        })

    # --- presentation -----------------------------------------------------
    @staticmethod
    def print_ledger(data: dict[str, Any]) -> None:
        feeds = data.get("feed_list", [])
        eats = data.get("eat_list", [])
        print(f"\n{len(feeds)} feed record(s)")
        print(f"  {'time':<17} {'way':<7} {'food':>11} {'water':>12}  result")
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
            print(f"  {t:<17} {f['way']:<7} {food:>11} {water:>12}  {res}")

        if eats:
            print(f"\n{len(eats)} eat session(s)")
            for e in eats:
                s = time.strftime("%m-%d %H:%M", time.localtime(e["start_time"]))
                mins = (e["end_time"] - e["start_time"]) / 60
                print(f"  {s}  {mins:>4.0f} min   ate {e['eat_weight']}g, "
                      f"{e['left_weight']}g left")
        else:
            print("\nNo eat sessions recorded.")
            print("  (These only appear if the bowl is left undisturbed after serving —")
            print("   lifting it to weigh cancels the measurement.)")

        NeakasaBackend.print_intake(data)

    @staticmethod
    def print_intake(data: dict[str, Any]) -> None:
        """Summarise delivered vs planned and what the cat actually ate."""
        served = [f for f in data.get("feed_list", []) if f["status"] == 1]
        failed = [f for f in data.get("feed_list", []) if f["status"] != 1]
        if served:
            food = sum(f["feed_weight"] for f in served)
            plan = sum(f["plan_feed_weight"] for f in served)
            water = sum(f["water_weight"] for f in served)
            wplan = sum(f["plan_water_weight"] for f in served)
            print(f"\nDelivered (device's own figures): {food}g food of {plan}g planned, "
                  f"{water}g water of {wplan}g planned")
        if failed:
            print(f"Failed feeds: {len(failed)}")
        eats = data.get("eat_list", [])
        if eats:
            print(f"Measured intake: {sum(e['eat_weight'] for e in eats)}g "
                  f"across {len(eats)} session(s)")
            print("  Note: each session is a single sample ~10 min after serving, so "
                  "anything eaten later isn't counted.")


async def _main() -> int:
    ap = argparse.ArgumentParser(description="Neakasa app-backend client")
    ap.add_argument("cmd", choices=["ledger", "intake", "raw"])
    ap.add_argument("--device", required=True, help="device_name, e.g. WL0300...")
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--email"); ap.add_argument("--password")
    ap.add_argument("--region", default="US", choices=["US", "EU", "AP"])
    args = ap.parse_args()

    email, password = args.email, args.password
    if not (email and password):
        try:
            from config import load as load_config
            cfg = load_config(); cfg.require_credentials()
            email, password = email or cfg.email, password or cfg.password
        except Exception:
            pass
    if not (email and password):
        print("need --email/--password or a riko config", file=sys.stderr)
        return 2

    async with NeakasaBackend(email, password, region=args.region) as nb:
        data = await nb.ledger(args.device, days=args.days)
        if args.cmd == "raw":
            print(json.dumps(data, indent=2))
        elif args.cmd == "intake":
            nb.print_intake(data)
        else:
            nb.print_ledger(data)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_main()))
    except KeyboardInterrupt:
        pass
