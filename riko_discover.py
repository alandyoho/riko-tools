#!/usr/bin/env python3
"""
riko_discover.py — step-1 discovery for the Neakasa Riko.

Logs into the Neakasa cloud with the (unofficial) neakasa-litterbox-sdk,
lists every device on the account, dumps the raw property map for each,
then sits on the MQTT push stream and logs *everything* the cloud sends —
property changes, events, and anything the M1-focused SDK would normally
drop — while you trigger feedings / open the app / etc.

Usage:
    pip install neakasa-litterbox-sdk
    export NEAKASA_EMAIL='you@example.com'
    export NEAKASA_PASSWORD='...'
    python3 riko_discover.py                 # snapshot + live watch
    python3 riko_discover.py --snapshot-only # just login + dump, then exit
    python3 riko_discover.py --poll 15       # also poll properties every 15s

Outputs (in ./riko_capture/):
    devices.json            everything list_devices() returned
    props_<device>.json     raw /thing/properties/get snapshot per device
    pushes.jsonl            one line per raw MQTT message (topic + body)
    poll_diffs.jsonl        property changes detected by polling (if --poll)

Tip: use a SECOND Neakasa account that the device has been shared to,
otherwise the phone app may keep logging you out.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from neakasa_litterbox_sdk import (
    LoginResult,
    NeakasaClient,
    Region,
    SessionExpiredError,
)

OUT = Path("riko_capture")
SESSION_FILE = OUT / ".session.json"

log = logging.getLogger("riko")


# ---------------------------------------------------------------- helpers
def now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str))


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    with path.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Return {key: {"old": ..., "new": ...}} for every changed/added/removed key."""
    out: dict[str, Any] = {}
    for k in set(old) | set(new):
        if old.get(k) != new.get(k):
            out[k] = {"old": old.get(k), "new": new.get(k)}
    return out


def unwrap_items(props: dict[str, Any]) -> dict[str, Any]:
    """Aliyun property maps are {key: {"value": v, "time": t}}. Flatten to {key: v}."""
    flat: dict[str, Any] = {}
    for k, v in props.items():
        if isinstance(v, dict) and "value" in v:
            flat[k] = v["value"]
        else:
            flat[k] = v
    return flat


# ---------------------------------------------------------------- login
async def login(client: NeakasaClient) -> None:
    cached = None
    if SESSION_FILE.exists():
        try:
            cached = LoginResult.from_dict(json.loads(SESSION_FILE.read_text()))
        except Exception:
            log.warning("Cached session unreadable; logging in fresh")
    try:
        result = await client.login(cached=cached)
    except SessionExpiredError:
        result = await client.login()
    if result is not cached:
        SESSION_FILE.write_text(json.dumps(result.to_dict()))
        log.info("Session minted and cached")
    else:
        log.info("Reused cached session")


# ---------------------------------------------------------------- snapshot
async def snapshot(client: NeakasaClient) -> dict[str, dict[str, Any]]:
    devices = await client.list_devices()
    write_json(OUT / "devices.json", [asdict(d) for d in devices])

    if not devices:
        log.error("No devices on this account. Is the Riko shared to it?")
        return {}

    print("\n=== Devices on account ===")
    for d in devices:
        print(f"  {d.product_name:<20} device_name={d.device_name}  "
              f"product_key={d.product_key}  role={d.role.name}  net={d.net_type}")

    snapshots: dict[str, dict[str, Any]] = {}
    for d in devices:
        try:
            # Private, but it's exactly the raw map we want; get_status() would
            # try to coerce this into an M1 DeviceStatus and likely fail.
            raw = await client._get_properties(d.device_name)  # noqa: SLF001
        except Exception as exc:
            log.error("properties/get failed for %s: %s", d.device_name, exc)
            continue
        path = OUT / f"props_{d.device_name}.json"
        write_json(path, raw)
        flat = unwrap_items(raw)
        snapshots[d.device_name] = flat
        print(f"\n=== Raw properties: {d.product_name} ({d.device_name}) → {path} ===")
        for k in sorted(flat):
            v = flat[k]
            vs = json.dumps(v) if isinstance(v, (dict, list)) else repr(v)
            if len(vs) > 100:
                vs = vs[:97] + "..."
            print(f"  {k:<28} {vs}")
    return snapshots


# ---------------------------------------------------------------- live watch
async def watch(client: NeakasaClient, snapshots: dict[str, dict[str, Any]],
                poll_every: float | None) -> None:
    pushes = OUT / "pushes.jsonl"
    stream = client.watch_status()

    # Tap the raw MQTT handler so we see non-property pushes (events, service
    # replies, errors) that the SDK would otherwise drop at debug level.
    original = stream._handle_message  # noqa: SLF001

    async def tapped(topic: str, payload: bytes) -> None:
        try:
            body: Any = json.loads(payload)
        except ValueError:
            body = payload.decode("utf-8", "replace")
        rec = {"ts": now_iso(), "topic": topic, "body": body}
        append_jsonl(pushes, rec)
        kind = "PROPS " if "/thing/properties" in topic else "OTHER "
        print(f"[{rec['ts']}] {kind}{topic}")
        if kind == "OTHER ":
            print("    " + json.dumps(body, default=str)[:600])
        await original(topic, payload)

    stream._handle_message = tapped  # type: ignore[method-assign]  # noqa: SLF001

    def on_change(update) -> None:  # noqa: ANN001
        for k, v in update.changes.items():
            print(f"    {update.device_name}: {k} = {v!r}")
        # keep the polling baseline in sync so we don't double-report
        snap = snapshots.setdefault(update.device_name, {})
        snap.update(update.changes)

    stream.on_change(on_change)

    async def poller() -> None:
        assert poll_every
        while True:
            await asyncio.sleep(poll_every)
            for name, old in list(snapshots.items()):
                try:
                    new = unwrap_items(await client._get_properties(name))  # noqa: SLF001
                except SessionExpiredError:
                    await login(client)
                    continue
                except Exception as exc:
                    log.warning("poll failed for %s: %s", name, exc)
                    continue
                changed = diff(old, new)
                if changed:
                    rec = {"ts": now_iso(), "device": name, "changes": changed}
                    append_jsonl(OUT / "poll_diffs.jsonl", rec)
                    print(f"[{rec['ts']}] POLL  {name}")
                    for k, c in changed.items():
                        print(f"    {k}: {c['old']!r} -> {c['new']!r}")
                    snapshots[name] = new

    print("\n=== Watching. Trigger a feeding / open the app / pull the bowl. Ctrl-C to stop. ===\n")
    async with stream:
        tasks = [asyncio.create_task(stream.run_forever())]
        if poll_every:
            tasks.append(asyncio.create_task(poller()))
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------- main
async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot-only", action="store_true")
    ap.add_argument("--poll", type=float, default=None,
                    help="also poll properties/get every N seconds and log diffs")
    ap.add_argument("--region", default="US", choices=[r.name for r in Region])
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    email = os.environ.get("NEAKASA_EMAIL")
    password = os.environ.get("NEAKASA_PASSWORD")
    if not email or not password:
        print("Set NEAKASA_EMAIL and NEAKASA_PASSWORD in the environment.", file=sys.stderr)
        return 2

    OUT.mkdir(exist_ok=True)

    async with NeakasaClient(email=email, password=password,
                             region=Region[args.region]) as client:
        await login(client)
        snapshots = await snapshot(client)
        if args.snapshot_only or not snapshots:
            return 0
        try:
            await watch(client, snapshots, args.poll)
        except KeyboardInterrupt:
            pass
    print(f"\nCapture saved under {OUT.resolve()}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
