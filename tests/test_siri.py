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

asyncio.run(main())
