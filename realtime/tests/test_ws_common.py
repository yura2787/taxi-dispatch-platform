"""Shared WebSocket steps (app/ws/common.py) in the branches the driver endpoint does
not reach: another required role, another endpoint's parser, the log of a close.

The driver endpoint tests (test_ws_driver.py) cover the rest end to end. Here the
socket is a fake that hands out prepared frames, so no app and no Redis are needed.
"""

import json
import logging
from typing import Annotated

import pytest
from pydantic import BaseModel, Field, TypeAdapter
from starlette.websockets import WebSocketState

from app import config
from app.auth import Principal, Role
from app.schemas.ws import AuthMessage, CloseCode, PingMessage, parse_with
from app.ws.common import WsClose, authenticate, close_socket
from app.ws.limits import TokenBucket


@pytest.fixture(autouse=True)
def dev_auth(monkeypatch):
    monkeypatch.setattr(config, "DEV_AUTH", True)


class FakeWebSocket:
    """Hands out the given text frames in order; records the close frame."""

    def __init__(self, *frames: str):
        self._frames = list(frames)
        self.application_state = WebSocketState.CONNECTED
        self.client_state = WebSocketState.CONNECTED
        self.closed_with: tuple[int, str] | None = None

    async def receive(self) -> dict:
        return {"type": "websocket.receive", "text": self._frames.pop(0)}

    async def close(self, code: int, reason: str) -> None:
        self.closed_with = (code, reason)


def frame(message_type: str, **data) -> str:
    return json.dumps({"type": message_type, "data": data})


# An endpoint that knows only auth and ping, like the dispatcher one will.
_AUTH_OR_PING = TypeAdapter(Annotated[AuthMessage | PingMessage, Field(discriminator="type")])


def parse_auth_or_ping(raw: str | bytes) -> BaseModel:
    return parse_with(_AUTH_OR_PING, raw)


@pytest.fixture
def authenticate_admin(clock):
    """Run the handshake of an admin-only endpoint over the given frames."""

    async def run(*frames: str) -> Principal:
        return await authenticate(
            FakeWebSocket(*frames), TokenBucket(clock), role=Role.ADMIN, parse=parse_auth_or_ping
        )

    return run


class TestAuthenticate:
    async def test_admin_token_is_accepted_where_admin_is_required(self, authenticate_admin):
        principal = await authenticate_admin(frame("auth", token="dev:admin:7"))

        assert principal == Principal(user_id=7, role=Role.ADMIN)

    @pytest.mark.parametrize("token", ["dev:driver:7", "dev:passenger:7"])
    async def test_other_role_is_forbidden_where_admin_is_required(self, authenticate_admin, token):
        with pytest.raises(WsClose) as exc_info:
            await authenticate_admin(frame("auth", token=token))

        assert exc_info.value.code is CloseCode.FORBIDDEN
        assert exc_info.value.reason.endswith("may not connect as an admin")

    async def test_valid_message_that_is_not_auth_closes_with_4401(self, authenticate_admin):
        with pytest.raises(WsClose) as exc_info:
            await authenticate_admin(frame("ping"))

        assert exc_info.value.code is CloseCode.INVALID_TOKEN
        assert exc_info.value.reason == "the first message must be auth"

    async def test_message_unknown_to_the_endpoint_parser_closes_with_4401(
        self, authenticate_admin
    ):
        # Valid on the driver endpoint, unknown on this one.
        with pytest.raises(WsClose) as exc_info:
            await authenticate_admin(frame("go_online"))

        assert exc_info.value.code is CloseCode.INVALID_TOKEN
        assert exc_info.value.reason == "invalid auth message: unknown message type"


class TestCloseSocket:
    async def test_log_names_the_endpoint_and_the_user(self, caplog):
        caplog.set_level(logging.INFO)
        websocket = FakeWebSocket()

        await close_socket(
            websocket,
            CloseCode.IDLE,
            "no messages for too long",
            role="admin",
            endpoint="dispatcher",
            who="admin 7",
        )

        assert "Closing dispatcher WebSocket with 4000 (admin 7): no messages" in caplog.text
        assert websocket.closed_with == (4000, "no messages for too long")

    @pytest.mark.parametrize(
        ("code", "level"),
        [(CloseCode.TRY_AGAIN_LATER, logging.WARNING), (CloseCode.IDLE, logging.INFO)],
    )
    async def test_close_is_logged_at_the_level_of_its_code(self, caplog, code, level):
        caplog.set_level(logging.INFO)

        await close_socket(FakeWebSocket(), code, "reason", role="admin", endpoint="x", who="y")

        assert [record.levelno for record in caplog.records] == [level]
