"""Press the SwitchBot Bot mounted on the feeder's feed button, over Bluetooth from the Pi.

On battery the feeder sleeps between meals and can't be reached; one press of its feed
button wakes it without feeding (2026-11-09), after which it takes commands. This is the
monitor's way to make that happen when nobody is home (BUGS.md #1, #13).

No hub and no internet: Bluetooth LE straight from the Pi (PySwitchbot + bleak). The Bot
must be in Press mode with no password. Its address comes from RIKO_SWITCHBOT_MAC.

    python3 switchbot_press.py            # press once, print the result
    python3 switchbot_press.py --scan     # list SwitchBot devices in range
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from dataclasses import dataclass

log = logging.getLogger("riko.switchbot")


@dataclass
class PressResult:
    ok: bool
    attempts: int
    seconds: float
    battery: int | None = None      # % from the Bot's advertisement
    rssi: int | None = None
    error: str = ""


async def _find(mac: str, scan_s: float):
    from switchbot import GetSwitchbotDevices
    devs = await GetSwitchbotDevices().discover(scan_timeout=scan_s)
    return devs.get(mac.upper())


async def press(mac: str, attempts: int = 4, scan_s: float = 6.0) -> PressResult:
    """Press once. The Bot advertises only every second or two and the signal varies, so a
    scan can miss it; retry the scan and the press up to `attempts` times."""
    from switchbot import Switchbot
    t0, err, battery, rssi = time.time(), "", None, None
    for i in range(1, attempts + 1):
        try:
            adv = await _find(mac, scan_s)
            if adv is None:
                err = "not seen in the scan"
                log.info("switchbot: try %d: %s", i, err)
                continue
            battery, rssi = adv.data.get("data", {}).get("battery"), adv.rssi
            if await Switchbot(adv.device).press():
                return PressResult(True, i, time.time() - t0, battery, rssi)
            err = "press not acknowledged"
        except Exception as exc:                    # BLE errors are many and transient
            err = repr(exc)
        log.info("switchbot: try %d: %s", i, err)
        await asyncio.sleep(1)
    return PressResult(False, attempts, time.time() - t0, battery, rssi, err)


async def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--scan", action="store_true", help="list SwitchBot devices in range")
    ap.add_argument("--mac", default=os.environ.get("RIKO_SWITCHBOT_MAC", ""))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if args.scan:
        from switchbot import GetSwitchbotDevices
        for mac, adv in (await GetSwitchbotDevices().discover(scan_timeout=10)).items():
            print(mac, adv.data.get("modelFriendlyName"), "rssi", adv.rssi, adv.data.get("data"))
        return 0
    if not args.mac:
        print("set RIKO_SWITCHBOT_MAC or pass --mac"); return 2
    r = await press(args.mac)
    print(r)
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
