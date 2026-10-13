#!/usr/bin/env python3
"""
riko_tsl_probe.py — try to fetch the Riko's thing model (TSL) from the
Aliyun Living Link gateway. Read-only: every call here is a "get".

Run in the same venv / with the same env vars as riko_discover.py.
Writes any successful response to riko_capture/tsl_<path>.json.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from neakasa_litterbox_sdk import LoginResult, NeakasaClient, Region

OUT = Path("riko_capture")
SESSION_FILE = OUT / ".session.json"

# (path, api_version, payload-builder). Ordered from most to least likely.
CANDIDATES = [
    ("/thing/tsl/get",              "1.0.0", lambda d: {"iotId": d.iot_id}),
    ("/thing/tsl/get",              "1.0.2", lambda d: {"iotId": d.iot_id}),
    ("/thing/tsl/get",              "1.0.4", lambda d: {"iotId": d.iot_id}),
    ("/thing/tsl/getByPk",          "1.0.0", lambda d: {"productKey": d.product_key}),
    ("/thing/model/get",            "1.0.0", lambda d: {"iotId": d.iot_id}),
    ("/thing/abilities/get",        "1.0.0", lambda d: {"iotId": d.iot_id}),
    ("/thing/abilities/get",        "1.0.4", lambda d: {"iotId": d.iot_id}),
    ("/thing/productInfo/get",      "1.0.0", lambda d: {"iotId": d.iot_id}),
    ("/thing/productInfo/get",      "1.1.2", lambda d: {"iotId": d.iot_id}),
    ("/thing/info/get",             "1.0.0", lambda d: {"iotId": d.iot_id}),
    ("/thing/info/get",             "1.0.4", lambda d: {"iotId": d.iot_id}),
    ("/thing/detailInfo/queryProductInfoByIotIds", "1.0.0",
        lambda d: {"iotIds": [d.iot_id]}),
    ("/thing/event/get",            "1.0.0", lambda d: {"iotId": d.iot_id}),
    ("/thing/events/get",           "1.0.0", lambda d: {"iotId": d.iot_id}),
]


async def main() -> int:
    email, password = os.environ.get("NEAKASA_EMAIL"), os.environ.get("NEAKASA_PASSWORD")
    if not email or not password:
        print("Set NEAKASA_EMAIL and NEAKASA_PASSWORD.", file=sys.stderr)
        return 2
    OUT.mkdir(exist_ok=True)

    async with NeakasaClient(email=email, password=password, region=Region.US) as client:
        cached = None
        if SESSION_FILE.exists():
            try:
                cached = LoginResult.from_dict(json.loads(SESSION_FILE.read_text()))
            except Exception:
                pass
        result = await client.login(cached=cached)
        if result is not cached:
            SESSION_FILE.write_text(json.dumps(result.to_dict()))

        devices = await client.list_devices()
        riko = next((d for d in devices if d.product_name.lower().startswith("riko")), None)
        if riko is None:
            print("No Riko on this account.", file=sys.stderr)
            return 1
        print(f"Probing {riko.product_name} iot_id={riko.iot_id} product_key={riko.product_key}\n")

        hits = 0
        for path, version, build in CANDIDATES:
            label = f"{path} v{version}"
            try:
                data = await client._aliyun_call_authed(  # noqa: SLF001
                    path,
                    api_version=version,
                    payload=build(riko),
                    language="en-US",
                    context=label,
                )
            except Exception as exc:
                msg = str(exc).replace("\n", " ")
                print(f"  MISS  {label:<50} {msg[:110]}")
                continue
            hits += 1
            fname = OUT / ("tsl_" + path.strip("/").replace("/", "_") + f"_v{version}.json")
            fname.write_text(json.dumps(data, indent=2, default=str))
            print(f"  HIT   {label:<50} -> {fname}")
            # Quick peek at anything that looks like a TSL.
            body = data
            if isinstance(body, str):
                try:
                    body = json.loads(body)
                except ValueError:
                    pass
            if isinstance(body, dict):
                for section in ("properties", "services", "events"):
                    items = body.get(section)
                    if isinstance(items, list):
                        ids = [i.get("identifier") for i in items if isinstance(i, dict)]
                        print(f"        {section} ({len(ids)}): {', '.join(str(i) for i in ids)}")
        print(f"\n{hits} hit(s). If zero, the thing model isn't exposed on these paths and "
              f"we'll get identifiers from the app traffic instead.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
