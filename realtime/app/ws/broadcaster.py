"""Snapshots of the city for the dispatcher map pages, inside the gateway process.

Nothing to do with the dispatcher process (app.dispatcher, the reaper): "dispatcher"
here is the person at the map, an admin.

Backpressure is "latest wins": every page has a SnapshotSlot that holds at most one
snapshot. A new one replaces the one the page has not taken yet, so a slow page gets
fewer snapshots, never an ever-growing queue of old ones, and it never slows the
others down. The slot is only filled from outside; the page's own session task is the
only one that sends on its socket (two tasks sending on one WebSocket is unsafe).
"""

import asyncio

from app.metrics import DISPATCHER_SNAPSHOT_SKIPPED


class SnapshotSlot:
    """The latest snapshot for one page, and a wake-up for its session."""

    def __init__(self) -> None:
        self._text: str | None = None
        self._ready = asyncio.Event()

    def put(self, text: str) -> None:
        """Replace the snapshot the page has not taken yet and wake its session."""
        if self._text is not None:
            DISPATCHER_SNAPSHOT_SKIPPED.inc()
        self._text = text
        self._ready.set()

    async def wait(self) -> str:
        """Wait for a snapshot and take it.

        Safe to cancel: the snapshot is taken only after the wait, with no await in
        between, so a cancelled wait leaves it in the slot for the next one. Meant for
        one waiter (the page's session); should another be woken with nothing left,
        it waits again.
        """
        while self._text is None:
            self._ready.clear()
            await self._ready.wait()
        text, self._text = self._text, None
        self._ready.clear()
        return text
