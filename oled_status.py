#!/usr/bin/env python3
"""
oled_status.py — live status on the Adafruit 2.23" OLED Bonnet: an outline
cat mascot that twists (leans left/right) and bounces (up/down), with a
speech bubble cycling through status lines tied to real device conditions.
"""
import asyncio
import json
import time
import board
import busio
import adafruit_ssd1305
from PIL import Image, ImageDraw, ImageFont

import sys
sys.path.insert(0, "/home/yoho/riko")
from riko import RikoStatus
import power
from config import load as load_config

REFRESH_SECONDS = 5
# Status comes from the monitor's snapshot (riko_state/status.json), written each
# pass. The display used to log in itself, which knocked the monitor's session out
# (one live session per account) and could get stuck half-logged-in.
STALE_SECONDS = 120   # monitor writes every ~30 s; older than this = monitor not running
FAILOVER_STALE_SECONDS = 700   # ...but only every 5 min while on cellular backup
ROTATE_SECONDS = 2
FLIP_180 = True       # the display is mounted upside down
JUST_FIXED_DISPLAY_SECONDS = 15

i2c = busio.I2C(board.SCL, board.SDA)
oled = adafruit_ssd1305.SSD1305_I2C(128, 32, i2c)
font = ImageFont.load_default()

def draw_cat(draw: ImageDraw.ImageDraw, x: int, y: int,
             blink: bool, flick: bool, lean: int) -> None:
    """lean: pixel offset applied to the top of the head relative to the
    bottom, giving a twist/tilt feel without true rotation math."""
    draw.line([(x + 2 + lean, y + 6), (x + 22 + lean, y + 6)], fill=1)
    draw.line([(x + 2, y + 22), (x + 22, y + 22)], fill=1)
    draw.line([(x + 2 + lean, y + 6), (x + 2, y + 22)], fill=1)
    draw.line([(x + 22 + lean, y + 6), (x + 22, y + 22)], fill=1)

    draw.line([(x + 2 + lean, y + 6), (x + 7 + lean, y + 0)], fill=1)
    draw.line([(x + 7 + lean, y + 0), (x + 9 + lean, y + 8)], fill=1)
    if flick:
        draw.line([(x + 15 + lean, y + 8), (x + 21 + lean, y + 3)], fill=1)
        draw.line([(x + 21 + lean, y + 3), (x + 23 + lean, y + 9)], fill=1)
    else:
        draw.line([(x + 15 + lean, y + 8), (x + 17 + lean, y + 0)], fill=1)
        draw.line([(x + 17 + lean, y + 0), (x + 22 + lean, y + 6)], fill=1)

    ex = x + lean // 2
    if blink:
        draw.line([(ex + 7, y + 13), (ex + 10, y + 13)], fill=1)
        draw.line([(ex + 14, y + 13), (ex + 17, y + 13)], fill=1)
    else:
        draw.ellipse([ex + 7, y + 11, ex + 10, y + 14], outline=1, fill=0)
        draw.ellipse([ex + 14, y + 11, ex + 17, y + 14], outline=1, fill=0)
    draw.point((ex + 12, y + 16), fill=1)
    for dy in (-2, 0, 2):
        draw.line([(ex - 4, y + 17 + dy), (ex + 6, y + 16 + dy)], fill=1)
        draw.line([(ex + 18, y + 16 + dy), (ex + 28, y + 17 + dy)], fill=1)

SUSPEND_VOICE = {
    20: "Hey! Where's my bowl?!",
    21: "Bowl's stuck, help!",
    22: "Can't get my bowl out",
    12: "My lid's open!",
    13: "I'm jammed, ouch!",
    11: "Something feels off in there",
    50: "Snacks running low...",
    51: "MY FOOD IS EMPTY",
    40: "Kinda thirsty here",
    41: "Water's all gone!",
    60: "Whoa, bowl's too heavy",
    62: "My scale feels weird",
    70: "Pump's being stubborn",
    71: "Pump's being stubborn",
    30: "Where'd my battery go?",
    31: "Lost power, uh oh",
    32: "Feeling a bit low...",
    33: "Getting a little warm",
}

STATE_VOICE = {
    "PREPARING": ["Ooh, dinner time!", "Mixing it up..."],
    "SERVING": ["Nom nom nom", "Eating time!"],
    "SLEEPING": ["Zzz... night mode", "Shh, I'm resting"],
    "CLEANING": ["Bath time!", "Getting squeaky clean"],
    "FAULT": ["Something's wrong...", "Need a human here"],
}

IDLE_VOICE = ["All good here!", "Just chillin'", "Nothing to report", "Nap time?"]

def render(icon_x: int, icon_y: int, message: str, frame: int, fixing: bool) -> None:
    image = Image.new("1", (128, 32))
    draw = ImageDraw.Draw(image)

    # animation cycle: bounce up/down every frame, lean left/right on a
    # slower cycle so the two don't always peak together
    bounce = [0, -2, 0, 1][frame % 4]
    lean = [0, 3, 0, -3][(frame // 2) % 4]
    blink = (frame % 8 == 7)
    flick = (frame % 5 == 2 and not blink)

    draw_cat(draw, icon_x, icon_y + bounce, blink, flick, lean)

    bubble_x0 = icon_x + 32
    bubble_y0, bubble_x1, bubble_y1 = 2, 126, 29
    draw.rounded_rectangle([bubble_x0, bubble_y0, bubble_x1, bubble_y1],
                            radius=4, outline=1, fill=0)
    draw.polygon([(bubble_x0, 14), (bubble_x0, 20), (bubble_x0 - 6, 17)], fill=1)

    words = message.split(" ")
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if len(trial) > 15:
            lines.append(cur)
            cur = w
        else:
            cur = trial
    if cur:
        lines.append(cur)
    lines = lines[:2]

    for i, line in enumerate(lines):
        draw.text((bubble_x0 + 4, bubble_y0 + 3 + i * 11), line, font=font, fill=1)

    oled.fill(0)
    oled.image(image.rotate(180) if FLIP_180 else image)
    oled.show()

_tare_state = {"was_wrong": False, "just_fixed_until": 0.0}

def get_status_messages(status_path) -> list[str]:
    """Battery state first (the Pi's own backup battery), then the feeder's status."""
    if status_path.with_name("SAFE_IDLE").exists():      # power_guard.py: battery nearly empty
        return ["Battery's nearly out.", "Resting till power's", "back. See you soon!"]
    ups = power.read_ups()
    if ups is not None and ups.on_battery:
        left = f", ~{ups.minutes_left}min" if ups.minutes_left else ""
        return ["Power's out! I'm on", f"battery {ups.percent}%{left}"] + _feeder_messages(status_path)
    return _feeder_messages(status_path)


def _feeder_messages(status_path) -> list[str]:
    try:
        snap = json.loads(status_path.read_text())
        on_cellular = status_path.with_name("FAILOVER").exists()   # wifi_failover.py takeover
        if time.time() - snap["ts"] > (FAILOVER_STALE_SECONDS if on_cellular else STALE_SECONDS):
            return ["Uh oh...", "monitor isn't updating"]
        if snap.get("error"):
            return ["Uh oh...", snap["error"][:24]]
        st = RikoStatus(snap["raw"])
        state_name = st.state.name
        param = st._p("deviceState", {}).get("param", 0)
        tare_ok = st.bowl_tare_g == 68
        now = time.monotonic()

        if tare_ok and _tare_state["was_wrong"]:
            _tare_state["just_fixed_until"] = now + JUST_FIXED_DISPLAY_SECONDS
        _tare_state["was_wrong"] = not tare_ok

        if not tare_ok:
            return [f"My bowl's wrong ({st.bowl_tare_g}g)", "Fixing it now, hang on!"]
        if now < _tare_state["just_fixed_until"]:
            return ["All fixed, good as new!", f"Bowl's at {st.bowl_tare_g}g now"]

        cover_present = st._p("bCoverAbsent", 1)  # inverted name: 1=on, 0=off

        alerts = []
        if on_cellular:
            alerts.append("Wifi's down! I'm on")
            alerts.append("cellular backup")
        if state_name == "SUSPENDED" and param in SUSPEND_VOICE:
            alerts.append(SUSPEND_VOICE[param])
            alerts.append(f"(error {param})")
        if not st.bowl_in:
            alerts.append("Hey, where'd my bowl go?!")
        if not cover_present:
            alerts.append("My food lid's off!")
        if st.food_level.name in ("INSUFFICIENT", "EMPTY"):
            alerts.append(f"Food's {st.food_level.name.lower()}, heads up")
        if st.water_level.name in ("LOW", "INSUFFICIENT", "EMPTY"):
            alerts.append(f"Water's {st.water_level.name.lower()}, heads up")

        if alerts:
            return alerts

        if state_name in STATE_VOICE:
            return STATE_VOICE[state_name]
        if state_name == "IDLE":
            return IDLE_VOICE
        return [f"I'm {state_name.lower()}"]
    except Exception as exc:
        return ["Uh oh...", str(exc)[:24]]

async def main():
    status_path = load_config().state_dir / "status.json"
    messages = get_status_messages(status_path)
    last_poll = time.monotonic()
    idx = 0
    while True:
        render(icon_x=6, icon_y=6, message=messages[idx % len(messages)],
               frame=idx, fixing=False)
        idx += 1
        await asyncio.sleep(ROTATE_SECONDS)
        if time.monotonic() - last_poll > REFRESH_SECONDS:
            messages = get_status_messages(status_path)
            last_poll = time.monotonic()

if __name__ == "__main__":
    asyncio.run(main())
