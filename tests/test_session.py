"""Session handling: one login shared between Riko and the ledger client, recovery from a
half-finished login, forced re-login after repeated failures, and the ledger's 1007 retry.

Background: on 2026-09-29 two clients on one account kept invalidating each other, a
re-login timed out half-way, and the monitor sat broken all night. Fake clients only.
"""
import asyncio, sys, tempfile, types
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor as M
from riko import Riko
from neakasa import Feeder

M.power = types.SimpleNamespace(read_ups=lambda: None)      # no battery reads in this test

class FakeClient:
    def __init__(self, lr): self.login_result = lr; self.logins = []
    async def login(self, *, cached="FRESH"):
        self.logins.append(cached)
        self.login_result = types.SimpleNamespace(iot_token="new", user_token="t2", tag="after",
                                                  to_dict=lambda: {})
        return self.login_result

async def main():
    # 1. the ledger client reads the shared client's session live, and never logs in itself
    c = FakeClient(types.SimpleNamespace(iot_token="a", tag="before"))
    f = Feeder(client=c)
    assert f._s.tag == "before"
    await c.login(); assert f._s.tag == "after", "the ledger client should see the re-login"
    async with f: pass
    assert len(c.logins) == 1, "a shared ledger client must not log in on enter"
    print("1 ok: ledger client shares the session live, no login of its own")

    # 2. a half-finished login (no IoT token) is completed before the next call
    r = Riko("e", "p", session_file=None)
    half = types.SimpleNamespace(iot_token="", tag="half")
    r._client = FakeClient(half)
    async def fn(): return "status"
    assert await r._with_relogin(fn) == "status"
    assert r._client.logins == [half], r._client.logins
    await r._with_relogin(fn)
    assert len(r._client.logins) == 1, "no re-login once the IoT token is there"
    print("2 ok: half session resumed via login(cached=...)")

    # 3. a 29003 (another login replaced ours) forces one fresh login and one retry
    r._client = FakeClient(types.SimpleNamespace(iot_token="ok"))
    tries = []
    async def flaky():
        tries.append(1)
        if len(tries) == 1: raise RuntimeError("server returned code 29003")
        return "ok"
    assert await r._with_relogin(flaky) == "ok" and len(tries) == 2 and r._client.logins == ["FRESH"]
    print("3 ok: 29003 -> fresh login and one retry")

    # 4. repeated status failures force a fresh login on the 2nd, then every 10th
    class FakeR:
        n = 0
        async def relogin(self): self.n += 1
        async def status(self): raise TimeoutError()
    notes = []
    notifier = types.SimpleNamespace(action_needed=lambda *a: notes.append(a) or True, fyi=lambda *a: True)
    mon = M.Monitor(FakeR(), None, notifier, M.Policy(killswitch=Path("/nonexistent")), 0, False)
    for _ in range(21): await mon.check()
    assert mon.r.n == 3, mon.r.n                # failures 2, 10 and 20
    print("4 ok: forced re-logins after 21 straight failures =", mon.r.n)

    # 5. the ledger's 1007 TokenInvalid -> one fresh login, one retry
    class FakeLedger:
        k = 0
        async def ledger(self, *a, **kw):
            self.k += 1
            if self.k == 1: raise RuntimeError("/api/feeder/record: code=1007 'TokenInvalid'")
            return {"feed_list": []}
    mon = M.Monitor(FakeR(), M.Store(Path(tempfile.mkdtemp()) / "m.db"), notifier,
                    M.Policy(killswitch=Path("/nonexistent")), 0, False, neakasa=FakeLedger(), device_name="dev")
    M._near_a_feed_slot = lambda st, *a: True
    await mon.check_ledger_failures(None)
    assert mon.neakasa.k == 2 and mon.r.n == 1
    print("5 ok: ledger 1007 retried once after a fresh login")

    # 6. within one pass the ledger is fetched once and shared; a failure isn't retried in that pass
    class CountingLedger:
        calls = 0; broken = False
        async def ledger(self, *a, **kw):
            self.calls += 1
            if self.broken: raise TimeoutError()
            return {"feed_list": [], "eat_list": []}
    mon = M.Monitor(FakeR(), M.Store(Path(tempfile.mkdtemp()) / "m.db"), notifier,
                    M.Policy(killswitch=Path("/nonexistent")), 0, False, neakasa=CountingLedger(), device_name="dev")
    mon._in_pass = True
    first = await mon._get_ledger(); assert await mon._get_ledger() is first and mon.neakasa.calls == 1
    mon._in_pass = False
    await mon._get_ledger(); assert mon.neakasa.calls == 2, "no caching outside a pass"
    mon._in_pass, mon._pass_ledger, mon._pass_ledger_failed = True, None, False
    mon.neakasa.broken = True
    for _ in range(3):
        try: await mon._get_ledger(); raise AssertionError("should have raised")
        except (TimeoutError, RuntimeError): pass
    assert mon.neakasa.calls == 3, mon.neakasa.calls
    print("6 ok: one ledger fetch per pass, shared; a failed fetch isn't repeated in the same pass")

asyncio.run(main())
