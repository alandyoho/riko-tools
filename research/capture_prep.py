#!/usr/bin/env python3
"""
capture_prep.py — high-frequency polling during an active feed cycle, to see
what changes in real time while food/water are being prepped.
"""
import asyncio
import json
import os
import time
import riko

POLL_INTERVAL = 1.0
DURATION = 900
VOLTAGE_CHANNELS = {
    1: "food_bin",
    2: "water_level",
    3: "grinder_motor",
    5: "water_pump",
}  # confirmed from TSL spec: 0=battery 1=food_bin 2=water_level 3=grinder_motor 4=NTC 5=water_pump 6=grinder_button
TRIGGER_FEED = True

async def main():
    out_path = f"riko_capture/prep_capture_{int(time.time())}.jsonl"
    print(f"logging to {out_path}")
    async with riko.Riko(os.environ["NEAKASA_EMAIL"], os.environ["NEAKASA_PASSWORD"]) as r:
        if TRIGGER_FEED:
            print("triggering feed...")
            await r.feed(8, 40)

        start = time.monotonic()
        with open(out_path, "a") as f:
            while time.monotonic() - start < DURATION:
                ts = time.time()
                entry = {"ts": ts}
                try:
                    props = await r.get_properties()
                    entry["bowlStatus"] = props.get("bowlStatus", {}).get("value")
                    entry["deviceState"] = props.get("deviceState", {}).get("value")
                except Exception as exc:
                    entry["props_error"] = str(exc)

                for ch, label in VOLTAGE_CHANNELS.items():
                    try:
                        v = await r.invoke("getVoltage", chanIdx=ch)
                        entry[f"v_{label}"] = v
                    except Exception as exc:
                        entry[f"v_{label}_error"] = str(exc)

                f.write(json.dumps(entry, default=str) + "\n")
                f.flush()
                state = entry.get("deviceState", {})
                print(f"{time.strftime('%H:%M:%S')}  state={state.get('state')} "
                      f"bowl={entry.get('bowlStatus')} "
                      f"grinder={entry.get('v_grinder_motor')} pump={entry.get('v_water_pump')}")

                if state.get("state") == 0 and time.monotonic() - start > 5:
                    print("cycle complete (back to IDLE)")
                    break

                await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
