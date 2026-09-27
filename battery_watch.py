#!/usr/bin/env python3
"""
battery_watch.py — dedicated, low-frequency logger for power-state and
device-state transitions, built specifically to investigate the battery
scheduler unreliability (Day 5 finding: device wakes near a slot, evaluates
something for ~29s, and goes back to sleep without feeding).

Kept SEPARATE from monitor.py deliberately:
  - monitor.py polls every 30s continuously for normal operation/remediation.
  - This one exists to answer a specific open question ("does polling itself
    keep the device awake?") and needs a tunable, typically gentler interval
    so it doesn't contaminate what it's trying to measure.

Logs every state/power-type CHANGE (not every poll) to both stdout (for
journalctl) and a dedicated jsonl file for later analysis.
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, "/home/yoho/riko")
import riko

POLL_INTERVAL = float(os.environ.get("BATTERY_WATCH_INTERVAL", "20"))
LOG_PATH = "riko_capture/battery_watch.jsonl"

async def main():
    print(f"battery_watch starting, poll interval={POLL_INTERVAL}s", flush=True)
    prev_state = None
    prev_pwr = None
    async with riko.Riko(os.environ["NEAKASA_EMAIL"], os.environ["NEAKASA_PASSWORD"]) as r:
        while True:
            try:
                st = await r.status()
                pwr_props = st._p("pwrStatus", {}) or {}
                pwr_type = pwr_props.get("pwrType")
                bat_pct = pwr_props.get("batPercentage")
                state_name = st.state.name

                if state_name != prev_state or pwr_type != prev_pwr:
                    ts = time.strftime("%Y-%m-%d %H:%M:%S")
                    line = {
                        "ts": ts,
                        "state": state_name,
                        "prev_state": prev_state,
                        "pwr_type": pwr_type,
                        "prev_pwr_type": prev_pwr,
                        "bat_pct": bat_pct,
                    }
                    print(f"{ts}  state={prev_state}->{state_name}  "
                          f"pwrType={prev_pwr}->{pwr_type}  bat={bat_pct}%", flush=True)
                    with open(LOG_PATH, "a") as f:
                        f.write(json.dumps(line) + "\n")
                    prev_state = state_name
                    prev_pwr = pwr_type
            except Exception as exc:
                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                print(f"{ts}  poll error: {exc}", flush=True)
            await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
