"""Per-key asyncio locks (one lock per user, per token, ...).

The engine used one process-wide asyncio.Lock for every BUY and every risk
settings write. A pause tapped by one user waited behind any other user's
in-flight order, including its upstream retries and its Telegram alert, and
no two users could ever execute at the same time. Correctness only ever
needed serialization per user: the database already fences each user's row
(FOR UPDATE / BEGIN IMMEDIATE) and its claims.

Locks are created on first use and dropped once nobody holds or waits for
them, so a key space that only grows (every token ever traded) stays bounded
by what is in flight.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Hashable


class KeyedLocks:
    def __init__(self) -> None:
        # key -> [lock, holders + waiters]
        self._entries: dict[Hashable, list] = {}

    @asynccontextmanager
    async def hold(self, key: Hashable):
        entry = self._entries.get(key)
        if entry is None:
            entry = self._entries[key] = [asyncio.Lock(), 0]
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0 and self._entries.get(key) is entry:
                del self._entries[key]

    def __len__(self) -> int:
        return len(self._entries)
