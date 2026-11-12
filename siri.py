"""Voice commands for the feeder, run from an iPhone Shortcut ("Run Script over SSH") via
siri.sh. Each command prints ONE short sentence, meant to be spoken back by Siri.

    siri.sh resume      resume a paused or stopped meal
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import load
from riko import ERROR_CODES, FeedCtrl, FeederState, Riko

LOG = Path(__file__).resolve().parent / "riko_state" / "siri.log"
JAM = 13          # grinder jammed with food: resume doesn't clear it (0 of 10, 2026-11-05)


def _log(cmd: str, said: str) -> None:
    try:
        with LOG.open("a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {cmd}: {said}\n")
    except OSError:
        pass


async def resume(r: Riko) -> str:
    st = await r.status()
    if st.state == FeederState.SLEEPING:
        return "The feeder is asleep on battery. There's no meal to resume."
    if st.state in (FeederState.PREPARING, FeederState.SERVING):
        return f"It's already {st.state.name.lower()} a meal. Nothing to resume."
    if st.state == FeederState.IDLE:
        return "The feeder is idle. There's no meal to resume."
    if st.state != FeederState.SUSPENDED:
        why = ERROR_CODES.get(st.state_param, f"code {st.state_param}")
        return f"The feeder is in {st.state.name.lower().replace('_', ' ')} ({why}), not a paused meal. I didn't send anything."

    code = st.state_param
    why = ERROR_CODES.get(code, f"code {code}")
    await r.feed_control(FeedCtrl.RESUME)
    for _ in range(10):                       # give it ~20 s to get going
        await asyncio.sleep(2)
        st = await r.status()
        if st.state != FeederState.SUSPENDED:
            return f"Resumed. The feeder is {st.state.name.lower()} again."
    tip = " A grinder jam usually has to be cleared by hand: take the bowl off first." if code == JAM else ""
    return f"I sent resume, but it's still stopped: {why}.{tip}"


COMMANDS = {"resume": resume}


async def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in COMMANDS:
        print("Say one of: " + ", ".join(COMMANDS) + ".")
        return 2
    logging.basicConfig(level=logging.ERROR)
    try:
        async with Riko.from_config(load()) as r:
            r.relogin = r.login               # share the account's one session with the monitor
            said = await COMMANDS[cmd](r)
    except Exception as exc:
        msg = str(exc)
        said = ("The feeder isn't answering. It may be asleep on battery or offline."
                if "20056" in msg or "6205" in msg else f"Something went wrong: {msg[:120]}")
    _log(cmd, said)
    print(said)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
