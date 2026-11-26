"""Snapshots of the city for the dispatcher map pages, inside the gateway process.

Nothing to do with the dispatcher process (app.dispatcher, the reaper): "dispatcher"
here is the person at the map, an admin.

Backpressure is "latest wins": every page has a SnapshotSlot that holds at most one
snapshot. A new one replaces the one the page has not taken yet, so a slow page gets
fewer snapshots, never an ever-growing queue of old ones, and it never slows the
others down. The slot is only filled from outside; the page's own session task is the
only one that sends on its socket (two tasks sending on one WebSocket is unsafe).

The snapshot is built once per tick for the whole process, however many pages watch:
one Redis read, one JSON string, the same string in every slot.

Stage 5 (several gateway instances): each would read Redis for the same snapshot. Then
the dispatcher process builds it once and shares it (pub/sub or a key), and tick()
takes the ready one instead of building it; the slots and sessions stay as they are.
"""

import asyncio
import logging
from typing import Protocol

from app import config
from app.clock import Clock
from app.metrics import (
    DISPATCHER_SNAPSHOT_BUILD,
    DISPATCHER_SNAPSHOT_BYTES,
    DISPATCHER_SNAPSHOT_DRIVERS,
    DISPATCHER_SNAPSHOT_FAILURES,
    DISPATCHER_SNAPSHOT_SKIPPED,
)
from app.services.drivers import LiveDrivers
from app.services.snapshot import build_dispatcher_snapshot

logger = logging.getLogger(__name__)


class LiveDriversSource(Protocol):
    """Where the drivers come from: DriverStore, or a stand-in in tests."""

    async def live_drivers(self) -> LiveDrivers: ...


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


class TooManySubscribers(Exception):
    """DISPATCHER_MAX_CONNECTIONS pages already watch in this process."""


class SnapshotBroadcaster:
    """Builds the dispatcher snapshot once per tick and hands it to every page."""

    def __init__(self, store: LiveDriversSource, clock: Clock) -> None:
        self._store = store
        self._clock = clock
        self._slots: set[SnapshotSlot] = set()
        # The last snapshot built, for a page that subscribes between ticks.
        self._latest: str | None = None
        # When the current run of failures was last logged; None while healthy.
        self._failure_logged_at: float | None = None

    @property
    def subscribers(self) -> int:
        return len(self._slots)

    def subscribe(self) -> SnapshotSlot:
        """A slot for a new page, holding the last snapshot at once if there is one.

        Raises TooManySubscribers above DISPATCHER_MAX_CONNECTIONS.
        """
        if len(self._slots) >= config.DISPATCHER_MAX_CONNECTIONS:
            raise TooManySubscribers
        slot = SnapshotSlot()
        if self._latest is not None:
            slot.put(self._latest)
        self._slots.add(slot)
        return slot

    def unsubscribe(self, slot: SnapshotSlot) -> None:
        """Forget the page's slot; safe to call more than once."""
        self._slots.discard(slot)
        if not self._slots:
            # Nobody watches, so no ticks until somebody does: the next first page
            # must get a fresh snapshot, not one from whenever the last page left.
            self._latest = None

    async def tick(self) -> bool:
        """Build one snapshot and put it into every slot. False if it failed.

        A failure is counted and logged (at most every DISPATCHER_FAILURE_LOG_INTERVAL_S)
        and never raised: the pages keep their last snapshot, its server_ms shows its
        age, and the next tick tries again. One caller at a time (the loop): two
        overlapping ticks could finish out of order and hand out the older snapshot.
        """
        started = self._clock.monotonic()
        try:
            live = await self._store.live_drivers()
            text = build_dispatcher_snapshot(live, self._clock.now_ms()).model_dump_json()
        except Exception:
            self._failed()
            return False
        DISPATCHER_SNAPSHOT_BUILD.observe(self._clock.monotonic() - started)
        DISPATCHER_SNAPSHOT_DRIVERS.set(len(live.drivers))
        DISPATCHER_SNAPSHOT_BYTES.set(len(text.encode()))

        # Kept only while someone watches: if the last page left during the read, the
        # snapshot would otherwise wait, ever older, for the next first page.
        if self._slots:
            self._latest = text
        # The same string for every page: serialized once, whatever their number.
        for slot in self._slots:
            slot.put(text)
        self._recovered()
        return True

    def _failed(self) -> None:
        DISPATCHER_SNAPSHOT_FAILURES.inc()
        now = self._clock.monotonic()
        logged_at = self._failure_logged_at
        if logged_at is None or now - logged_at >= config.DISPATCHER_FAILURE_LOG_INTERVAL_S:
            self._failure_logged_at = now
            logger.warning("Dispatcher snapshot failed", exc_info=True)

    def _recovered(self) -> None:
        if self._failure_logged_at is not None:
            self._failure_logged_at = None
            logger.info("Dispatcher snapshots recovered")
