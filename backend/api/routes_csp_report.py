"""POST /api/csp-report — where browsers report Content-Security-Policy refusals.

A refused script, stylesheet, font or image is otherwise invisible
server-side: the browser drops it and the page quietly degrades, which inside
Telegram is a blank Mini App and nothing in any log. Every policy in the
Caddyfile names this endpoint twice, so both wire formats arrive here:

  application/csp-report    report-uri: {"csp-report": {...}}, one per POST
  application/reports+json  report-to (Reporting API): [{"type":
                            "csp-violation", "body": {...}}, ...], batched

Browsers send reports on their own, so this is anonymous by necessity and
everything in a report is untrusted. The log line is its only output:

  * rate limited per client address (backend.core.client_identity), like the
    public screener;
  * bodies are capped here as well as at the edge;
  * each logged field is reduced to a small validated token: the page path
    without query string, identifiers masked; the directive; the blocked
    resource and the script involved, cut to an origin or a keyword. The
    referrer, policy text, script sample and user agent are never logged,
    and nothing from the request itself (cookies, headers) is read.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import time
from collections import deque
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response

from backend.core.client_identity import client_identity

router = APIRouter()
log = logging.getLogger("csp")

# The same 64 KB Caddy enforces on /api, so it also holds when the app is
# reached without the proxy. A report is ~1.5 KB, mostly the policy text the
# browser echoes back.
MAX_BODY_BYTES = 64_000

# A browser reports each distinct violation once per document, so a page load
# sends a handful at most; this only bites a loop or a flood.
REPORT_RATE_LIMIT = 30
REPORT_RATE_WINDOW_SECONDS = 60.0
_MAX_TRACKED_CLIENTS = 4096

# A Reporting API batch can carry many reports. Identical ones collapse into
# one line with a count; past this many distinct lines the rest are counted.
MAX_LINES_PER_REQUEST = 10

_REPORT_URI = "application/csp-report"
_REPORTING_API = "application/reports+json"
_CONTENT_TYPES = {_REPORT_URI, _REPORTING_API, "application/json"}

# client key -> recent request timestamps. The key is a client IP, never
# anything taken from a report.
_requests: dict[str, deque[float]] = {}


def reset_rate_limits() -> None:
    """Test helper: drop every bucket."""
    _requests.clear()


def _enforce_rate_limit(request: Request) -> None:
    client = client_identity(request)
    now = time.monotonic()
    bucket = _requests.get(client)
    if bucket is None:
        # Bound memory against a flood of distinct source addresses.
        if len(_requests) >= _MAX_TRACKED_CLIENTS:
            oldest = min(_requests, key=lambda key: _requests[key][-1] if _requests[key] else 0.0)
            _requests.pop(oldest, None)
        bucket = _requests[client] = deque()
    while bucket and now - bucket[0] > REPORT_RATE_WINDOW_SECONDS:
        bucket.popleft()
    if len(bucket) >= REPORT_RATE_LIMIT:
        raise HTTPException(
            status_code=429,
            detail="too many reports",
            headers={"Retry-After": str(int(REPORT_RATE_WINDOW_SECONDS))},
        )
    bucket.append(now)


_DIRECTIVE_RE = re.compile(r"[a-z][a-z-]{0,39}")
# Keywords ("inline", "eval", "wasm-eval", "trusted-types-sink") and bare
# schemes ("data", "blob"): how browsers report a resource that is not a URL.
_TOKEN_RE = re.compile(r"[a-z][a-z0-9+.-]{0,31}")
_HOST_RE = re.compile(r"[a-z0-9.-]{1,253}")
_IDENTIFIER_RE = re.compile(
    r"0x[0-9a-f]{4,}"                                 # wallet addresses, tx hashes
    r"|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"   # UUIDs
    r"|[0-9a-f]{32,}"                                 # bare hex digests
    r"|\d{5,}",                                       # numeric ids (Telegram users)
    re.I,
)
_PATH_UNSAFE_RE = re.compile(r"[^A-Za-z0-9/._~%{}-]")
_MAX_PATH_CHARS = 120


def _text(report: dict, *keys: str) -> str:
    """First string value among the report-uri and Reporting API spellings."""
    for key in keys:
        value = report.get(key)
        if isinstance(value, str):
            return value.strip()
    return ""


def _directive(value: str) -> str:
    # Older WebKit and Firefox send the whole directive ("img-src 'self'") as
    # violated-directive; the name is its first word.
    words = value.lower().split()
    return words[0] if words and _DIRECTIVE_RE.fullmatch(words[0]) else "unknown"


def _resource(value: str) -> str:
    """A blocked resource or script source, cut to an origin or a keyword.

    No path, query string or credentials survive. Other schemes keep only
    their name: "chrome-extension" is all an extension's report needs to say,
    and the id after it belongs to the visitor.
    """
    value = value.lower()
    if not value:
        return "-"
    if _TOKEN_RE.fullmatch(value):
        return value
    try:
        parts = urlsplit(value)
        host, port = parts.hostname, parts.port
    except ValueError:
        return "invalid"
    if not _TOKEN_RE.fullmatch(parts.scheme):
        return "invalid"
    if parts.scheme not in ("http", "https", "ws", "wss"):
        return parts.scheme
    if not host:
        return "invalid"
    if ":" in host:
        try:
            host = f"[{ipaddress.IPv6Address(host.split('%')[0])}]"
        except ValueError:
            return "invalid"
    elif not _HOST_RE.fullmatch(host):
        return "invalid"
    return f"{parts.scheme}://{host}" + (f":{port}" if port else "")


def _document(value: str) -> str:
    """The reporting page's path: no query string or fragment, identifiers
    masked, so /screener/trader/0x<40 hex> logs as /screener/trader/{id}."""
    if not value:
        return "-"
    try:
        parts = urlsplit(value)
    except ValueError:
        return "invalid"
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return scheme if _TOKEN_RE.fullmatch(scheme) else "invalid"
    path = _PATH_UNSAFE_RE.sub("_", _IDENTIFIER_RE.sub("{id}", parts.path or "/"))
    return path if len(path) <= _MAX_PATH_CHARS else path[:_MAX_PATH_CHARS] + "..."


def describe(report: dict) -> str:
    """One report as the fields of its log line."""
    disposition = _text(report, "disposition").lower()
    directive = (_text(report, "effective-directive", "effectiveDirective")
                 or _text(report, "violated-directive", "violatedDirective"))
    return " ".join((
        "directive=" + _directive(directive),
        "blocked=" + _resource(_text(report, "blocked-uri", "blockedURL")),
        "document=" + _document(_text(report, "document-uri", "documentURL")),
        "source=" + _resource(_text(report, "source-file", "sourceFile")),
        "disposition=" + (disposition if disposition in ("enforce", "report") else "-"),
    ))


def _parse(content_type: str, raw: bytes) -> tuple[str, list[dict]] | None:
    """(the directive the browser followed, report bodies), or None when the
    body is not a CSP report in either format."""
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if isinstance(payload, list) and content_type != _REPORT_URI:
        # Other report types are not CSP and are dropped.
        return "report-to", [
            item["body"] for item in payload
            if isinstance(item, dict) and item.get("type") == "csp-violation"
            and isinstance(item.get("body"), dict)
        ]
    if (isinstance(payload, dict) and content_type != _REPORTING_API
            and isinstance(payload.get("csp-report"), dict)):
        return "report-uri", [payload["csp-report"]]
    return None


async def _read_body(request: Request) -> bytes:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(413, "report too large")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(413, "report too large")
    return bytes(body)


@router.post("/csp-report", status_code=204, include_in_schema=False)
async def csp_report(request: Request) -> Response:
    _enforce_rate_limit(request)
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in _CONTENT_TYPES:
        raise HTTPException(415, "expected application/csp-report or application/reports+json")
    parsed = _parse(content_type, await _read_body(request))
    if parsed is None:
        raise HTTPException(400, "not a CSP violation report")
    via, reports = parsed
    lines: dict[str, int] = {}
    for report in reports:
        line = f"{describe(report)} via={via}"
        lines[line] = lines.get(line, 0) + 1
    for index, (line, count) in enumerate(lines.items()):
        if index == MAX_LINES_PER_REQUEST:
            log.warning("CSP violation: %d more distinct report(s) in this request not logged",
                        len(lines) - index)
            break
        log.warning("CSP violation %s%s", line, f" count={count}" if count > 1 else "")
    return Response(status_code=204)
