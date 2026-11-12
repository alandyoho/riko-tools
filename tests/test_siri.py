"""siri.py resume: what Siri says in each feeder state. Simulated feeder, no network."""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import siri
from riko import FeedCtrl, RikoStatus

async def nosleep(s): pass
siri.asyncio = type("A", (), {"sleep": staticmethod(nosleep)})

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

asyncio.run(main())
