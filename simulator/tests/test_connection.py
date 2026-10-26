"""The websockets adapter against a real local websockets server: close codes reach
the bot, abort() drops the TCP connection without a close frame, close() sends one.

Real sockets need real time, so these tests use short real timeouts, not FakeClock.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from sim.connection import Closed, ConnectFailed, open_connection

Handler = Callable[[ServerConnection], Awaitable[None]]
TIMEOUT_S = 2


class Server:
    """A local websockets server running one handler; records how each client left."""

    def __init__(self) -> None:
        self.url = ""
        self.received: list[str] = []
        # The close frame the server got: its code, or None if the TCP just dropped.
        self.client_closed: asyncio.Future[int | None] = asyncio.Future()

    async def wait_for_client(self, ws: ServerConnection) -> None:
        try:
            async for message in ws:
                self.received.append(message)
        except ConnectionClosed as exc:
            self._closed(exc)
        else:
            self._closed_cleanly(ws)

    def _closed(self, exc: ConnectionClosed) -> None:
        if not self.client_closed.done():
            self.client_closed.set_result(exc.rcvd.code if exc.rcvd else None)

    def _closed_cleanly(self, ws: ServerConnection) -> None:
        if not self.client_closed.done():
            self.client_closed.set_result(ws.close_code)


@pytest.fixture
async def start_server() -> AsyncIterator[Callable[[Handler | None], Awaitable[Server]]]:
    servers = []

    async def start(handler: Handler | None = None) -> Server:
        state = Server()
        ws_server = await serve(handler or state.wait_for_client, "127.0.0.1", 0)
        servers.append(ws_server)
        port = ws_server.sockets[0].getsockname()[1]
        state.url = f"ws://127.0.0.1:{port}"
        return state

    yield start
    for ws_server in servers:
        ws_server.close()
        await ws_server.wait_closed()


class TestAdapter:
    async def test_messages_go_both_ways(self, start_server):
        async def echo(ws: ServerConnection) -> None:
            async for message in ws:
                await ws.send(f"echo {message}")

        server = await start_server(echo)
        connection = await open_connection(server.url)

        await connection.send("ping")

        assert await asyncio.wait_for(connection.recv(), TIMEOUT_S) == "echo ping"
        await connection.close(1000)

    async def test_binary_message_is_read_as_text(self, start_server):
        async def binary(ws: ServerConnection) -> None:
            await ws.send(b'{"type": "pong"}')
            await ws.wait_closed()

        server = await start_server(binary)
        connection = await open_connection(server.url)

        assert await asyncio.wait_for(connection.recv(), TIMEOUT_S) == '{"type": "pong"}'
        await connection.close(1000)

    async def test_close_code_of_the_server_reaches_the_bot(self, start_server):
        async def refuse(ws: ServerConnection) -> None:
            await ws.recv()
            await ws.close(4403, "no profile")

        server = await start_server(refuse)
        connection = await open_connection(server.url)
        await connection.send("auth")

        with pytest.raises(Closed) as exc_info:
            await asyncio.wait_for(connection.recv(), TIMEOUT_S)

        assert exc_info.value.code == 4403

    async def test_sending_after_the_server_closed_raises_closed(self, start_server):
        async def close_at_once(ws: ServerConnection) -> None:
            await ws.close(4000)

        server = await start_server(close_at_once)
        connection = await open_connection(server.url)
        with pytest.raises(Closed):
            await asyncio.wait_for(connection.recv(), TIMEOUT_S)

        with pytest.raises(Closed) as exc_info:
            await connection.send("location")

        assert exc_info.value.code == 4000

    async def test_close_sends_the_code_to_the_server(self, start_server):
        server = await start_server()
        connection = await open_connection(server.url)

        await connection.close(1000)

        assert await asyncio.wait_for(server.client_closed, TIMEOUT_S) == 1000

    async def test_abort_drops_the_connection_without_a_close_frame(self, start_server):
        server = await start_server()
        connection = await open_connection(server.url)
        await connection.send("hello")

        connection.abort()

        assert await asyncio.wait_for(server.client_closed, TIMEOUT_S) is None
        assert server.received == ["hello"]

    async def test_protocol_pings_of_the_server_are_answered(self, start_server):
        # uvicorn pings every 20 s and drops clients that do not answer; the adapter
        # answers by itself, even while the bot is not reading.
        pong_received = asyncio.Event()

        async def ping_the_client(ws: ServerConnection) -> None:
            await asyncio.wait_for(await ws.ping(), TIMEOUT_S)
            pong_received.set()
            await ws.wait_closed()

        server = await start_server(ping_the_client)
        connection = await open_connection(server.url)

        await asyncio.wait_for(pong_received.wait(), TIMEOUT_S)

        await connection.close(1000)

    async def test_adapter_sends_no_protocol_pings_of_its_own(self, start_server):
        server = await start_server()

        connection = await open_connection(server.url)

        # The application ping is the keepalive; library pings would close the
        # connection with 1011 whenever the event loop lags under load.
        assert connection._ws.ping_interval is None
        await connection.close(1000)

    async def test_connection_dropped_by_the_server_has_no_code(self, start_server):
        async def drop(ws: ServerConnection) -> None:
            ws.transport.abort()

        server = await start_server(drop)
        connection = await open_connection(server.url)

        with pytest.raises(Closed) as exc_info:
            await asyncio.wait_for(connection.recv(), TIMEOUT_S)

        assert exc_info.value.code is None


class TestOpenConnection:
    async def test_refused_connection_is_connect_failed(self, unused_tcp_port):
        with pytest.raises(ConnectFailed):
            await open_connection(f"ws://127.0.0.1:{unused_tcp_port}")

    async def test_http_error_instead_of_a_websocket_is_connect_failed(self):
        # e.g. 502 from nginx while realtime restarts.
        async def not_websocket(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()

        http = await asyncio.start_server(not_websocket, "127.0.0.1", 0)
        port = http.sockets[0].getsockname()[1]
        try:
            with pytest.raises(ConnectFailed):
                await open_connection(f"ws://127.0.0.1:{port}")
        finally:
            http.close()
            await http.wait_closed()
