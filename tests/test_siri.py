"""siri.py resume: what Siri says in each feeder state. Simulated feeder, no network."""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import siri
from riko import FeedCtrl, RikoStatus

async def nosleep(s): pass
siri.asyncio = type("A", (), {"sleep": staticmethod(nosleep), "wait_for": staticmethod(asyncio.wait_for)})

class Feeder:
    def __init__(self, state, param=0, after=None):
        self.state, self.param, self.after, self.sent = state, param, after, []
    async def status(self):
        return RikoStatus({"deviceState": {"value": {"state": self.state, "param": self.param}}})
    async def feed_control(self, action):
        self.sent.append(action)
        if self.after is not None: self.state = self.after

async def main():
    f = Feeder(3, 0, after=1)                                  # paused -> resumes, preparing
    said = await siri.resume(f)
    assert f.sent == [FeedCtrl.RESUME] and said == "Resumed. The feeder is preparing again.", said
    f = Feeder(3, 13)                                          # food jam: stays stopped
    said = await siri.resume(f)
    assert f.sent == [FeedCtrl.RESUME] and "still stopped" in said and "cleared by hand" in said, said
    for state, words in ((0, "idle"), (5, "asleep"), (1, "already preparing"), (2, "already serving"), (4, "didn't send")):
        f = Feeder(state)
        said = await siri.resume(f)
        assert f.sent == [] and words in said, (state, said)
    print("ok: paused -> resumed; jam -> sent, still stopped + hand-clear tip; idle/asleep/running/fault -> nothing sent")

    # status: the sentence Siri reads
    import json
    class S:
        def __init__(self, **p): self.p = p
        async def status(self):
            raw = {"deviceState": {"value": {"state": self.p.get("state", 0), "param": self.p.get("param", 0)}},
                   "pwrStatus": {"value": {"pwrType": self.p.get("pwr", 0), "batPercentage": 85}},
                   "foodLvl": {"value": self.p.get("food", 2)}, "waterLvl": {"value": self.p.get("water", 4)},
                   "fdPlanStr": {"value": json.dumps({"bEn": [1], "bDayEn": [1], "time": [0], "rept": [127],
                                                       "food": [5], "water": [35], "aud": [2], "type": [1]})}}
            return RikoStatus(raw)
        async def live_scale(self):
            if self.p.get("scale") == "off": return 2.0, 68
            return 68 + self.p.get("bowl", 0), 68
    said = await siri.status(S())
    assert said.startswith("Idle, on mains. Food and water OK.") and "Next 12:00 AM, 5 grams." in said \
        and said.endswith("Bowl is empty."), said
    said = await siri.status(S(state=5, pwr=1, water=1))
    assert said.startswith("Sleeping, on battery at 85 percent. Heads up: water is very low.") and "Bowl" not in said, said
    said = await siri.status(S(state=3, param=13, food=1, bowl=11))
    assert said.startswith("Stopped: grinder jammed with food, on mains. Heads up: food is low.") \
        and said.endswith("Bowl has 11 grams."), said
    print("ok: status ->", said)

    # pause: only a meal that is preparing
    f = Feeder(1, after=3)
    said = await siri.pause(f)
    assert f.sent == [FeedCtrl.PAUSE] and said.startswith("Paused."), said
    for state, param, words in ((2, 0, "Too late"), (3, 5, "already stopped: paused"), (3, 13, "grinder jammed"),
                                (0, 0, "idle"), (5, 0, "sleeping")):
        f = Feeder(state, param)
        said = await siri.pause(f)
        assert f.sent == [] and words in said, (state, param, said)
    print("ok: pause -> only while preparing; serving/stopped/idle/asleep -> nothing sent")

    # feed: the default meal, refused when running, asleep, bowl off, fed recently, or over the day's max
    import time as T
    class FF(S):
        def __init__(self, **p):
            super().__init__(**p); self.fed = []
        async def status(self):
            st = await super().status()
            st.raw["defFdCfg"] = {"value": {"food": 5, "water": 35, "mixMin": 10}}
            st.raw["bowlStatus"] = {"value": {"bBowlIn": 0 if self.p.get("bowl_off") else 1, "curWeight": 68, "bowlGram": 68}}
            return st
        async def feed(self, f, w): self.fed.append((f, w))
    class Led:
        def __init__(self, feeds): self.feeds = feeds
        async def ledger(self, *a, **k): return {"feed_list": self.feeds}
    now = T.time()
    lt = T.localtime(now); midnight = now - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)
    f = FF(); said = await siri.feed(f, Led([{"feed_time": now - 3600, "feed_weight": 5}]))
    assert f.fed == [(5, 35)] and said.startswith("Feeding 5 grams with 35 of water."), said
    cases = [(dict(state=1), [], "already preparing"), (dict(state=5), [], "No SwitchBot"),
             (dict(state=3, param=5), [], "stopped (paused)"), (dict(bowl_off=True), [], "bowl is off"),
             (dict(), [{"feed_time": now - 600, "feed_weight": 5}], "under 20 minutes ago"),
             (dict(), [{"feed_time": midnight + 60, "feed_weight": 57}], "over 60 grams")]
    for kw, feeds, words in cases:
        f = FF(**kw); said = await siri.feed(f, Led(feeds))
        assert f.fed == [] and words in said, (kw, said)
    f = FF(); said = await siri.feed(f, None)
    assert f.fed == [] and "couldn't check" in said
    print("ok: feed -> 5 + 35; refused when running, asleep, paused, bowl off, fed <20 min ago, over 60 g, no ledger")

    # the SwitchBot: a press wakes a sleeping feeder (state 5 -> 0)
    import os
    os.environ["RIKO_SWITCHBOT_MAC"] = "AA:BB:CC:DD:EE:FF"
    def bot(f, ok=True, wakes=True, to=0):
        presses = []
        async def press(mac):
            presses.append(mac)
            if ok and wakes: f.p["state"] = to
            return siri.switchbot_press.PressResult(ok, 1, 7.0, 100, -55, "" if ok else "not seen in the scan")
        siri._press = press
        return presses
    class W(FF):
        async def device_time(self): return T.time() + self.p.get("drift", 0)
        async def sync_time(self): self.p["synced"] = True; self.p["drift"] = 0

    # feed on battery, asleep -> press, wake, feed (soak ~6 min on battery)
    f = W(state=5, pwr=1); presses = bot(f)
    said = await siri.feed(f, Led([]))
    assert presses and f.fed == [(5, 35)] and said.startswith("I woke it with the SwitchBot. Feeding 5 grams"), said
    # ...the guards still come first: fed 10 min ago -> no press at all
    f = W(state=5, pwr=1); presses = bot(f)
    said = await siri.feed(f, Led([{"feed_time": now - 600, "feed_weight": 5}]))
    assert not presses and f.fed == [] and "under 20 minutes" in said, said
    # ...press fails / doesn't wake / it starts its own meal on waking
    for kw, words, fed in ((dict(ok=False), "didn't go through", []), (dict(wakes=False), "didn't wake up", []),
                           (dict(to=1), "started a meal by itself", [])):
        f = W(state=5, pwr=1); bot(f, **kw)
        said = await siri.feed(f, Led([]))
        assert f.fed == fed and words in said, (kw, said)
    os.environ.pop("RIKO_SWITCHBOT_MAC")
    f = W(state=5, pwr=1); said = await siri.feed(f, Led([]))
    assert f.fed == [] and "No SwitchBot" in said, said
    os.environ["RIKO_SWITCHBOT_MAC"] = "AA:BB:CC:DD:EE:FF"
    print("ok: feed on battery -> SwitchBot wake then feed; guards before any press; press failures reported")

    # wake: press if asleep, then fix the clock (never mid-meal)
    f = W(state=5, pwr=1, drift=-15 * 60); presses = bot(f)
    said = await siri.wake(f)
    assert presses and f.p.get("synced") and said == ("Awake. Its clock was 15 minutes slow, so I synced it. "
                                                     "It's on battery and will sleep again in about a minute."), said
    f = W(state=0, drift=3); presses = bot(f)
    said = await siri.wake(f)
    assert not presses and not f.p.get("synced") and said == "It's already awake. Its clock is right.", said
    f = W(state=1, drift=600); presses = bot(f)
    said = await siri.wake(f)
    assert not f.p.get("synced") and "left the clock alone" in said, said
    print("ok: wake ->", "press + clock sync; already awake; mid-meal leaves the clock")

asyncio.run(main())
