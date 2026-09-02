"""Upstream transport resilience (2026-09-02 production log review).

Two measured failures drive these tests:

  * data-api answers 429 under our own load, and the old policy retried on
    two fixed sleeps with no jitter, so colliding workers re-collided.
  * the edge sends a clean HTTP/2 GOAWAY once a connection has carried 10,000
    streams (`ConnectionTerminated error_code:0, last_stream_id:19999`, seven
    times in 3.4 days). httpx raises RemoteProtocolError instead of retrying
    on a fresh connection, and a CACHED SDK client stays poisoned afterwards.

The invariant that must survive all of this: reads retry, submissions never do.
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

import httpx

from backend.core import polymarket as pm_mod
from backend.core.copy_engine import CopyEngine
from backend.core.polymarket import PolymarketClient


def run(coro):
    return asyncio.run(coro)


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else []
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=None, response=self)


class _ScriptedClient:
    """Replays a script of responses/exceptions and records sleeps."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def get(self, url, params=None):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class ReadRetryTests(unittest.TestCase):
    def setUp(self):
        self.slept: list[float] = []

        async def fake_sleep(d):
            self.slept.append(d)

        self._real_sleep = pm_mod.asyncio.sleep
        pm_mod.asyncio.sleep = fake_sleep

    def tearDown(self):
        pm_mod.asyncio.sleep = self._real_sleep

    def _client(self, script):
        pm = PolymarketClient(client=_ScriptedClient(script))
        pm._owns_client = False
        return pm

    def test_goaway_midread_is_retried_on_a_fresh_attempt(self):
        """The exact production failure: RemoteProtocolError, then success."""
        pm = self._client([
            httpx.RemoteProtocolError("<ConnectionTerminated error_code:0, "
                                      "last_stream_id:19999>"),
            _FakeResponse(200, [{"ok": True}]),
        ])
        out = run(pm._get("https://data-api.polymarket.com/positions"))
        self.assertEqual(out, [{"ok": True}])
        self.assertEqual(pm._client.calls, 2)
        self.assertEqual(len(self.slept), 1)

    def test_transport_failure_eventually_raises_rather_than_returning_none(self):
        """Callers rely on `except Exception`; a silent None would corrupt them."""
        pm = self._client([httpx.ConnectError("boom")] * pm_mod.READ_MAX_ATTEMPTS)
        with self.assertRaises(httpx.ConnectError):
            run(pm._get("https://data-api.polymarket.com/positions"))
        self.assertEqual(pm._client.calls, pm_mod.READ_MAX_ATTEMPTS)

    def test_429_honours_retry_after_over_computed_backoff(self):
        pm = self._client([
            _FakeResponse(429, headers={"retry-after": "7"}),
            _FakeResponse(200, [1]),
        ])
        self.assertEqual(run(pm._get("https://data-api.polymarket.com/x")), [1])
        self.assertEqual(self.slept, [7.0])

    def test_retry_after_is_capped(self):
        pm = self._client([
            _FakeResponse(429, headers={"retry-after": "9999"}),
            _FakeResponse(200, [1]),
        ])
        run(pm._get("https://data-api.polymarket.com/x"))
        self.assertEqual(self.slept, [pm_mod.RETRY_AFTER_CAP])

    def test_429_without_retry_after_uses_jittered_backoff(self):
        """No fixed 1s/3s ladder: two clients must not sleep in lockstep."""
        delays = set()
        for _ in range(24):
            self.slept.clear()
            pm = self._client([_FakeResponse(429), _FakeResponse(200, [1])])
            run(pm._get("https://data-api.polymarket.com/x"))
            delays.add(self.slept[0])
        self.assertGreater(len(delays), 1, "backoff is not jittered")
        self.assertLessEqual(max(delays), pm_mod.READ_BACKOFF_CAP)

    def test_500_is_not_retried(self):
        """A per-wallet 500 is deterministic upstream; retrying multiplies it."""
        pm = self._client([_FakeResponse(500)])
        with self.assertRaises(httpx.HTTPStatusError):
            run(pm._get("https://data-api.polymarket.com/x"))
        self.assertEqual(pm._client.calls, 1)
        self.assertEqual(self.slept, [])

    def test_gateway_errors_are_retried(self):
        pm = self._client([_FakeResponse(503), _FakeResponse(200, [1])])
        self.assertEqual(run(pm._get("https://data-api.polymarket.com/x")), [1])
        self.assertEqual(pm._client.calls, 2)


class _FakeClient:
    def __init__(self, tag): self.tag = tag; self.closed = False
    async def close(self): self.closed = True


class CollateralClientRecoveryTests(unittest.TestCase):
    """A recycled connection must not cost us the tick."""

    def _engine(self, collateral_fn):
        built = []

        async def factory(user):
            c = _FakeClient(f"client-{len(built)}")
            built.append(c)
            return c

        eng = CopyEngine(db=SimpleNamespace(), pm=SimpleNamespace(),
                         client_factory=factory, collateral_fn=collateral_fn)
        return eng, built

    def test_goaway_rebuilds_the_client_and_retries_the_read(self):
        seen = []

        async def collateral(client):
            seen.append(client.tag)
            if len(seen) == 1:
                raise httpx.RemoteProtocolError(
                    "<ConnectionTerminated error_code:0, last_stream_id:19999>")
            return 42.0

        eng, built = self._engine(collateral)
        user = {"id": "0xuser"}

        async def go():
            client = await eng._get_client(user)
            return await eng._read_collateral(user, client)

        value, client = run(go())
        self.assertEqual(value, 42.0)
        self.assertEqual(seen, ["client-0", "client-1"])
        self.assertTrue(built[0].closed, "poisoned client was not closed")
        self.assertIs(client, built[1])
        self.assertIs(eng._clients["0xuser"], built[1])

    def test_sdk_transport_error_is_also_recovered(self):
        from polymarket import errors as pm_errors
        calls = []

        async def collateral(client):
            calls.append(client.tag)
            if len(calls) == 1:
                raise pm_errors.TransportError("connection lost")
            return 7.5

        eng, _ = self._engine(collateral)
        user = {"id": "0xuser"}

        async def go():
            return await eng._read_collateral(user, await eng._get_client(user))

        self.assertEqual(run(go())[0], 7.5)

    def test_only_one_rebuild_is_attempted(self):
        """Fail closed rather than loop: the second failure propagates."""
        async def collateral(client):
            raise httpx.RemoteProtocolError("still gone")

        eng, built = self._engine(collateral)
        user = {"id": "0xuser"}

        async def go():
            return await eng._read_collateral(user, await eng._get_client(user))

        with self.assertRaises(httpx.RemoteProtocolError):
            run(go())
        self.assertEqual(len(built), 2)

    def test_non_transport_errors_are_not_retried(self):
        """A rejection is an answer, not a broken pipe — don't rebuild on it."""
        from polymarket import errors as pm_errors
        calls = []

        async def collateral(client):
            calls.append(1)
            raise pm_errors.UserInputError("bad request")

        eng, built = self._engine(collateral)
        user = {"id": "0xuser"}

        async def go():
            return await eng._read_collateral(user, await eng._get_client(user))

        with self.assertRaises(pm_errors.UserInputError):
            run(go())
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(built), 1)


if __name__ == "__main__":
    unittest.main()
