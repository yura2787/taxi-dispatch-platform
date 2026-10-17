"""A driver bot: behaves like the phone of a real driver.

Life of a bot (ТЗ 08, section 5):
1. connect, send auth at once, wait for state_snapshot; go online if offline;
2. every LOCATION_INTERVAL_S send where the car is, every PING_INTERVAL_S a ping;
3. on a close: stop for good (4401, 4403, 4409, 1008, 1009) or reconnect after a
   random delay that doubles with every failed attempt ("full jitter");
4. on stop: go offline, wait for the snapshot, close with 1000.

Every 5-20 minutes a bot takes a break: offline for a few minutes, the connection
open, only pings. A flaky bot (a share of them) now and then drops its connection
without a close frame or falls silent: live load for the reaper and for reconnects.

The car itself (Car + the drive task) lives for the whole bot, not per connection:
it keeps driving while the bot reconnects, so the bot never jumps.
"""

import asyncio
import contextlib
import enum
import logging
import random
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sim import config, protocol
from sim.clock import Clock
from sim.connection import Closed, ConnectFailed, Connection
from sim.geo import Point, haversine_m, random_point_in
from sim.osrm import NoRoute, OsrmClient, OsrmUnavailable
from sim.protocol import CloseVerdict, ErrorVerdict
from sim.routes import Route, RouteWalker

logger = logging.getLogger(__name__)

Connect = Callable[[], Awaitable[Connection]]

AVAILABLE = "available"
OFFLINE = "offline"
# Statuses of a driver with an order: impossible before stage 3, so a bot that sees
# one is talking to a server it does not understand.
BUSY_STATUSES = frozenset({"offered", "on_trip"})

# Share of drops among the misbehaviours of a flaky bot; the rest fall silent.
DROP_SHARE = 0.5

# The goodbye connection after a stop: within the runner's stop budget, which is
# SHUTDOWN_TIMEOUT_S + 2.
GOODBYE_TIMEOUT_S = config.SHUTDOWN_TIMEOUT_S + 1

_STOP_LOG_LEVEL = {
    4401: logging.ERROR,
    4403: logging.ERROR,
    4409: logging.WARNING,
}


class BotStopped(Exception):
    """The bot must stop for good: reconnecting would not help."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class HandshakeTimeout(Exception):
    """No state_snapshot in time after auth or go_online."""


class Dropped(Exception):
    """A flaky bot dropped its connection on purpose and comes back after `seconds`."""

    def __init__(self, seconds: float):
        super().__init__(f"dropped for {seconds:.0f}s")
        self.seconds = seconds


class Misbehaviour(enum.Enum):
    # Lose the connection without a close frame, like a phone in a tunnel.
    DROP = "drop"
    # Send no points for SILENCE_S (pings go on), like a frozen GPS.
    SILENCE = "silence"


# What may end a goodbye connection: the server, not a bug of the simulator.
_GOODBYE_FAILURES = (ConnectFailed, HandshakeTimeout, Closed, BotStopped)


@dataclass
class BotStats:
    """What one bot has done; the runner adds up the bots for the report."""

    locations_sent: int = 0
    # Error codes from the server, e.g. {"location_jump": 1}.
    errors: Counter[str] = field(default_factory=Counter)
    # Close codes from the server; a dropped connection counts as 1006.
    closes: Counter[int] = field(default_factory=Counter)
    connect_failures: int = 0
    handshake_timeouts: int = 0
    reconnects: int = 0
    unknown_messages: int = 0
    # Ping -> pong, measured from the moment the ping was scheduled, so a late send
    # (a busy event loop) shows up in the numbers instead of hiding (no coordinated
    # omission).
    rtt_s: list[float] = field(default_factory=list)
    # Pings without a pong by the time of the next one: stalls the RTT samples miss.
    pongs_missed: int = 0
    # Tried to connect at all: a bot stopped during the ramp-up never does.
    started: bool = False
    went_online: bool = False
    breaks: int = 0
    drops: int = 0
    silences: int = 0
    # OSRM calls that failed (start position or route); the car stood meanwhile.
    osrm_failures: int = 0
    # Stops after which the server may still hold the bot online, until the reaper:
    # go_offline not confirmed in time, or no goodbye connection after a drop.
    left_online: int = 0


class Car:
    """Where the bot is. Driven by the clock: the position is computed when asked,
    from the distance driven (speed x time) along the current route."""

    def __init__(self, position: Point):
        self._position = position
        self._heading: float | None = None
        self._walker: RouteWalker | None = None
        self._speed_mps = 0.0
        self._started_at = 0.0

    def where(self, now: float) -> tuple[Point, float | None, float]:
        """Point, heading (the last known one while standing) and speed in m/s."""
        if self._walker is None:
            return self._position, self._heading, 0.0
        position = self._walker.at(self._speed_mps * (now - self._started_at))
        if position.heading is not None:
            self._heading = position.heading
        if position.finished:
            return position.point, self._heading, 0.0
        return position.point, self._heading, self._speed_mps

    def drive(self, route: Route, speed_mps: float, now: float) -> float:
        """Start along the route; returns the seconds it takes."""
        self._walker = RouteWalker(route)
        self._speed_mps = speed_mps
        self._started_at = now
        return self._walker.length_m / speed_mps

    def park(self, now: float) -> None:
        self._position, self._heading, _ = self.where(now)
        self._walker = None


class DriverBot:
    def __init__(
        self,
        driver_id: int,
        *,
        connect: Connect,
        osrm: OsrmClient,
        clock: Clock,
        rng: random.Random,
        flaky: bool = False,
    ):
        self.driver_id = driver_id
        self.stats = BotStats()
        # For the runner's status line; connected means the socket is open.
        self.connected = False
        self.status: str | None = None
        self.stop_reason: str | None = None
        self.on_break = False
        self.flaky = flaky

        self._connect = connect
        self._osrm = osrm
        self._clock = clock
        # Seeded per (seed, driver_id) by the runner: the same bot makes the same
        # choices however many bots run.
        self._rng = rng
        # Ticks of bots are spread over the interval, not all at the same moment.
        self._phase = rng.random() * config.LOCATION_INTERVAL_S
        # Pings too: sessions start within the ramp-up (10 s), so a ping at start + 20 s
        # would bunch every bot's pings into half of each 20 s period.
        self._ping_phase = rng.random() * config.PING_INTERVAL_S

        self._car: Car | None = None
        self._drive_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._snapshot_received = asyncio.Event()
        self._online = asyncio.Event()
        self._offline = asyncio.Event()
        # When the bot last asked to go online and has had no "available" since.
        self._go_online_sent_at: float | None = None
        # The server may hold the bot online: set by "available" and by every
        # go_online, cleared only by "offline" or by giving up (left_online). Unlike
        # status it outlives the connection, so a stop between two connections still
        # knows it has to go offline.
        self._may_be_online = False
        self._last_snapshot: protocol.StateSnapshot | None = None
        # The bot goes online whenever the server says it is offline, unless it is
        # shutting down.
        self._wants_online = True
        self._last_ts = 0
        self._ping_scheduled_at: float | None = None
        self._failed_attempts = 0
        # Lifetime schedules on the monotonic clock, set when the bot first serves.
        self._break_at: float | None = None
        self._break_ends_at = 0.0
        self._misbehave_at: float | None = None
        self._silent_until = 0.0

    @property
    def position(self) -> Point | None:
        if self._car is None:
            return None
        return self._car.where(self._clock.monotonic())[0]

    def stop(self) -> None:
        """Ask the bot to go offline and leave; run() returns when it has."""
        self._stopping.set()

    async def run(self) -> None:
        try:
            while not self._stopping.is_set():
                try:
                    await self._session()
                except ConnectFailed as exc:
                    self.stats.connect_failures += 1
                    logger.warning("driver %d: cannot connect: %s", self.driver_id, exc)
                except HandshakeTimeout as exc:
                    self.stats.handshake_timeouts += 1
                    logger.warning("driver %d: %s, reconnecting", self.driver_id, exc)
                except Closed as exc:
                    self._on_closed(exc.code)
                except Dropped as exc:
                    # Back without a backoff: the drop was the bot's own idea.
                    await self._sleep_unless_stopped(exc.seconds)
                    continue
                else:
                    break
                if not self._stopping.is_set():
                    self.stats.reconnects += 1
                    await self._backoff()
            # Stopped without a confirmed offline: away from the server (a drop, a
            # reconnect delay), before the snapshot of a new connection, or closed by
            # the server during the shutdown.
            await self._say_goodbye()
        except BotStopped as exc:
            # No goodbye after a fatal stop: the server refused the bot for good (or
            # another connection holds the driver), and the stop is loud anyway:
            # an ERROR or WARNING line, stop_reason in the report, --strict fails.
            self.stop_reason = exc.reason
        finally:
            self.connected = False
            if self._drive_task is not None:
                self._drive_task.cancel()
                # gather, not suppress(CancelledError): a cancel of run() itself must
                # not be swallowed here.
                await asyncio.gather(self._drive_task, return_exceptions=True)

    # --- One connection -------------------------------------------------------

    async def _say_goodbye(self) -> None:
        """If the server may still hold the bot online (its last point is fresh, the
        reaper has not come yet), connect once more just to go offline: after a stop
        no bot may be left in drivers:last_seen (5.7)."""
        if not self._may_be_online:
            return
        # Bounded, so a slow or absent server never exceeds the runner's stop budget
        # (SHUTDOWN_TIMEOUT_S + 2); the budget covers connect, auth and go_offline.
        goodbye = asyncio.create_task(self._session(goodbye=True))
        deadline = asyncio.create_task(self._clock.sleep(GOODBYE_TIMEOUT_S))
        try:
            await asyncio.wait({goodbye, deadline}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (goodbye, deadline):
                task.cancel()
            outcome, _ = await asyncio.gather(goodbye, deadline, return_exceptions=True)
        if isinstance(outcome, Closed):
            self._count_close(outcome.code)
        if isinstance(outcome, asyncio.CancelledError):
            self._left_online("could not go offline after the stop (timed out)")
        elif isinstance(outcome, _GOODBYE_FAILURES):
            self._left_online(f"could not go offline after the stop ({outcome!r})")
        elif outcome is not None:
            raise outcome  # a bug, not the server: keep its traceback
        elif self._may_be_online:
            # The session ended quietly, but without an "offline": closed by the
            # server while waiting for it.
            self._left_online("could not go offline after the stop (closed)")

    def _left_online(self, why: str) -> None:
        """Give up going offline: counted once, and no further goodbye is tried."""
        self._may_be_online = False
        self.stats.left_online += 1
        logger.warning("driver %d: %s; online until the reaper takes it", self.driver_id, why)

    async def _session(self, *, goodbye: bool = False) -> None:
        """Returns once the bot has stopped; raises when the connection ends.

        A goodbye session only authenticates and goes offline.
        """
        self.stats.started = True
        conn = await self._connect()
        self.connected = True
        self.status = None
        self._snapshot_received.clear()
        self._ping_scheduled_at = None
        self._go_online_sent_at = None
        receiver = asyncio.create_task(self._receive(conn))
        stopping = asyncio.create_task(self._stopping.wait())
        ended = False
        try:
            if goodbye:
                self._wants_online = False
                await self._authenticate(conn, receiver, stopping=None)
            elif await self._handshake(conn, receiver, stopping):
                await self._serve(conn, receiver, stopping)
            await self._shutdown(conn, receiver)
            ended = True
        except BotStopped:
            # Stopped by what the server said, while the connection is still open.
            with contextlib.suppress(Closed):
                await conn.close(1000)
            ended = True
            raise
        except (Closed, Dropped):
            ended = True  # closed by the server, or dropped on purpose
            raise
        finally:
            if not ended:
                # A timeout, a cancel or a bug: drop the socket instead of leaving it
                # open, and without waiting for a server that may not answer.
                conn.abort()
            self.connected = False
            for task in (receiver, stopping):
                task.cancel()
            await asyncio.gather(receiver, stopping, return_exceptions=True)

    async def _wait(
        self,
        receiver: asyncio.Task[None],
        *,
        seconds: float,
        event: asyncio.Event | None = None,
        stopping: asyncio.Task[bool] | None = None,
    ) -> str:
        """Wait for the event, a stop or the time to pass, whichever comes first:
        "event", "stop" or "timeout". Raises what ended the receiver (a close)."""
        sleep = asyncio.create_task(self._clock.sleep(max(seconds, 0.0)))
        waiters: set[asyncio.Future] = {receiver, sleep}
        event_task = None
        if event is not None:
            event_task = asyncio.create_task(event.wait())
            waiters.add(event_task)
        if stopping is not None:
            waiters.add(stopping)
        helpers = [task for task in (sleep, event_task) if task is not None]
        try:
            done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in helpers:
                task.cancel()
            await asyncio.gather(*helpers, return_exceptions=True)
        if receiver in done:
            receiver.result()  # raises: the receiver ends only with a close or a stop
        if event_task is not None and event_task in done:
            return "event"
        if stopping is not None and stopping in done:
            return "stop"
        return "timeout"

    async def _handshake(
        self, conn: Connection, receiver: asyncio.Task[None], stopping: asyncio.Task[bool]
    ) -> bool:
        """Auth, then online. False if asked to stop meanwhile."""
        if not await self._authenticate(conn, receiver, stopping):
            return False
        snapshot = self._last_snapshot
        assert snapshot is not None  # set together with _snapshot_received
        if self._car is None:
            await self._place_car(snapshot.position)

        # Going online is the receiver's job (see _on_snapshot); here we only wait.
        result = await self._wait_online_unless_on_break(receiver, stopping)
        if result == "timeout":
            raise HandshakeTimeout("not online after go_online")
        if result == "stop":
            return False
        # Not on a break: a bot that reconnects mid-break has not been online yet.
        if self._online.is_set():
            self.stats.went_online = True
        return True

    async def _authenticate(
        self, conn: Connection, receiver: asyncio.Task[None], stopping: asyncio.Task[bool] | None
    ) -> bool:
        """Send auth and wait for the first snapshot. False if asked to stop meanwhile."""
        await conn.send(protocol.auth(protocol.token_for(self.driver_id)))
        result = await self._wait(
            receiver,
            seconds=config.CONNECT_TIMEOUT_S,
            event=self._snapshot_received,
            stopping=stopping,
        )
        if result == "timeout":
            raise HandshakeTimeout("no state_snapshot after auth")
        if result == "stop":
            return False
        # The spec resets the backoff here. Known risk: a server that answers auth and
        # then fails at once keeps every bot reconnecting within a second.
        self._failed_attempts = 0
        return True

    async def _wait_online_unless_on_break(
        self, receiver: asyncio.Task[None], stopping: asyncio.Task[bool]
    ) -> str:
        if self.on_break:
            # Reconnected during a break: stays offline until the break is over.
            return "event"
        return await self._wait(
            receiver, seconds=config.CONNECT_TIMEOUT_S, event=self._online, stopping=stopping
        )

    async def _serve(
        self, conn: Connection, receiver: asyncio.Task[None], stopping: asyncio.Task[bool]
    ) -> None:
        """Ticks and pings on a fixed schedule until asked to stop."""
        now = self._clock.monotonic()
        next_tick = now + self._phase
        # Within the first interval, so the server's idle timeout is never at risk.
        next_ping = now + self._ping_phase
        while True:
            wake = min(next_tick, next_ping)
            result = await self._wait(
                receiver, seconds=wake - self._clock.monotonic(), stopping=stopping
            )
            if result == "stop":
                return
            now = self._clock.monotonic()
            if now >= next_ping:
                await self._ping(conn, scheduled_at=next_ping)
                next_ping = _next_slot(next_ping, config.PING_INTERVAL_S, now)
            if now >= next_tick:
                # Breaks and misbehaviour are checked on ticks: a moment 2 s late does
                # not matter, and no extra timer can make the loop spin.
                await self._update_break(conn, now)
                self._maybe_misbehave(conn, now)
                self._check_back_online(now)
                await self._send_location(conn, now)
                next_tick = _next_slot(next_tick, config.LOCATION_INTERVAL_S, now)

    def _check_back_online(self, now: float) -> None:
        """A bot the server does not let back online (no answer, an error instead of
        the snapshot) would stand forever sending only pings: reconnect instead."""
        sent_at = self._go_online_sent_at
        if sent_at is not None and now - sent_at > config.CONNECT_TIMEOUT_S:
            raise HandshakeTimeout("not back online after go_online")

    async def _ping(self, conn: Connection, *, scheduled_at: float) -> None:
        if self._ping_scheduled_at is not None:
            # No pong for a whole interval: count it, and let this ping replace it, so
            # one lost pong does not end the RTT samples. One ping in flight at most.
            self.stats.pongs_missed += 1
        self._ping_scheduled_at = scheduled_at
        await conn.send(protocol.ping())

    async def _update_break(self, conn: Connection, now: float) -> None:
        if self._break_at is None:
            self._break_at = now + self._rng.uniform(*config.BREAK_EVERY_S)
        if self.on_break and now >= self._break_ends_at:
            self.on_break = False
            self._wants_online = True
            self._break_at = now + self._rng.uniform(*config.BREAK_EVERY_S)
            if self.flaky and self._misbehave_at is not None:
                # Not right after the break: a misbehaviour due during it waits for
                # FLAKY_EVERY_S of driving like any other.
                earliest = now + self._rng.uniform(*config.FLAKY_EVERY_S)
                self._misbehave_at = max(self._misbehave_at, earliest)
            self._start_driving()
            self._go_online_sent_at = now
            self._may_be_online = True
            await conn.send(protocol.go_online())
            logger.info("driver %d: break over, going online", self.driver_id)
        elif (
            not self.on_break
            and now >= self._break_at
            and self.status == AVAILABLE
            # A silent bot may already be offline on the server: no break mid-silence.
            and now >= self._silent_until
        ):
            duration = self._rng.uniform(*config.BREAK_DURATION_S)
            self.on_break = True
            self._wants_online = False
            self._break_ends_at = now + duration
            self.stats.breaks += 1
            await self._stop_driving()
            await conn.send(protocol.go_offline())
            logger.info("driver %d: break for %.0fs", self.driver_id, duration)

    def _maybe_misbehave(self, conn: Connection, now: float) -> None:
        if not self.flaky:
            return
        if self._misbehave_at is None:
            self._misbehave_at = now + self._rng.uniform(*config.FLAKY_EVERY_S)
        silent = now < self._silent_until
        if now < self._misbehave_at or self.status != AVAILABLE or self.on_break or silent:
            return
        # The next one counts from the end of this one: FLAKY_EVERY_S of normal driving.
        if self._choose_misbehaviour() is Misbehaviour.SILENCE:
            self.stats.silences += 1
            self._silent_until = now + config.SILENCE_S
            self._misbehave_at = self._silent_until + self._rng.uniform(*config.FLAKY_EVERY_S)
            logger.info("driver %d: silent for %ds", self.driver_id, config.SILENCE_S)
            return
        seconds = self._rng.uniform(*config.DROP_DURATION_S)
        self._misbehave_at = now + seconds + self._rng.uniform(*config.FLAKY_EVERY_S)
        self.stats.drops += 1
        logger.info("driver %d: dropping the connection for %.0fs", self.driver_id, seconds)
        # No close frame: the server sees what it sees when a phone enters a tunnel.
        conn.abort()
        raise Dropped(seconds)

    def _choose_misbehaviour(self) -> Misbehaviour:
        return Misbehaviour.DROP if self._rng.random() < DROP_SHARE else Misbehaviour.SILENCE

    async def _send_location(self, conn: Connection, now: float) -> None:
        # On a break the status says "available" until the go_offline is answered.
        if self.status != AVAILABLE or self.on_break or now < self._silent_until:
            return
        assert self._car is not None  # placed in the handshake, before going online
        point, heading, speed = self._car.where(now)
        # Strictly increasing, even if the wall clock stands still or steps back.
        self._last_ts = max(self._clock.now_ms(), self._last_ts + 1)
        await conn.send(protocol.location(point, heading=heading, speed=speed, ts=self._last_ts))
        self.stats.locations_sent += 1

    async def _shutdown(self, conn: Connection, receiver: asyncio.Task[None]) -> None:
        """Go offline (so the server does not wait for the reaper) and close with 1000.

        Also when a go_online is still unanswered: the server may be putting the
        driver online right now, and go_offline comes after it. Before the first
        snapshot the connection is not authenticated yet: it only closes, and the
        goodbye after it (see run) goes offline.
        """
        self._wants_online = False
        try:
            if receiver.done():
                receiver.result()  # raises how the connection ended
            if self._may_be_online and self._snapshot_received.is_set():
                self._offline.clear()  # an older "offline" must not end the wait
                # On a break go_offline is already on its way on this connection:
                # sent when the break began, or on the snapshot of a reconnect.
                if not self.on_break:
                    await conn.send(protocol.go_offline())
                result = await self._wait(
                    receiver, seconds=config.SHUTDOWN_TIMEOUT_S, event=self._offline
                )
                if result == "timeout":
                    self._left_online("go_offline not confirmed")
            await conn.close(1000)
        except Closed as exc:
            if protocol.classify_close(exc.code) is not CloseVerdict.RECONNECT:
                # Closed for good: a goodbye would be refused or, after 4409, take
                # the driver offline from the connection that has taken it over.
                self._on_closed(exc.code)  # counts, logs and raises BotStopped
            # The server closed first: nothing left to close, only to count. Not
            # confirmed offline, the bot still says goodbye (see run).
            code = self._count_close(exc.code)
            logger.info("driver %d: closed with %d while stopping", self.driver_id, code)

    # --- Messages from the server ---------------------------------------------

    async def _receive(self, conn: Connection) -> None:
        while True:
            message = protocol.parse_server_message(await conn.recv())
            match message:
                case protocol.StateSnapshot():
                    await self._on_snapshot(conn, message)
                case protocol.Pong():
                    self._on_pong()
                case protocol.ServerError(code=code):
                    self._on_error(code)
                case protocol.UnknownMessage(type=message_type):
                    self.stats.unknown_messages += 1
                    logger.warning(
                        "driver %d: unknown or malformed message %r", self.driver_id, message_type
                    )

    async def _on_snapshot(self, conn: Connection, snapshot: protocol.StateSnapshot) -> None:
        self._last_snapshot = snapshot
        self.status = snapshot.status
        if snapshot.status in BUSY_STATUSES:
            logger.error("driver %d: unexpected status %s", self.driver_id, snapshot.status)
            raise BotStopped(f"status {snapshot.status}")
        if snapshot.status == AVAILABLE:
            self._online.set()
            self._go_online_sent_at = None
            self._may_be_online = True
        else:
            self._online.clear()
        if snapshot.status == OFFLINE:
            self._offline.set()
            self._may_be_online = False
        else:
            self._offline.clear()
        self._snapshot_received.set()
        if snapshot.status == AVAILABLE and self.on_break:
            # Reconnected during a break whose go_offline was lost with the connection.
            await conn.send(protocol.go_offline())
        # After auth, and after the reaper took the bot offline (not_online + snapshot).
        if snapshot.status == OFFLINE and self._wants_online:
            if self._go_online_sent_at is None:
                self._go_online_sent_at = self._clock.monotonic()
            self._may_be_online = True
            await conn.send(protocol.go_online())

    def _on_pong(self) -> None:
        if self._ping_scheduled_at is None:
            return
        self.stats.rtt_s.append(self._clock.monotonic() - self._ping_scheduled_at)
        self._ping_scheduled_at = None

    def _on_error(self, code: str) -> None:
        self.stats.errors[code] += 1
        match protocol.classify_error(code):
            case ErrorVerdict.NOT_ONLINE:
                logger.info("driver %d: not_online (taken offline), going online", self.driver_id)
            case ErrorVerdict.REJECTED_LOCATION:
                logger.warning("driver %d: point rejected: %s", self.driver_id, code)
            case ErrorVerdict.FATAL:
                logger.error("driver %d: server error %s, stopping", self.driver_id, code)
                raise BotStopped(f"error {code}")
            case ErrorVerdict.BUG:
                logger.error("driver %d: server error %s (simulator bug)", self.driver_id, code)

    def _on_closed(self, code: int | None) -> None:
        """Count the close; raise BotStopped unless the bot should reconnect."""
        code = self._count_close(code)
        match protocol.classify_close(code):
            case CloseVerdict.FATAL:
                level = _STOP_LOG_LEVEL[code]
                hint = ": run make seed-drivers" if code == 4403 else ""
                logger.log(
                    level, "driver %d: closed with %d, stopping%s", self.driver_id, code, hint
                )
                raise BotStopped(f"closed {code}")
            case CloseVerdict.BUG:
                logger.error("driver %d: closed with %d (simulator bug)", self.driver_id, code)
                raise BotStopped(f"closed {code}")
            case CloseVerdict.RECONNECT:
                logger.warning("driver %d: closed with %d, reconnecting", self.driver_id, code)

    def _count_close(self, code: int | None) -> int:
        """Count a close by the server; a connection lost without a frame is 1006."""
        code = protocol.ABNORMAL_CLOSURE if code is None else code
        self.stats.closes[code] += 1
        return code

    async def _backoff(self) -> None:
        delay = reconnect_delay(self._failed_attempts, self._rng)
        self._failed_attempts += 1
        await self._sleep_unless_stopped(delay)

    async def _sleep_unless_stopped(self, seconds: float) -> None:
        stopping = asyncio.create_task(self._stopping.wait())
        sleep = asyncio.create_task(self._clock.sleep(seconds))
        try:
            await asyncio.wait({stopping, sleep}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopping.cancel()
            sleep.cancel()
            await asyncio.gather(stopping, sleep, return_exceptions=True)

    # --- Driving ---------------------------------------------------------------

    async def _place_car(self, snapshot_position: Point | None) -> None:
        """Where a new bot starts: its last position on the server if it makes sense,
        else a random place in the city on a road (or off it, without OSRM)."""
        if snapshot_position is not None and config.SERVICE_AREA.contains(snapshot_position):
            start = snapshot_position
        else:
            start = random_point_in(config.CITY_AREA, self._rng)
            try:
                start = await self._osrm.nearest(start)
            except (OsrmUnavailable, NoRoute) as exc:
                # Off the road, then: the first route snaps it back.
                self.stats.osrm_failures += 1
                # DEBUG: OSRM down at the start would mean a line per bot; the count
                # is in the summary.
                logger.debug("driver %d: no road for the start (%s)", self.driver_id, exc)
        self._car = Car(start)
        if not self.on_break:
            self._start_driving()

    def _start_driving(self) -> None:
        self._drive_task = asyncio.create_task(self._drive())

    async def _stop_driving(self) -> None:
        """Park the car where it is now (a break)."""
        if self._drive_task is not None:
            self._drive_task.cancel()
            await asyncio.gather(self._drive_task, return_exceptions=True)
            self._drive_task = None
        if self._car is not None:
            self._car.park(self._clock.monotonic())

    async def _drive(self) -> None:
        car = self._car
        assert car is not None  # driving starts only once the car is placed
        try:
            while True:
                route = await self._next_route()
                speed_mps = self._rng.uniform(*config.SPEED_KMH) / 3.6
                await self._clock.sleep(car.drive(route, speed_mps, self._clock.monotonic()))
                car.park(self._clock.monotonic())
                await self._clock.sleep(self._rng.uniform(*config.ROUTE_END_PAUSE_S))
        except Exception:
            # A bug in the motion code: without this the task would die silently and
            # the bot would stand still forever.
            logger.exception("driver %d: driving failed, stopping", self.driver_id)
            self.stop_reason = "driving failed"
            self.stop()

    async def _next_route(self) -> Route:
        """A route from where the car stands. While OSRM is down the car stands (and
        keeps sending points); the retry delay doubles up to OSRM_RETRY_MAX_S."""
        delay = 1.0
        warned = False
        while True:
            try:
                return await self._pick_route()
            except (OsrmUnavailable, NoRoute) as exc:
                self.stats.osrm_failures += 1
                # Once per outage at WARNING: 1000 bots retrying would drown the log.
                level = logging.DEBUG if warned else logging.WARNING
                warned = True
                logger.log(
                    level, "driver %d: no route (%s), retry in ~%.0fs", self.driver_id, exc, delay
                )
            # "Equal jitter": between half and the whole delay.
            await self._clock.sleep(delay / 2 + self._rng.uniform(0, delay / 2))
            delay = min(delay * 2, config.OSRM_RETRY_MAX_S)

    async def _pick_route(self) -> Route:
        assert self._car is not None
        origin, _, _ = self._car.where(self._clock.monotonic())
        last: Route | None = None
        for _ in range(config.ROUTE_ATTEMPTS):
            destination = random_point_in(config.CITY_AREA, self._rng)
            try:
                destination = await self._osrm.nearest(destination)
                if haversine_m(origin, destination) < config.ROUTE_MIN_DISTANCE_M:
                    continue
                route = (await self._osrm.route(origin, destination)).starting_at(origin)
            except NoRoute:
                continue  # another destination right away
            if all(config.SERVICE_AREA.contains(point) for point in route.geometry):
                return route
            last = route
        if last is None:
            raise NoRoute(f"no route in {config.ROUTE_ATTEMPTS} attempts")
        # Points outside the service area are still accepted by the server.
        return last


def reconnect_delay(failed_attempts: int, rng: random.Random) -> float:
    """random(0, min(RECONNECT_MAX_S, 2 ** attempts)), "full jitter": spread out, so
    1000 bots cut off by a server restart do not all come back in the same second."""
    return rng.uniform(0, min(config.RECONNECT_MAX_S, 2**failed_attempts))


def _next_slot(slot: float, interval: float, now: float) -> float:
    """The next slot of a fixed schedule; slots missed by a late wake-up are skipped,
    not caught up in a burst."""
    slot += interval
    while slot <= now:
        slot += interval
    return slot
