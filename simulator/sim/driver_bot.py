"""A driver bot: behaves like the phone of a real driver.

Life of a bot (ТЗ 08, section 5):
1. connect, send auth at once, wait for state_snapshot; go online if offline;
2. every LOCATION_INTERVAL_S send where the car is, every PING_INTERVAL_S a ping;
3. on a close: stop for good (4401, 4403, 4409, 1008, 1009) or reconnect after a
   random delay that doubles with every failed attempt ("full jitter");
4. on stop: go offline, wait for the snapshot, close with 1000.

The car itself (Car + the drive task) lives for the whole bot, not per connection:
it keeps driving while the bot reconnects, so the bot never jumps.
"""

import asyncio
import contextlib
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
    went_online: bool = False


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
    ):
        self.driver_id = driver_id
        self.stats = BotStats()
        # For the runner's status line; connected means the socket is open.
        self.connected = False
        self.status: str | None = None
        self.stop_reason: str | None = None

        self._connect = connect
        self._osrm = osrm
        self._clock = clock
        # Seeded per (seed, driver_id) by the runner: the same bot makes the same
        # choices however many bots run.
        self._rng = rng
        # Ticks of bots are spread over the interval, not all at the same moment.
        self._phase = rng.random() * config.LOCATION_INTERVAL_S

        self._car: Car | None = None
        self._drive_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._snapshot_received = asyncio.Event()
        self._online = asyncio.Event()
        self._offline = asyncio.Event()
        # When the bot last asked to go online and has had no "available" since.
        self._go_online_sent_at: float | None = None
        self._last_snapshot: protocol.StateSnapshot | None = None
        # The bot goes online whenever the server says it is offline, unless it is
        # shutting down.
        self._wants_online = True
        self._last_ts = 0
        self._ping_scheduled_at: float | None = None
        self._failed_attempts = 0

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
                else:
                    return
                if self._stopping.is_set():
                    return
                self.stats.reconnects += 1
                await self._backoff()
        except BotStopped as exc:
            self.stop_reason = exc.reason
        finally:
            self.connected = False
            if self._drive_task is not None:
                self._drive_task.cancel()
                # gather, not suppress(CancelledError): a cancel of run() itself must
                # not be swallowed here.
                await asyncio.gather(self._drive_task, return_exceptions=True)

    # --- One connection -------------------------------------------------------

    async def _session(self) -> None:
        """Returns once the bot has stopped; raises when the connection ends."""
        conn = await self._connect()
        self.connected = True
        self.status = None
        self._ping_scheduled_at = None
        self._go_online_sent_at = None
        receiver = asyncio.create_task(self._receive(conn))
        stopping = asyncio.create_task(self._stopping.wait())
        ended = False
        try:
            if await self._handshake(conn, receiver, stopping):
                await self._serve(conn, receiver, stopping)
            await self._shutdown(conn, receiver)
            ended = True
        except BotStopped:
            # Stopped by what the server said, while the connection is still open.
            with contextlib.suppress(Closed):
                await conn.close(1000)
            ended = True
            raise
        except Closed:
            ended = True  # closed by the server
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
        stopping: asyncio.Task | None = None,
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
        self, conn: Connection, receiver: asyncio.Task[None], stopping: asyncio.Task
    ) -> bool:
        """Auth, then online. False if asked to stop meanwhile."""
        self._snapshot_received.clear()
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
        snapshot = self._last_snapshot
        assert snapshot is not None  # set together with _snapshot_received
        if self._car is None:
            await self._place_car(snapshot.position)

        # Going online is the receiver's job (see _on_snapshot); here we only wait.
        result = await self._wait(
            receiver, seconds=config.CONNECT_TIMEOUT_S, event=self._online, stopping=stopping
        )
        if result == "timeout":
            raise HandshakeTimeout("not online after go_online")
        if result == "stop":
            return False
        self.stats.went_online = True
        return True

    async def _serve(
        self, conn: Connection, receiver: asyncio.Task[None], stopping: asyncio.Task
    ) -> None:
        """Ticks and pings on a fixed schedule until asked to stop."""
        now = self._clock.monotonic()
        next_tick = now + self._phase
        next_ping = now + config.PING_INTERVAL_S
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

    async def _send_location(self, conn: Connection, now: float) -> None:
        if self.status != AVAILABLE or self._car is None:
            return
        point, heading, speed = self._car.where(now)
        # Strictly increasing, even if the wall clock stands still or steps back.
        self._last_ts = max(self._clock.now_ms(), self._last_ts + 1)
        await conn.send(protocol.location(point, heading=heading, speed=speed, ts=self._last_ts))
        self.stats.locations_sent += 1

    async def _shutdown(self, conn: Connection, receiver: asyncio.Task[None]) -> None:
        """Go offline (so the server does not wait for the reaper) and close with 1000.

        Also when a go_online is still unanswered: the server may be putting the
        driver online right now, and go_offline comes after it.
        """
        self._wants_online = False
        try:
            if receiver.done():
                receiver.result()  # raises how the connection ended
            if self.status == AVAILABLE or self._go_online_sent_at is not None:
                self._offline.clear()  # an older "offline" must not end the wait
                await conn.send(protocol.go_offline())
                await self._wait(receiver, seconds=config.SHUTDOWN_TIMEOUT_S, event=self._offline)
            await conn.close(1000)
        except Closed as exc:
            # The server closed first: nothing left to close, only to count.
            code = protocol.ABNORMAL_CLOSURE if exc.code is None else exc.code
            self.stats.closes[code] += 1
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
                        "driver %d: unknown message type %r", self.driver_id, message_type
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
        else:
            self._online.clear()
        if snapshot.status == OFFLINE:
            self._offline.set()
        else:
            self._offline.clear()
        self._snapshot_received.set()
        # After auth, and after the reaper took the bot offline (not_online + snapshot).
        if snapshot.status == OFFLINE and self._wants_online:
            if self._go_online_sent_at is None:
                self._go_online_sent_at = self._clock.monotonic()
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
        code = protocol.ABNORMAL_CLOSURE if code is None else code
        self.stats.closes[code] += 1
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

    async def _backoff(self) -> None:
        delay = reconnect_delay(self._failed_attempts, self._rng)
        self._failed_attempts += 1
        stopping = asyncio.create_task(self._stopping.wait())
        sleep = asyncio.create_task(self._clock.sleep(delay))
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
            with contextlib.suppress(OsrmUnavailable, NoRoute):
                start = await self._osrm.nearest(start)
        self._car = Car(start)
        self._drive_task = asyncio.create_task(self._drive())

    async def _drive(self) -> None:
        car = self._car
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
        while True:
            try:
                return await self._pick_route()
            except (OsrmUnavailable, NoRoute) as exc:
                logger.warning(
                    "driver %d: no route (%s), retry in ~%.0fs", self.driver_id, exc, delay
                )
            # "Equal jitter": between half and the whole delay.
            await self._clock.sleep(delay / 2 + self._rng.uniform(0, delay / 2))
            delay = min(delay * 2, config.OSRM_RETRY_MAX_S)

    async def _pick_route(self) -> Route:
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
