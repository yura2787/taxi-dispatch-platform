"""
Simulator-wide test fixtures.

Only universal fixtures live here; domain ones (connections, bots) belong in the test
module that needs them. A local fixture with the same name shadows the one here.
"""

import asyncio
import heapq
import itertools

import pytest

from sim.clock import Clock

# Rounds of the event loop with no new sleep and no wake-up after which the tasks
# are taken as settled and the time may move: enough for a reply to travel through an
# in-memory connection and a bot to react to it. Real sockets need real time: tests
# against a local websockets server use the real Clock.
SETTLE_ROUNDS = 20


class FakeClock(Clock):
    """Virtual time: a sleep() returns as soon as everything else has settled, as if
    the time had passed.

    Sleepers wait on their own future; one scheduler task moves the time to the
    earliest deadline and wakes that sleeper, but only after SETTLE_ROUNDS quiet loop
    rounds. Any sleep or wake-up restarts the count, so a woken bot always gets to
    react (send, receive, fall asleep again) before the time moves on. A bot ticking
    every 2 s and pinging every 20 s thus sees the timeline of real life, without the
    waiting.

    asyncio's own timeouts (asyncio.timeout, wait_for) run on the real loop time and
    know nothing about this clock. Every requested delay is recorded in `sleeps`.
    """

    # Any realistic moment: 2026-01-01 00:00 UTC.
    START_MS = 1_767_225_600_000

    def __init__(self) -> None:
        self.elapsed = 0.0
        self.sleeps: list[float] = []
        self._sleepers: list[tuple[float, int, asyncio.Future[None]]] = []
        self._order = itertools.count()
        # Bumped by every sleep and every wake-up: the scheduler moves the time only
        # after SETTLE_ROUNDS rounds without a change.
        self._activity = 0
        self._scheduler: asyncio.Task[None] | None = None

    def now_ms(self) -> int:
        return self.START_MS + round(self.elapsed * 1000)

    def monotonic(self) -> float:
        return 1_000.0 + self.elapsed

    def advance(self, seconds: float) -> None:
        """Move the time without sleeping, e.g. between two messages of a test."""
        self.elapsed += seconds

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        wake = asyncio.get_running_loop().create_future()
        deadline = self.elapsed + max(seconds, 0.0)
        heapq.heappush(self._sleepers, (deadline, next(self._order), wake))
        self._activity += 1
        if self._scheduler is None or self._scheduler.done():
            self._scheduler = asyncio.create_task(self._run())
        # Cancelled while waiting: the future is cancelled with the task, and the
        # scheduler skips the entry.
        await wake

    async def _run(self) -> None:
        while self._live_sleepers():
            activity = self._activity
            for _ in range(SETTLE_ROUNDS):
                await asyncio.sleep(0)
            if self._activity == activity:
                self._wake_the_earliest()

    def _live_sleepers(self) -> bool:
        while self._sleepers and self._sleepers[0][2].done():
            heapq.heappop(self._sleepers)
        return bool(self._sleepers)

    def _wake_the_earliest(self) -> None:
        if not self._live_sleepers():
            return
        deadline, _, wake = heapq.heappop(self._sleepers)
        self.elapsed = max(self.elapsed, deadline)
        self._activity += 1
        wake.set_result(None)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()
