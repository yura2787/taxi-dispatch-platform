"""WebSocket endpoint of the dispatcher map: /rt/ws/dispatcher.

Not the dispatcher process (app.dispatcher): this is the map page of an admin.

Life of a connection:
1. accept, then wait WS_AUTH_TIMEOUT_S for {"type": "auth"} (4408 / 4401 / 4403 for a
   role other than admin);
2. subscribe to the snapshot broadcaster (1013 above DISPATCHER_MAX_CONNECTIONS); the
   first dispatcher_snapshot comes at once, from the broadcaster;
3. send every snapshot the slot hands out and answer ping, until the client leaves,
   goes idle (4000), floods (1008), sends a too big message (1009) or does not take
   what is sent within DISPATCHER_SEND_TIMEOUT_S (1013);
4. on the way out, unsubscribe.

A slow page never slows the others: the broadcaster only fills its slot (the latest
snapshot wins), and only this session's task sends on its socket.

Several tabs of one admin are fine: there is no takeover (4409) here. The page changes
nothing in Redis; it only watches.
"""

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app import config
from app.auth import Role
from app.clock import Clock
from app.metrics import WS_CONNECTIONS
from app.schemas.dispatcher import parse_dispatcher_message
from app.schemas.ws import (
    AuthMessage,
    CloseCode,
    ErrorCode,
    ErrorMessage,
    InvalidMessage,
    PingMessage,
    PongMessage,
)
from app.ws.broadcaster import SnapshotBroadcaster, SnapshotSlot, TooManySubscribers
from app.ws.common import WsClose, authenticate, close_socket, receive_raw
from app.ws.limits import RateVerdict, TokenBucket

logger = logging.getLogger(__name__)

router = APIRouter()

_ERROR_TEXT = {
    ErrorCode.ALREADY_AUTHENTICATED: "this connection is already authenticated",
    ErrorCode.RATE_LIMITED: "too many messages; slow down",
}


@router.websocket("/rt/ws/dispatcher")
async def dispatcher_websocket(websocket: WebSocket) -> None:
    state = websocket.app.state
    session = DispatcherSession(websocket, state.snapshots, state.clock)
    await session.run()


class DispatcherSession:
    """One dispatcher map page, from accept to cleanup."""

    def __init__(self, websocket: WebSocket, broadcaster: SnapshotBroadcaster, clock: Clock):
        self._websocket = websocket
        self._broadcaster = broadcaster
        self._rate = TokenBucket(clock)
        self._role = "unknown"
        # Set by a successful handshake.
        self._user_id = 0
        self._counted = False
        self._slot: SnapshotSlot | None = None

    async def run(self) -> None:
        await self._websocket.accept()
        try:
            await self._handshake()
            await self._serve()
        except WsClose as close:
            await self._close(close.code, close.reason)
        except WebSocketDisconnect:
            pass  # the page was closed (also mid-send); nothing to tell it
        except Exception:
            # A bug, not a client: counted and logged like any close, never silent.
            logger.exception("Dispatcher map session of admin %s failed", self._user_id or "?")
            await self._close(CloseCode.INTERNAL_ERROR, "internal error")
        finally:
            self._cleanup()

    # ------------------------------------------------------------------
    # Handshake
    # ------------------------------------------------------------------

    async def _handshake(self) -> None:
        principal = await authenticate(
            self._websocket, self._rate, role=Role.ADMIN, parse=parse_dispatcher_message
        )
        self._user_id = principal.user_id
        self._role = Role.ADMIN
        WS_CONNECTIONS.labels(self._role).inc()
        # Own flag: there is no driver Connection here to tell a finished handshake.
        self._counted = True
        # Logged here, so every "closed" below has its "opened", a refusal included.
        logger.info("Admin %d opened the dispatcher map", self._user_id)
        try:
            self._slot = self._broadcaster.subscribe()
        except TooManySubscribers:
            raise WsClose(CloseCode.TRY_AGAIN_LATER, "too many dispatcher connections") from None

    # ------------------------------------------------------------------
    # Serving
    # ------------------------------------------------------------------

    async def _serve(self) -> None:
        """Send snapshots and answer messages until the connection ends.

        Two things are awaited at once: the client's next message and the next
        snapshot. The receive task lives across wake-ups: a snapshot wakes the session
        but never cancels it, or a message on its way in could be lost. The idle
        deadline is absolute and moves only with the client's messages: snapshots wake
        the session every interval, and a timeout restarted on each wake-up would never
        fire.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + config.WS_IDLE_TIMEOUT_S
        receive: asyncio.Task | None = None
        snapshot: asyncio.Task | None = None
        try:
            while True:
                if receive is None:
                    receive = asyncio.create_task(receive_raw(self._websocket))
                if snapshot is None:
                    snapshot = asyncio.create_task(self._slot.wait())
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise WsClose(CloseCode.IDLE, "no messages for too long")
                done, _ = await asyncio.wait(
                    {receive, snapshot}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
                )
                if receive in done:
                    # Raises WebSocketDisconnect (the page closed) or WsClose (1009).
                    raw = receive.result()
                    receive = None
                    deadline = loop.time() + config.WS_IDLE_TIMEOUT_S
                    await self._on_message(raw)
                if snapshot in done:
                    text = snapshot.result()
                    snapshot = None
                    await self._send_text(text)
        finally:
            for task in (receive, snapshot):
                if task is not None:
                    task.cancel()
            await asyncio.gather(
                *(task for task in (receive, snapshot) if task is not None),
                return_exceptions=True,
            )

    async def _on_message(self, raw: str | bytes) -> None:
        verdict = self._rate.take()
        if verdict is RateVerdict.WARN:
            await self._error(ErrorCode.RATE_LIMITED)
        elif verdict is RateVerdict.CLOSE:
            raise WsClose(CloseCode.POLICY_VIOLATION, "too many messages")
        elif verdict is RateVerdict.ALLOW:
            await self._handle(raw)

    async def _handle(self, raw: str | bytes) -> None:
        try:
            message = parse_dispatcher_message(raw)
        except InvalidMessage as exc:
            await self._send_text(ErrorMessage.build(exc.code, exc.message).model_dump_json())
            return
        match message:
            case PingMessage():
                await self._send_text(PongMessage().model_dump_json())
            case AuthMessage():
                await self._error(ErrorCode.ALREADY_AUTHENTICATED)

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    async def _send_text(self, text: str) -> None:
        """Send, or close with 1013 if the page does not take it in time.

        Every send of the session goes through here: a page that takes no snapshot
        takes no pong either.
        """
        try:
            async with asyncio.timeout(config.DISPATCHER_SEND_TIMEOUT_S) as limit:
                await self._websocket.send_text(text)
        except TimeoutError:
            if not limit.expired():
                raise  # a TimeoutError from below, not this limit: a bug to see
            raise WsClose(
                CloseCode.TRY_AGAIN_LATER, "the page does not keep up with the snapshots"
            ) from None

    async def _error(self, code: ErrorCode) -> None:
        await self._send_text(ErrorMessage.build(code, _ERROR_TEXT[code]).model_dump_json())

    async def _close(self, code: CloseCode, reason: str) -> None:
        # A socket that took no snapshot may not take the close frame either: the
        # session ends after DISPATCHER_CLOSE_TIMEOUT_S anyway (the close is already
        # counted and logged by then), and the server drops the connection.
        try:
            async with asyncio.timeout(config.DISPATCHER_CLOSE_TIMEOUT_S) as limit:
                await close_socket(
                    self._websocket,
                    code,
                    reason,
                    role=self._role,
                    endpoint="dispatcher",
                    who=f"admin {self._user_id or '?'}",
                )
        except TimeoutError:
            if not limit.expired():
                raise
            logger.warning("Admin %s did not take the close frame in time", self._user_id or "?")

    def _cleanup(self) -> None:
        if self._slot is not None:
            self._broadcaster.unsubscribe(self._slot)
        if self._counted:
            WS_CONNECTIONS.labels(self._role).dec()
            logger.info("Admin %d closed the dispatcher map", self._user_id)
