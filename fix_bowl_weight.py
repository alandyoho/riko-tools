#!/usr/bin/env python3
"""
fix_bowl_weight.py — correct the Neakasa Riko's stored empty-bowl weight.

WHY THIS EXISTS
The Riko computes "food left in bowl" as (scale reading) minus (a stored empty-bowl
weight). It ships with that constant set to 65 g. The bowls in the box weigh about
68 g. So every reading carries ~3 g of phantom food — nearly half of an 8 g meal, and
enough to make the app report leftovers in a bowl you just washed.

The app's tare button does NOT fix this: it zeroes the platform and leaves the 65 g
alone. The underlying device command accepts the correct weight; the app simply never
sends one. This script sends it.

USAGE
    pip install neakasa-litterbox-sdk
    python3 fix_bowl_weight.py --email you@example.com --weight 68

    # weigh your own bowl first; --weight takes the real number
    # --dry-run shows what it would do without changing anything

BEFORE RUNNING
  * Weigh your empty, dry bowl on a kitchen scale. Use that number.
  * TAKE THE BOWL OFF THE TRAY before running. The command re-zeros the platform, so
    if the bowl is sitting on it the zero point captures the bowl and readings go
    badly wrong. Put the bowl back after.
  * Your bowls may differ by a gram; they're within rounding of each other, so one
    value covers all of them.

CAVEAT
The Neakasa phone app appears to overwrite this value with its own default (65) when
it syncs — observed after an app update. If your readings drift back, re-run this.

Unofficial. Not affiliated with Neakasa.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

try:
    from neakasa_litterbox_sdk import NeakasaClient, Region
except ImportError:
    sys.exit("Install the SDK first:  pip install neakasa-litterbox-sdk")


async def run(email: str, password: str, weight: int, region: str,
              device_name: str | None, dry_run: bool) -> int:
    async with NeakasaClient(email=email, password=password,
                             region=Region[region]) as client:
        await client.login()
        devices = await client.list_devices()
        if not devices:
            print("No devices on this account.", file=sys.stderr)
            return 1

        if device_name:
            match = [d for d in devices if d.device_name == device_name]
            if not match:
                print(f"No device named {device_name!r}. Devices on this account:", file=sys.stderr)
                for d in devices:
                    print(f"  {d.device_name}  ({d.product_name})", file=sys.stderr)
                return 1
        else:
            match = [d for d in devices if any(k in d.product_name.lower()
                     for k in ("riko", "feeder", "pet"))]
            if not match:
                match = devices
            if len(match) > 1:
                print(f"This account has {len(match)} devices. Pick one with "
                      f"--device-name <name>:", file=sys.stderr)
                for d in match:
                    print(f"  --device-name {d.device_name}   ({d.product_name})", file=sys.stderr)
                return 2

        dev = match[0]
        print(f"targeting device: {dev.device_name}")
        print(f"Device: {dev.product_name} ({dev.device_name})")

        # show the current stored value
        props = await client._get_properties(dev.device_name)   # noqa: SLF001
        bowl = props.get("bowlStatus", {})
        bowl = bowl.get("value", bowl) if isinstance(bowl, dict) else {}
        current = bowl.get("bowlGram")
        print(f"Stored empty-bowl weight: {current} g")
        if bowl.get("bBowlIn"):
            print("\n!! The device reports a bowl ON the tray.")
            print("   Take it off before running for real, or the zero point will be wrong.")
            if not dry_run:
                print("   Aborting. Remove the bowl and re-run.", file=sys.stderr)
                return 1

        if current == weight:
            print(f"Already set to {weight} g — nothing to do.")
            return 0

        if dry_run:
            print(f"[dry run] would set it to {weight} g")
            return 0

        print(f"Setting it to {weight} g…")
        await client._aliyun_call_authed(                       # noqa: SLF001
            "/thing/service/invoke",
            api_version="1.0.5",
            payload={"iotId": dev.iot_id, "identifier": "resetScaleZero",
                     "args": {"bowlGram": int(weight)}},
            language="en-US",
            context="resetScaleZero",
        )
        await asyncio.sleep(3)

        props = await client._get_properties(dev.device_name)   # noqa: SLF001
        bowl = props.get("bowlStatus", {})
        bowl = bowl.get("value", bowl) if isinstance(bowl, dict) else {}
        now = bowl.get("bowlGram")
        if now == weight:
            print(f"Done — now reads {now} g.")
            print("Put the bowl back on the tray. An empty bowl should now show ~0 g of food.")
            return 0
        print(f"Sent, but the device still reports {now} g. Try again in a moment.",
              file=sys.stderr)
        return 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Fix the Neakasa Riko's stored empty-bowl weight.")
    ap.add_argument("--email", required=True, help="your Neakasa account email")
    ap.add_argument("--password", help="prompted for if omitted")
    ap.add_argument("--weight", type=int, required=True,
                    help="your empty bowl's real weight in grams (weigh it)")
    ap.add_argument("--region", default="US", choices=["US", "EU", "AP"])
    ap.add_argument("--device-name", default=None,
                    help="only needed if you have more than one Neakasa device")
    ap.add_argument("--dry-run", action="store_true",
                    help="show the current value and stop")
    args = ap.parse_args()

    if not 0 <= args.weight <= 200:
        return print("--weight must be 0-200 g", file=sys.stderr) or 2

    password = args.password or getpass.getpass("Neakasa password: ")
    try:
        return asyncio.run(run(args.email, password, args.weight,
                               args.region, args.device_name, args.dry_run))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
