"""The WebSocket connection of one bot, behind a small interface.

The bot talks to `Connection`; tests give it an in-memory one, the simulator the
adapter over the `websockets` library below, the only code that knows the library.
"""

from typing import Protocol

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from sim import config


class Closed(Exception):
    """The connection is closed; `code` is None if it dropped without a close frame."""

    def __init__(self, code: int | None):
        super().__init__(f"connection closed with {code}")
        self.code = code


class ConnectFailed(Exception):
    """The connection could not be opened: refused, timed out or a bad handshake
    (e.g. 502 from nginx while realtime restarts)."""


class Connection(Protocol):
    async def send(self, text: str) -> None: ...

    async def recv(self) -> str: ...

    async def close(self, code: int) -> None: ...

    def abort(self) -> None:
        """Drop the TCP connection without a close frame, like a phone in a tunnel."""


class WebsocketsConnection:
    def __init__(self, ws: ClientConnection):
        self._ws = ws

    async def send(self, text: str) -> None:
        try:
            await self._ws.send(text)
        except ConnectionClosed as exc:
            raise _closed(exc) from None

    async def recv(self) -> str:
        try:
            message = await self._ws.recv()
        except ConnectionClosed as exc:
            raise _closed(exc) from None
        return message if isinstance(message, str) else message.decode()

    async def close(self, code: int) -> None:
        await self._ws.close(code)

    def abort(self) -> None:
        self._ws.transport.abort()


def _closed(exc: ConnectionClosed) -> Closed:
    # rcvd is the close frame from the server; None if the connection just dropped.
    return Closed(exc.rcvd.code if exc.rcvd is not None else None)


async def open_connection(url: str) -> Connection:
    try:
        ws = await connect(
            url,
            open_timeout=config.CONNECT_TIMEOUT_S,
            # No protocol pings of our own: the application ping is the keepalive and
            # the RTT sample (spec 12.1), and the library's would close a connection
            # with 1011 whenever the event loop lags under load, failing the very runs
            # that measure the server. The server's pings are still answered.
            ping_interval=None,
            # A server that does not answer the close frame is not waited for long.
            close_timeout=config.CLOSE_TIMEOUT_S,
            # Messages are ~150 bytes: compression would only cost CPU for 1000 bots.
            compression=None,
        )
    except (OSError, TimeoutError, InvalidHandshake) as exc:
        raise ConnectFailed(f"{type(exc).__name__}: {exc}") from exc
    return WebsocketsConnection(ws)
