"""One definition of "which client is this?", for every per-client limit.

Production evidence (2026-09-02): uvicorn's access log recorded exactly two
source addresses across 14,650 requests --

    9768  127.0.0.1     the container's own healthcheck
    4882  172.20.0.3    every single request from the internet

-- because Caddy runs in its own container on the Docker network and the app
was started without --proxy-headers. `request.client.host` was therefore
Caddy's address for every external caller, so every "per client IP" limit was
really one global bucket:

  * CREATE_WALLET_RATE_LIMIT="3/3600" capped wallet creation at three per hour
    across the entire platform, and one abusive caller closed onboarding for
    everyone;
  * the public screener's 60/minute budget was shared by the whole internet,
    inverting "cannot be used as a free bulk export" into a trivial denial of
    service.

The old helper in routes_user tried to correct for this but trusted
X-Forwarded-For only from loopback -- a rule written for the retired
Tailscale/localhost.run tunnel, where uvicorn genuinely saw 127.0.0.1. Caddy
in its own container is 172.x, so the branch never fired.

This mirrors the screener's `canonicalClientIdentity` in
trader-screener/lib/serviceGuards.mjs, deliberately: two surfaces behind the
same proxy should agree on who the caller is.

Trust model: the forwarded chain is believed ONLY when the immediate peer is a
private address, i.e. the non-published Caddy hop. A direct remote caller
cannot forge its way into another client's bucket, because its own peer
address is public and the header is ignored.
"""
from __future__ import annotations

import ipaddress

UNKNOWN = "unknown"


def _normalize(value: str | None) -> str | None:
    """Parse an address, unwrapping [v6] brackets and ::ffff: mapping."""
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return str(addr)


# RFC1918 plus loopback, listed explicitly.
#
# NOT ipaddress.is_private, which also covers the RFC 5737 documentation
# ranges (203.0.113.0/24 and friends), CGNAT 100.64.0.0/10, link-local
# 169.254.0.0/16 and the benchmarking range. Trusting those would let a caller
# from a carrier-grade-NAT address assert any identity it liked if the app
# were ever reachable without the proxy in front. This mirrors the screener's
# canonicalClientIdentity, which enumerates the same three ranges.
_TRUSTED_NETWORKS = tuple(ipaddress.ip_network(cidr) for cidr in (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
))


def is_trusted_proxy(value: str | None) -> bool:
    """True for the private hop the reverse proxy occupies.

    Loopback stays trusted so a local tunnel or a direct-to-uvicorn deployment
    keeps working; the RFC1918 ranges cover the container network.
    """
    ip = _normalize(value)
    if ip is None:
        return False
    addr = ipaddress.ip_address(ip)
    if addr.is_loopback:
        return True
    return any(addr in net for net in _TRUSTED_NETWORKS)


def client_identity(request) -> str:
    """The address a per-client limit should be keyed on.

    Falls back to the peer address whenever the chain cannot be trusted or
    cannot be parsed, so a malformed header degrades to today's behaviour
    rather than to a shared or attacker-chosen bucket.
    """
    client = getattr(request, "client", None)
    peer = _normalize(getattr(client, "host", None)) or UNKNOWN
    if peer == UNKNOWN or not is_trusted_proxy(peer):
        return peer
    forwarded = ""
    try:
        forwarded = request.headers.get("x-forwarded-for", "") or ""
    except Exception:
        forwarded = ""
    for candidate in forwarded.split(","):
        resolved = _normalize(candidate)
        if resolved is not None:
            return resolved
    return peer
