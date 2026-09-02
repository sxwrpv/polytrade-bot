"""Per-client limits must key on the caller, not on the proxy.

Production, 2026-09-02: uvicorn's access log carried exactly two source
addresses across 14,650 requests — 127.0.0.1 (the container's own healthcheck)
and 172.20.0.3 (Caddy, i.e. everyone else). Every "per client IP" limit was
therefore one global bucket.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from backend.core.client_identity import client_identity, is_trusted_proxy


def _req(peer, forwarded=None):
    headers = {}
    if forwarded is not None:
        headers["x-forwarded-for"] = forwarded
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers=headers)


class TrustedProxyTests(unittest.TestCase):
    def test_the_caddy_container_hop_is_trusted(self):
        self.assertTrue(is_trusted_proxy("172.20.0.3"))   # the observed address

    def test_private_and_loopback_ranges_are_trusted(self):
        for addr in ("127.0.0.1", "::1", "10.1.2.3", "192.168.0.9", "172.31.255.1"):
            self.assertTrue(is_trusted_proxy(addr), addr)

    def test_public_addresses_are_not_trusted(self):
        for addr in ("203.0.113.9", "8.8.8.8", "172.32.0.1", "2001:db8::1"):
            self.assertFalse(is_trusted_proxy(addr), addr)

    def test_garbage_is_not_trusted(self):
        for addr in (None, "", "not-an-ip", "999.1.1.1"):
            self.assertFalse(is_trusted_proxy(addr), repr(addr))


class ClientIdentityTests(unittest.TestCase):
    def test_the_production_case_resolves_to_the_real_caller(self):
        """172.20.0.3 is Caddy; the header carries who actually called."""
        self.assertEqual(
            client_identity(_req("172.20.0.3", "203.0.113.9, 172.20.0.3")),
            "203.0.113.9")

    def test_two_callers_behind_the_proxy_get_different_buckets(self):
        a = client_identity(_req("172.20.0.3", "203.0.113.9"))
        b = client_identity(_req("172.20.0.3", "198.51.100.4"))
        self.assertNotEqual(a, b, "distinct callers still share one bucket")

    def test_a_direct_caller_cannot_forge_its_way_into_another_bucket(self):
        """The peer is public, so its header is not believed."""
        self.assertEqual(
            client_identity(_req("203.0.113.9", "10.0.0.1")), "203.0.113.9")

    def test_loopback_still_works_for_a_tunnel_or_direct_deployment(self):
        self.assertEqual(
            client_identity(_req("127.0.0.1", "203.0.113.9")), "203.0.113.9")

    def test_no_header_falls_back_to_the_peer(self):
        self.assertEqual(client_identity(_req("172.20.0.3")), "172.20.0.3")

    def test_a_malformed_chain_degrades_to_the_peer(self):
        """Never to a shared or attacker-chosen bucket."""
        self.assertEqual(
            client_identity(_req("172.20.0.3", "not-an-ip")), "172.20.0.3")

    def test_the_first_parseable_entry_wins(self):
        self.assertEqual(
            client_identity(_req("172.20.0.3", "junk, 203.0.113.9")), "203.0.113.9")

    def test_ipv4_mapped_ipv6_is_normalised(self):
        """::ffff:203.0.113.9 and 203.0.113.9 must be one bucket, not two."""
        self.assertEqual(
            client_identity(_req("172.20.0.3", "::ffff:203.0.113.9")), "203.0.113.9")

    def test_bracketed_ipv6_is_unwrapped(self):
        self.assertEqual(
            client_identity(_req("172.20.0.3", "[2001:db8::1]")), "2001:db8::1")

    def test_a_missing_client_is_unknown_not_a_crash(self):
        self.assertEqual(
            client_identity(SimpleNamespace(client=None, headers={})), "unknown")


class WiringTests(unittest.TestCase):
    """Both rate-limited surfaces must agree on who the caller is."""

    def test_wallet_creation_uses_the_shared_definition(self):
        from backend.api.routes_user import _client_ip
        self.assertEqual(
            _client_ip(_req("172.20.0.3", "203.0.113.9")), "203.0.113.9")

    def test_public_screener_uses_the_shared_definition(self):
        from backend.api import routes_public_screener as ps
        ps.reset_rate_limits()
        ps._enforce_rate_limit(_req("172.20.0.3", "203.0.113.9"))
        ps._enforce_rate_limit(_req("172.20.0.3", "198.51.100.4"))
        self.assertEqual(sorted(ps.rate_limit_keys()),
                         ["198.51.100.4", "203.0.113.9"])
        ps.reset_rate_limits()

    def test_one_screener_client_cannot_exhaust_everyone_elses_budget(self):
        from fastapi import HTTPException
        from backend.api import routes_public_screener as ps
        ps.reset_rate_limits()
        noisy = _req("172.20.0.3", "203.0.113.9")
        for _ in range(ps.PUBLIC_RATE_LIMIT):
            ps._enforce_rate_limit(noisy)
        with self.assertRaises(HTTPException):
            ps._enforce_rate_limit(noisy)
        # a different caller is unaffected — this was the whole bug
        ps._enforce_rate_limit(_req("172.20.0.3", "198.51.100.4"))
        ps.reset_rate_limits()

    def test_one_ip_cannot_close_wallet_creation_for_everyone(self):
        from backend.api import routes_user as ru
        ru._create_hits.clear()
        noisy = _req("172.20.0.3", "203.0.113.9")
        limit = int(ru.CREATE_WALLET_RATE_LIMIT.split("/")[0])
        for _ in range(limit):
            self.assertFalse(ru._create_rate_limited(ru._client_ip(noisy)))
        self.assertTrue(ru._create_rate_limited(ru._client_ip(noisy)))
        other = _req("172.20.0.3", "198.51.100.4")
        self.assertFalse(ru._create_rate_limited(ru._client_ip(other)),
                         "a second user was locked out by someone else's quota")
        ru._create_hits.clear()


if __name__ == "__main__":
    unittest.main()
