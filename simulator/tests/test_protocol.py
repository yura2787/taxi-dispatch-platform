"""Messages of the /rt/ws/driver protocol as the bot builds and reads them, and what
the bot does about each close code and error code (ТЗ 08, section 5.4)."""

import json

import pytest

from sim import protocol
from sim.geo import Point
from sim.protocol import (
    CloseVerdict,
    ErrorVerdict,
    Pong,
    ServerError,
    StateSnapshot,
    UnknownMessage,
)
from tests.factories import make_snapshot


class TestClientMessages:
    def test_token_is_the_dev_token_of_the_driver(self):
        assert protocol.token_for(42) == "dev:driver:42"

    def test_auth_carries_the_token(self):
        message = json.loads(protocol.auth("dev:driver:42"))

        assert message == {"type": "auth", "data": {"token": "dev:driver:42"}}

    @pytest.mark.parametrize("build", [protocol.ping, protocol.go_online, protocol.go_offline])
    def test_messages_without_data_have_an_empty_object(self, build):
        message = json.loads(build())

        assert message["data"] == {}

    def test_location_has_rounded_coordinates_and_an_int_ts(self):
        point = Point(48.29212345678, 25.93581234567)

        message = json.loads(protocol.location(point, heading=90.5, speed=12.3456, ts=1_000))

        assert message == {
            "type": "location",
            "data": {
                "lat": 48.292123,
                "lon": 25.935812,
                "heading": 90.5,
                "speed": 12.35,
                "ts": 1_000,
            },
        }
        assert isinstance(message["data"]["ts"], int)

    def test_location_heading_and_speed_may_be_null(self):
        message = json.loads(protocol.location(Point(48.3, 25.9), heading=None, speed=None, ts=1))

        assert message["data"]["heading"] is None
        assert message["data"]["speed"] is None


class TestServerMessages:
    def test_snapshot_with_a_position(self):
        raw = make_snapshot(status="available", position=Point(48.29, 25.93))

        message = protocol.parse_server_message(raw)

        assert message == StateSnapshot(status="available", position=Point(48.29, 25.93))

    def test_snapshot_without_a_position(self):
        message = protocol.parse_server_message(make_snapshot())

        assert message == StateSnapshot(status="offline", position=None)

    def test_pong(self):
        message = protocol.parse_server_message('{"type": "pong", "data": {}}')

        assert message == Pong()

    def test_error_carries_its_code(self):
        raw = '{"type": "error", "data": {"code": "location_jump", "message": "..."}}'

        message = protocol.parse_server_message(raw)

        assert message == ServerError(code="location_jump")

    def test_unknown_type_does_not_fail(self):
        message = protocol.parse_server_message('{"type": "offer", "data": {"id": 1}}')

        assert message == UnknownMessage(type="offer")

    @pytest.mark.parametrize("raw", ["not json", "[]", '{"data": {}}'])
    def test_message_without_a_type_does_not_fail(self, raw):
        message = protocol.parse_server_message(raw)

        assert message == UnknownMessage(type=None)

    @pytest.mark.parametrize(
        ("raw", "message_type"),
        [
            ('{"type": "pong"}', "pong"),
            ('{"type": "error", "data": {}}', "error"),
            ('{"type": "state_snapshot", "data": {"status": "available"}}', "state_snapshot"),
        ],
        ids=["no_data", "error_without_code", "snapshot_without_position"],
    )
    def test_known_type_with_broken_data_keeps_its_type(self, raw, message_type):
        # What drifted shows in the log, not just "unknown message type None".
        message = protocol.parse_server_message(raw)

        assert message == UnknownMessage(type=message_type)


class TestCloseVerdicts:
    @pytest.mark.parametrize(
        ("code", "verdict"),
        [
            (4401, CloseVerdict.FATAL),
            (4403, CloseVerdict.FATAL),
            (4409, CloseVerdict.FATAL),
            (1008, CloseVerdict.BUG),
            (1009, CloseVerdict.BUG),
            (4000, CloseVerdict.RECONNECT),
            (4408, CloseVerdict.RECONNECT),
            (1011, CloseVerdict.RECONNECT),
            (1001, CloseVerdict.RECONNECT),
            (1006, CloseVerdict.RECONNECT),
            (None, CloseVerdict.RECONNECT),
            (4999, CloseVerdict.RECONNECT),
        ],
        ids=str,
    )
    def test_close_code(self, code, verdict):
        assert protocol.classify_close(code) is verdict


class TestErrorVerdicts:
    @pytest.mark.parametrize(
        ("code", "verdict"),
        [
            ("not_online", ErrorVerdict.NOT_ONLINE),
            ("location_jump", ErrorVerdict.REJECTED_LOCATION),
            ("location_stale", ErrorVerdict.REJECTED_LOCATION),
            ("location_out_of_order", ErrorVerdict.REJECTED_LOCATION),
            ("not_eligible", ErrorVerdict.FATAL),
            ("busy", ErrorVerdict.FATAL),
            ("rate_limited", ErrorVerdict.BUG),
            ("invalid_json", ErrorVerdict.BUG),
            ("invalid_message", ErrorVerdict.BUG),
            ("unknown_type", ErrorVerdict.BUG),
            ("already_authenticated", ErrorVerdict.BUG),
            ("brand_new_code", ErrorVerdict.BUG),
        ],
    )
    def test_error_code(self, code, verdict):
        assert protocol.classify_error(code) is verdict
