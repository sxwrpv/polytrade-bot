"""Liveness signals for the readiness endpoint.

`/api/health` proves the web process can answer HTTP. It returned 200 on all
9,768 checks between 30 Aug and 2 Sep while the copy engine placed zero orders,
ran on the fallback detector because POLYGON_RPC_URL was blank, and had its
hardening pass throw on every boot. Nothing in that signal could distinguish a
working system from a stopped one.

A background loop is healthy when it has completed a pass RECENTLY, so each
loop stamps its name here on success and readiness compares the age against a
staleness budget derived from that loop's own interval.

Nothing here holds a wallet address, a user id, a market or a secret — only
names, timestamps and counts — because the readiness endpoint is reachable
without a session.
"""
from __future__ import annotations

import threading
import time

# Loop name -> the multiple of its own interval after which it is stale. A
# loop that runs every 5s and has not completed a pass in 60s is not slow, it
# is stuck; a 15-minute crawler needs a much wider window.
STALENESS_FACTOR = 4.0
MIN_STALENESS_SECONDS = 30.0


class Heartbeats:
    """Last-success clock per background loop. Thread- and task-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._marks: dict[str, float] = {}
        self._intervals: dict[str, float] = {}
        self._counts: dict[str, int] = {}

    def register(self, name: str, interval_seconds: float) -> None:
        """Declare a loop and its cadence, before it has ever succeeded.

        Registering matters: an unregistered loop is indistinguishable from a
        loop that has never run, and the engine being absent entirely is the
        single most important thing this endpoint reports.
        """
        with self._lock:
            self._intervals[name] = float(interval_seconds)
            self._marks.setdefault(name, 0.0)
            self._counts.setdefault(name, 0)

    def mark(self, name: str) -> None:
        with self._lock:
            self._marks[name] = time.time()
            self._counts[name] = self._counts.get(name, 0) + 1

    def budget(self, name: str) -> float:
        interval = self._intervals.get(name, 0.0)
        return max(MIN_STALENESS_SECONDS, interval * STALENESS_FACTOR)

    def snapshot(self) -> dict[str, dict]:
        now = time.time()
        with self._lock:
            names = set(self._intervals) | set(self._marks)
            out = {}
            for name in sorted(names):
                last = self._marks.get(name, 0.0)
                interval = self._intervals.get(name, 0.0)
                budget = max(MIN_STALENESS_SECONDS, interval * STALENESS_FACTOR)
                out[name] = {
                    "registered": name in self._intervals,
                    "interval_seconds": round(interval, 3) if interval else None,
                    "last_success_age_seconds": (
                        None if not last else round(now - last, 1)),
                    "successes": self._counts.get(name, 0),
                    "stale": (True if not last else (now - last) > budget),
                    "staleness_budget_seconds": round(budget, 1),
                }
            return out


class UpstreamCounters:
    """Rolling upstream outcome counts, for the readiness endpoint.

    Absolute counters plus the process start, so a reader can derive a rate
    without this module keeping a time series.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started_at = time.time()
        self.requests = 0
        self.rate_limited = 0
        self.server_errors = 0
        self.transport_errors = 0
        self.retries = 0

    def record(self, *, ok: bool = False, rate_limited: bool = False,
               server_error: bool = False, transport_error: bool = False,
               retry: bool = False) -> None:
        with self._lock:
            if ok:
                self.requests += 1
            if rate_limited:
                self.rate_limited += 1
            if server_error:
                self.server_errors += 1
            if transport_error:
                self.transport_errors += 1
            if retry:
                self.retries += 1

    def snapshot(self) -> dict:
        with self._lock:
            elapsed = max(1.0, time.time() - self.started_at)
            return {
                "window_seconds": round(elapsed, 1),
                "requests": self.requests,
                "rate_limited": self.rate_limited,
                "server_errors": self.server_errors,
                "transport_errors": self.transport_errors,
                "retries": self.retries,
                "rate_limited_per_hour": round(self.rate_limited / elapsed * 3600, 2),
                "server_errors_per_hour": round(self.server_errors / elapsed * 3600, 2),
            }


# Process-wide instances. The engine and the loops are singletons per process,
# so a module-level registry is the honest shape; passing them through every
# constructor would only obscure that.
heartbeats = Heartbeats()
upstream = UpstreamCounters()


def reset_for_tests() -> None:
    global heartbeats, upstream
    heartbeats = Heartbeats()
    upstream = UpstreamCounters()
