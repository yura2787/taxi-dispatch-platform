"""
Simulator-wide test fixtures: a virtual clock, and in-memory stand-ins for the
realtime server and OSRM, so bots run for minutes of virtual time in milliseconds.

A local fixture with the same name shadows the one here.
"""

import asyncio
import heapq
import itertools
import json

import pytest

from sim.clock import Clock
from sim.connection import Closed, ConnectFailed
from sim.geo import Point
from sim.routes import Route

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


# ---------------------------------------------------------------------------
# Realtime server in memory
# ---------------------------------------------------------------------------


class FakeConnection:
    """One bot connection to FakeServer: what the bot sent, what it will receive."""

    def __init__(self, server: "FakeServer"):
        self.server = server
        self.sent: list[dict] = []
        self.inbox: asyncio.Queue[str | Closed] = asyncio.Queue()
        # Set by the bot: the code it closed with, and whether it dropped the TCP.
        self.closed_with: int | None = None
        self.aborted = False
        # Set by the server side: sends fail with this once the server closed.
        self._gone: Closed | None = None

    # --- the bot's side (sim.connection.Connection) ---

    async def send(self, text: str) -> None:
        if self._gone is not None:
            raise self._gone
        message = json.loads(text)
        self.sent.append(message)
        self.server.handle(self, message)

    async def recv(self) -> str:
        item = await self.inbox.get()
        if isinstance(item, Closed):
            raise item
        return item

    async def close(self, code: int) -> None:
        self.closed_with = code
        self._gone = Closed(code)

    def abort(self) -> None:
        self.aborted = True
        self._gone = Closed(None)

    # --- the server's side ---

    def push(self, message_type: str, **data) -> None:
        self.inbox.put_nowait(json.dumps({"type": message_type, "data": data}))

    def drop(self, code: int | None = None) -> None:
        """The server closes with `code`; None is a connection lost without a frame."""
        self._gone = Closed(code)
        self.inbox.put_nowait(self._gone)

    def sent_types(self) -> list[str]:
        return [message["type"] for message in self.sent]


class FakeServer:
    """Answers like /rt/ws/driver: snapshots, pongs, not_online for an offline driver.

    `status` and `position` are the driver's state on the server; tests change them
    (e.g. status = "offline" is the reaper). Message types in `silent` get no answer.
    """

    def __init__(self, *, status: str = "offline", position: Point | None = None):
        self.status = status
        self.position = position
        self.silent: set[str] = set()
        # A close code to answer auth with, or "drop" to lose the connection instead.
        self.close_after_auth: int | str | None = None
        # This many connection attempts fail before one succeeds.
        self.refuse_connections = 0
        self.connections: list[FakeConnection] = []
        self.connect_attempts = 0

    async def connect(self) -> FakeConnection:
        self.connect_attempts += 1
        if self.refuse_connections:
            self.refuse_connections -= 1
            raise ConnectFailed("connection refused")
        connection = FakeConnection(self)
        self.connections.append(connection)
        return connection

    def handle(self, connection: FakeConnection, message: dict) -> None:
        message_type, data = message["type"], message["data"]
        if message_type in self.silent:
            return
        if message_type == "auth":
            if self.close_after_auth == "drop":
                connection.drop(None)
            elif self.close_after_auth is not None:
                connection.drop(self.close_after_auth)
            else:
                self.send_snapshot(connection)
        elif message_type == "go_online":
            self.status = "available"
            self.send_snapshot(connection)
        elif message_type == "go_offline":
            self.status = "offline"
            self.send_snapshot(connection)
        elif message_type == "ping":
            connection.push("pong")
        elif message_type == "location":
            if self.status != "available":
                connection.push("error", code="not_online", message="go online first")
                self.send_snapshot(connection)
            else:
                self.position = Point(data["lat"], data["lon"])

    def send_snapshot(self, connection: FakeConnection) -> None:
        position = None
        if self.position is not None:
            position = {"lat": self.position.lat, "lon": self.position.lon}
            position |= {"heading": None, "speed": None}
        connection.push(
            "state_snapshot",
            status=self.status,
            tariff="economy",
            in_zone=True,
            position=position,
        )

    def sent(self, message_type: str) -> list[dict]:
        """Data of every message of this type the bot sent, over all connections."""
        return [
            message["data"]
            for connection in self.connections
            for message in connection.sent
            if message["type"] == message_type
        ]


class FakeOsrm:
    """Snaps nothing and routes in straight lines; can be told to fail.

    `failure` is raised by every call while set. `leave_area` bends every route
    through a point outside the service area.
    """

    def __init__(self, clock: FakeClock):
        self._clock = clock
        self.failure: Exception | None = None
        self.leave_area = False
        self.routes: list[Route] = []
        self.calls: list[tuple[str, float]] = []

    async def nearest(self, point: Point) -> Point:
        self._call("nearest")
        return point

    async def route(self, origin: Point, destination: Point) -> Route:
        self._call("route")
        middle = Point((origin.lat + destination.lat) / 2, (origin.lon + destination.lon) / 2)
        if self.leave_area:
            middle = Point(middle.lat, 27.0)
        route = Route(geometry=(origin, middle, destination))
        self.routes.append(route)
        return route

    def _call(self, name: str) -> None:
        self.calls.append((name, self._clock.elapsed))
        if self.failure is not None:
            raise self.failure


@pytest.fixture
def server() -> FakeServer:
    return FakeServer()


@pytest.fixture
def osrm(clock) -> FakeOsrm:
    return FakeOsrm(clock)
