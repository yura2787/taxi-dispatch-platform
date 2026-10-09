"""WebSocket endpoint of drivers: /rt/ws/driver.

Life of a connection:
1. accept, then wait WS_AUTH_TIMEOUT_S for {"type": "auth"} (4408 / 4401 / 4403);
2. bind the driver to this connection in Redis (driver_connect), then register it,
   which tells an older connection of the same driver to close with 4409;
3. send state_snapshot, then serve messages until the client leaves, goes idle
   (4000), floods (1008), sends a too big message (1009) or is superseded (4409);
4. on the way out, take the driver out of the GEO sets, if this connection still
   owns them (driver_disconnect).

Every message first passes the size check and the rate limit, then is parsed.
"""

import asyncio
import logging
import uuid
from typing import NoReturn

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from redis.exceptions import RedisError
from starlette.websockets import WebSocketState

from app import config
from app.auth import AuthError, Role, get_authenticator
from app.clock import Clock
from app.metrics import LOCATION_UPDATES, WS_CLOSES, WS_CONNECTIONS, LocationResult
from app.schemas.ws import (
    AuthMessage,
    CloseCode,
    ErrorCode,
    ErrorMessage,
    GoOfflineMessage,
    GoOnlineMessage,
    InvalidMessage,
    LocationData,
    LocationMessage,
    PingMessage,
    PongMessage,
    StateSnapshotMessage,
    parse_message,
)
from app.services.drivers import DriverProfile, DriverStore, LocationUpdate, Outcome
from app.services.locations import Location, LocationFilter, Verdict
from app.ws.limits import RateVerdict, TokenBucket
from app.ws.manager import Connection, ConnectionRegistry

logger = logging.getLogger(__name__)

router = APIRouter()

# Close codes worth a WARNING: a client or the server is misbehaving.
_WARN_ON_CLOSE = {
    CloseCode.INVALID_TOKEN,
    CloseCode.FORBIDDEN,
    CloseCode.POLICY_VIOLATION,
    CloseCode.INTERNAL_ERROR,
}

_REJECTIONS = {
    Verdict.OUT_OF_ORDER: (LocationResult.REJECTED_OUT_OF_ORDER, ErrorCode.LOCATION_OUT_OF_ORDER),
    Verdict.STALE: (LocationResult.REJECTED_STALE, ErrorCode.LOCATION_STALE),
    Verdict.JUMP: (LocationResult.REJECTED_JUMP, ErrorCode.LOCATION_JUMP),
}

_ERROR_TEXT = {
    ErrorCode.ALREADY_AUTHENTICATED: "this connection is already authenticated",
    ErrorCode.NOT_ONLINE: "the driver is offline; send go_online first",
    ErrorCode.NOT_ELIGIBLE: "the driver may not go online",
    ErrorCode.BUSY: "not possible during an offer or a trip",
    ErrorCode.LOCATION_STALE: "the point is too old",
    ErrorCode.LOCATION_OUT_OF_ORDER: "the point is older than the previous one",
    ErrorCode.LOCATION_JUMP: "the point is too far from the previous one",
    ErrorCode.RATE_LIMITED: "too many messages; slow down",
}


@router.websocket("/rt/ws/driver")
async def driver_websocket(websocket: WebSocket) -> None:
    state = websocket.app.state
    session = DriverSession(websocket, state.drivers, state.driver_connections, state.clock)
    await session.run()


class _Close(Exception):
    """Close the connection with this code."""

    def __init__(self, code: CloseCode, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


class DriverSession:
    """One driver WebSocket connection, from accept to cleanup."""

    def __init__(
        self,
        websocket: WebSocket,
        store: DriverStore,
        registry: ConnectionRegistry,
        clock: Clock,
    ):
        self._websocket = websocket
        self._store = store
        self._registry = registry
        self._clock = clock
        self._rate = TokenBucket(clock)
        self._role = "unknown"
        # Set by a successful handshake.
        self._driver_id = 0
        self._profile: DriverProfile | None = None
        self._connection: Connection | None = None
        self._locations: LocationFilter | None = None

    async def run(self) -> None:
        await self._websocket.accept()
        try:
            await self._handshake()
            await self._serve()
        except _Close as close:
            await self._close(close.code, close.reason)
        except WebSocketDisconnect:
            pass  # the client left; nothing to tell it
        except RedisError as exc:
            # Without Redis the driver's state cannot be kept; the client reconnects.
            await self._close(CloseCode.INTERNAL_ERROR, f"Redis error: {exc}")
        finally:
            await self._cleanup()

    # ------------------------------------------------------------------
    # Handshake
    # ------------------------------------------------------------------

    async def _handshake(self) -> None:
        try:
            async with asyncio.timeout(config.WS_AUTH_TIMEOUT_S):
                raw = await self._receive()
        except TimeoutError:
            raise _Close(CloseCode.AUTH_TIMEOUT, "no auth message in time") from None
        # The first message is the first token of the bucket; it is always there.
        self._rate.take()

        try:
            message = parse_message(raw)
        except InvalidMessage as exc:
            raise _Close(CloseCode.INVALID_TOKEN, f"invalid auth message: {exc.message}") from None
        if not isinstance(message, AuthMessage):
            raise _Close(CloseCode.INVALID_TOKEN, "the first message must be auth")
        try:
            principal = get_authenticator().authenticate(message.data.token)
        except AuthError as exc:
            raise _Close(CloseCode.INVALID_TOKEN, str(exc)) from None
        if principal.role is not Role.DRIVER:
            raise _Close(CloseCode.FORBIDDEN, f"role {principal.role} may not connect as a driver")

        driver_id = principal.user_id
        profile = await self._store.get_profile(driver_id)
        if profile is None or not profile.may_go_online:
            raise _Close(CloseCode.FORBIDDEN, f"driver {driver_id} may not work")

        # Redis first, then the registry: the old connection's cleanup must already
        # see a foreign conn_id, or it would take the driver out of the GEO sets.
        connection = Connection(str(uuid.uuid4()))
        state = await self._store.connect(driver_id, connection.conn_id)
        self._registry.register(driver_id, connection)

        self._driver_id, self._profile, self._connection = driver_id, profile, connection
        self._role = Role.DRIVER
        WS_CONNECTIONS.labels(self._role).inc()
        # The last point in Redis: older buffered points and jumps are caught from the
        # very first point of this connection.
        last = state.position.as_accepted_point() if state.position else None
        self._locations = LocationFilter(self._clock, last=last)
        logger.info("Driver %d connected (%s)", driver_id, connection.conn_id)
        await self._send(StateSnapshotMessage.build(state, profile_tariff=profile.tariff))

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    async def _serve(self) -> None:
        superseded = asyncio.create_task(self._connection.wait_superseded())
        try:
            while True:
                raw = await self._receive_unless_superseded(superseded)
                verdict = self._rate.take()
                if verdict is RateVerdict.ALLOW:
                    await self._handle(raw)
                elif verdict is RateVerdict.WARN:
                    await self._error(ErrorCode.RATE_LIMITED)
                elif verdict is RateVerdict.CLOSE:
                    raise _Close(CloseCode.POLICY_VIOLATION, "too many messages")
        finally:
            superseded.cancel()

    async def _receive_unless_superseded(self, superseded: asyncio.Task) -> str | bytes:
        # A message being handled is never interrupted: a takeover is noticed while
        # waiting for the next one. If it lands mid-message, the Lua scripts answer
        # stale_connection anyway.
        receive = asyncio.create_task(self._receive())
        done, _ = await asyncio.wait(
            {receive, superseded},
            timeout=config.WS_IDLE_TIMEOUT_S,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if receive in done:
            return receive.result()
        receive.cancel()
        if superseded in done:
            raise _Close(CloseCode.SUPERSEDED, "a newer connection of this driver opened")
        raise _Close(CloseCode.IDLE, "no messages for too long")

    async def _handle(self, raw: str | bytes) -> None:
        try:
            message = parse_message(raw)
        except InvalidMessage as exc:
            if exc.message_type == "location":
                LOCATION_UPDATES.labels(LocationResult.REJECTED_INVALID).inc()
            await self._send(ErrorMessage.build(exc.code, exc.message))
            return

        match message:
            case LocationMessage():
                await self._location(message.data)
            case PingMessage():
                await self._send(PongMessage())
            case GoOnlineMessage():
                await self._go_online()
            case GoOfflineMessage():
                await self._go_offline()
            case AuthMessage():
                await self._error(ErrorCode.ALREADY_AUTHENTICATED)

    async def _go_online(self) -> None:
        outcome = await self._store.go_online(
            self._driver_id, self._connection.conn_id, self._clock.now_ms()
        )
        if outcome is Outcome.OK:
            logger.info("Driver %d is online", self._driver_id)
            await self._send_snapshot()
        elif outcome is Outcome.NOT_ELIGIBLE:
            await self._error(ErrorCode.NOT_ELIGIBLE)
        elif outcome is Outcome.BUSY:
            await self._error(ErrorCode.BUSY)
        else:
            self._raise_stale(outcome)

    async def _go_offline(self) -> None:
        outcome = await self._store.go_offline(self._driver_id, self._connection.conn_id)
        if outcome is Outcome.OK:
            logger.info("Driver %d is offline", self._driver_id)
            await self._send_snapshot()
        elif outcome is Outcome.NOT_ONLINE:
            await self._not_online()
        elif outcome is Outcome.BUSY:
            await self._error(ErrorCode.BUSY)
        else:
            self._raise_stale(outcome)

    async def _location(self, data: LocationData) -> None:
        decision = self._locations.check(Location(lat=data.lat, lon=data.lon, ts=data.ts))
        if decision.verdict is Verdict.THROTTLED:
            # Normal for a phone sending a bit too often: counted, not answered.
            LOCATION_UPDATES.labels(LocationResult.THROTTLED).inc()
            return
        if decision.verdict in _REJECTIONS:
            result, code = _REJECTIONS[decision.verdict]
            LOCATION_UPDATES.labels(result).inc()
            await self._error(code)
            return

        update = LocationUpdate(
            point=decision.point, heading=data.heading, speed=data.speed, in_zone=decision.in_zone
        )
        outcome = await self._store.update_location(
            self._driver_id, self._connection.conn_id, update
        )
        if outcome is Outcome.OK:
            self._locations.commit(decision)
            in_zone = decision.in_zone
            LOCATION_UPDATES.labels(
                LocationResult.ACCEPTED if in_zone else LocationResult.OUT_OF_ZONE
            ).inc()
        elif outcome is Outcome.NOT_ONLINE:
            LOCATION_UPDATES.labels(LocationResult.REJECTED_OFFLINE).inc()
            await self._not_online()
        else:
            LOCATION_UPDATES.labels(LocationResult.REJECTED_STALE_CONNECTION).inc()
            self._raise_stale(outcome)

    async def _not_online(self) -> None:
        # The reaper may have taken the driver offline while this connection stayed
        # open, and it cannot tell the client (stage 5). The snapshot does.
        await self._error(ErrorCode.NOT_ONLINE)
        await self._send_snapshot()

    def _raise_stale(self, outcome: Outcome) -> NoReturn:
        # The only outcome left once a caller has handled the others.
        assert outcome is Outcome.STALE_CONNECTION, f"unexpected script outcome {outcome}"
        # Another connection (e.g. on another gateway instance) owns the driver now.
        raise _Close(CloseCode.SUPERSEDED, "the driver is connected elsewhere")

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    async def _receive(self) -> str | bytes:
        message = await self._websocket.receive()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1000), message.get("reason"))
        raw = message.get("text")
        if raw is None:
            raw = message.get("bytes") or b""
        # uvicorn's --ws-max-size refuses such frames first; this check keeps the
        # limit when the app runs without it (tests, another server).
        size = len(raw.encode()) if isinstance(raw, str) else len(raw)
        if size > config.WS_MAX_MESSAGE_BYTES:
            raise _Close(CloseCode.MESSAGE_TOO_BIG, f"message of {size} bytes")
        return raw

    async def _send(self, message: BaseModel) -> None:
        await self._websocket.send_text(message.model_dump_json())

    async def _send_snapshot(self) -> None:
        state = await self._store.get_state(self._driver_id)
        await self._send(StateSnapshotMessage.build(state, profile_tariff=self._profile.tariff))

    async def _error(self, code: ErrorCode) -> None:
        await self._send(ErrorMessage.build(code, _ERROR_TEXT[code]))

    async def _close(self, code: CloseCode, reason: str) -> None:
        WS_CLOSES.labels(self._role, int(code)).inc()
        log = logger.warning if code in _WARN_ON_CLOSE else logger.info
        log(
            "Closing driver WebSocket with %d (driver %s): %s", code, self._driver_id or "?", reason
        )
        if (
            self._websocket.application_state is WebSocketState.CONNECTED
            and self._websocket.client_state is WebSocketState.CONNECTED
        ):
            try:
                await self._websocket.close(code, reason)
            except WebSocketDisconnect:
                pass  # the client left at the same moment

    async def _cleanup(self) -> None:
        if self._connection is None:
            return  # never got past the handshake
        self._registry.unregister(self._driver_id, self._connection)
        try:
            outcome = await self._store.disconnect(self._driver_id, self._connection.conn_id)
        except RedisError as exc:
            # The driver may stay in a GEO set without a connection: the reaper takes
            # them offline within DRIVER_SILENCE_S + REAPER_INTERVAL_S.
            logger.warning("Driver %d disconnect not recorded: %s", self._driver_id, exc)
        else:
            logger.info("Driver %d disconnected (%s)", self._driver_id, outcome)
        finally:
            WS_CONNECTIONS.labels(self._role).dec()
