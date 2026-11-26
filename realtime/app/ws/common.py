"""What every WebSocket endpoint does the same way: read a frame within the size
limit, authenticate the first message, close with a code.

Free functions, not a base session class: each endpoint keeps its own life cycle
(the driver one has Lua scripts, a location filter and takeovers with 4409) and
calls these for the shared steps. Config is read at call time, so tests can shorten
the timeouts with monkeypatch.
"""

import asyncio
import logging
from collections.abc import Callable

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app import config
from app.auth import AuthError, Principal, Role, get_authenticator
from app.metrics import WS_CLOSES
from app.schemas.ws import AuthMessage, CloseCode, InvalidMessage
from app.ws.limits import TokenBucket

logger = logging.getLogger(__name__)

# Close codes worth a WARNING: a client or the server is misbehaving.
_WARN_ON_CLOSE = {
    CloseCode.INVALID_TOKEN,
    CloseCode.FORBIDDEN,
    CloseCode.POLICY_VIOLATION,
    CloseCode.INTERNAL_ERROR,
    CloseCode.TRY_AGAIN_LATER,
}

# For the 4403 reason; a new role without a phrase fails loudly instead of reading wrong.
_ROLE_PHRASE = {Role.DRIVER: "a driver", Role.PASSENGER: "a passenger", Role.ADMIN: "an admin"}


class WsClose(Exception):
    """Close the connection with this code."""

    def __init__(self, code: CloseCode, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


async def receive_raw(websocket: WebSocket) -> str | bytes:
    """The next text or binary frame.

    Raises WebSocketDisconnect when the client has left, and WsClose(1009) for a frame
    over WS_MAX_MESSAGE_BYTES.
    """
    message = await websocket.receive()
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000), message.get("reason"))
    raw = message.get("text")
    if raw is None:
        raw = message.get("bytes") or b""
    # uvicorn's --ws-max-size refuses such frames first; this check keeps the
    # limit when the app runs without it (tests, another server).
    size = len(raw.encode()) if isinstance(raw, str) else len(raw)
    if size > config.WS_MAX_MESSAGE_BYTES:
        raise WsClose(CloseCode.MESSAGE_TOO_BIG, f"message of {size} bytes")
    return raw


async def authenticate(
    websocket: WebSocket,
    rate: TokenBucket,
    *,
    role: Role,
    parse: Callable[[str | bytes], object],
) -> Principal:
    """Wait for the auth message and return who sent it, if they have `role`.

    `parse` is the endpoint's own message parser: a first message that is valid
    there but not auth gets its own reason. Raises WsClose with 4408 (nothing in
    WS_AUTH_TIMEOUT_S), 4401 (not auth, invalid, bad token) or 4403 (another role).
    """
    try:
        async with asyncio.timeout(config.WS_AUTH_TIMEOUT_S):
            raw = await receive_raw(websocket)
    except TimeoutError:
        raise WsClose(CloseCode.AUTH_TIMEOUT, "no auth message in time") from None
    # The first message is the first token of the bucket; it is always there.
    rate.take()

    try:
        message = parse(raw)
    except InvalidMessage as exc:
        raise WsClose(CloseCode.INVALID_TOKEN, f"invalid auth message: {exc.message}") from None
    if not isinstance(message, AuthMessage):
        raise WsClose(CloseCode.INVALID_TOKEN, "the first message must be auth")
    try:
        principal = get_authenticator().authenticate(message.data.token)
    except AuthError as exc:
        raise WsClose(CloseCode.INVALID_TOKEN, str(exc)) from None
    if principal.role is not role:
        raise WsClose(
            CloseCode.FORBIDDEN, f"role {principal.role} may not connect as {_ROLE_PHRASE[role]}"
        )
    return principal


async def close_socket(
    websocket: WebSocket,
    code: CloseCode,
    reason: str,
    *,
    role: str,
    endpoint: str,
    who: str,
) -> None:
    """Count and log the close, then send the close frame if anyone can still get it.

    `role` labels the metric ("unknown" before auth); `endpoint` and `who` only go
    into the log line, e.g. "driver" and "driver 42".
    """
    WS_CLOSES.labels(role, int(code)).inc()
    log = logger.warning if code in _WARN_ON_CLOSE else logger.info
    log("Closing %s WebSocket with %d (%s): %s", endpoint, code, who, reason)
    if (
        websocket.application_state is WebSocketState.CONNECTED
        and websocket.client_state is WebSocketState.CONNECTED
    ):
        try:
            await websocket.close(code, reason)
        except WebSocketDisconnect:
            pass  # the client left at the same moment
