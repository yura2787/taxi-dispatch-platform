"""Driver WebSocket messages: parsing of incoming ones with their error codes, and
the JSON shape of outgoing ones."""

import json

import pytest

from app.schemas.ws import (
    AuthMessage,
    ErrorCode,
    ErrorMessage,
    GoOfflineMessage,
    GoOnlineMessage,
    InvalidMessage,
    LocationMessage,
    PingMessage,
    PongMessage,
    StateSnapshotMessage,
    parse_message,
)
from app.services.drivers import DriverState, DriverStatus, Position

TS = 1_767_225_600_000


def location(**data) -> str:
    fields = {"lat": 48.2921, "lon": 25.9358, "heading": 90.0, "speed": 12.5, "ts": TS}
    return json.dumps({"type": "location", "data": fields | data})


def invalid(raw: str) -> InvalidMessage:
    with pytest.raises(InvalidMessage) as exc_info:
        parse_message(raw)
    return exc_info.value


# ---------------------------------------------------------------------------
# Incoming
# ---------------------------------------------------------------------------


class TestValidMessages:
    @pytest.mark.parametrize(
        ("raw", "model"),
        [
            ('{"type": "auth", "data": {"token": "dev:driver:1"}}', AuthMessage),
            ('{"type": "ping", "data": {}}', PingMessage),
            ('{"type": "go_online", "data": {}}', GoOnlineMessage),
            ('{"type": "go_offline", "data": {}}', GoOfflineMessage),
        ],
    )
    def test_message_is_parsed_into_its_model(self, raw, model):
        assert isinstance(parse_message(raw), model)

    def test_location_is_parsed(self):
        message = parse_message(location())

        assert isinstance(message, LocationMessage)
        assert message.data.lat == 48.2921
        assert message.data.ts == TS

    def test_bytes_are_parsed_like_text(self):
        assert isinstance(parse_message(location().encode()), LocationMessage)

    def test_integer_coordinates_are_accepted(self):
        assert parse_message(location(lat=48, lon=26)).data.lat == 48.0

    @pytest.mark.parametrize("field", ["heading", "speed"])
    def test_null_heading_or_speed_is_accepted(self, field):
        assert getattr(parse_message(location(**{field: None})).data, field) is None

    def test_missing_heading_and_speed_are_none(self):
        raw = json.dumps({"type": "location", "data": {"lat": 48.29, "lon": 25.93, "ts": TS}})

        data = parse_message(raw).data

        assert (data.heading, data.speed) == (None, None)

    @pytest.mark.parametrize(
        "data",
        [{"lat": -90, "lon": -180}, {"lat": 90, "lon": 180}, {"heading": 0}, {"speed": 0}],
        ids=["min-lat-lon", "max-lat-lon", "north", "standing"],
    )
    def test_boundary_values_are_accepted(self, data):
        assert isinstance(parse_message(location(**data)), LocationMessage)


class TestInvalidMessages:
    @pytest.mark.parametrize("raw", ["{", "not json", ""], ids=["truncated", "text", "empty"])
    def test_broken_json_is_invalid_json(self, raw):
        assert invalid(raw).code is ErrorCode.INVALID_JSON

    @pytest.mark.parametrize("raw", ["[]", '"ping"', "42", "null"])
    def test_json_that_is_not_an_object_is_invalid_message(self, raw):
        assert invalid(raw).code is ErrorCode.INVALID_MESSAGE

    def test_unknown_type_is_unknown_type(self):
        assert invalid('{"type": "offer_response", "data": {}}').code is ErrorCode.UNKNOWN_TYPE

    def test_message_without_type_is_invalid_message(self):
        assert invalid('{"data": {}}').code is ErrorCode.INVALID_MESSAGE

    def test_message_without_data_is_invalid_message(self):
        assert invalid('{"type": "ping"}').code is ErrorCode.INVALID_MESSAGE

    @pytest.mark.parametrize(
        "data",
        [
            {"lat": 90.0001},
            {"lat": -90.0001},
            {"lon": 180.0001},
            {"lon": -180.0001},
            {"heading": 360},
            {"heading": -1},
            {"speed": -1},
            {"lat": None},
            {"lat": "48.29"},
            {"ts": 1.5},
            {"ts": 1.0},
            {"ts": "1767225600000"},
            {"ts": True},
            {"ts": 0},
            {"ts": 10**14},
            {"extra": 1},
        ],
        ids=[
            "lat-above-90",
            "lat-below-90",
            "lon-above-180",
            "lon-below-180",
            "heading-360",
            "heading-negative",
            "speed-negative",
            "lat-null",
            "lat-string",
            "ts-fraction",
            "ts-float",
            "ts-string",
            "ts-bool",
            "ts-zero",
            "ts-too-large",
            "unknown-field",
        ],
    )
    def test_invalid_location_is_invalid_message_of_type_location(self, data):
        error = invalid(location(**data))

        assert error.code is ErrorCode.INVALID_MESSAGE
        assert error.message_type == "location"

    @pytest.mark.parametrize("value", ["Infinity", "-Infinity", "NaN", "1e999"])
    def test_non_finite_numbers_are_rejected(self, value):
        raw = location().replace('"speed": 12.5', f'"speed": {value}')

        assert invalid(raw).code is ErrorCode.INVALID_MESSAGE

    def test_error_message_names_the_field_without_echoing_the_input(self):
        error = invalid(location(lat=123.456))

        assert error.message.startswith("data.lat: ")
        assert "123.456" not in error.message

    @pytest.mark.parametrize(
        "raw",
        [
            '{"type": "auth", "data": {"token": ""}}',
            '{"type": "auth", "data": {"token": 42}}',
            '{"type": "ping", "data": {"x": 1}}',
            '{"type": "ping", "data": {}, "extra": 1}',
        ],
        ids=["empty-token", "numeric-token", "data-field", "envelope-field"],
    )
    def test_other_invalid_messages_are_invalid_message(self, raw):
        assert invalid(raw).code is ErrorCode.INVALID_MESSAGE


# ---------------------------------------------------------------------------
# Outgoing
# ---------------------------------------------------------------------------


class TestOutgoing:
    def test_snapshot_of_a_driver_with_position(self):
        state = DriverState(
            status=DriverStatus.AVAILABLE,
            tariff="comfort",
            in_zone=True,
            position=Position(lat=48.29, lon=25.93, heading=None, speed=3.0, ts=TS, updated_at=TS),
            conn_id="conn-1",
        )

        message = StateSnapshotMessage.build(state, profile_tariff="economy")

        assert json.loads(message.model_dump_json()) == {
            "type": "state_snapshot",
            "data": {
                "status": "available",
                "tariff": "comfort",
                "in_zone": True,
                "position": {"lat": 48.29, "lon": 25.93, "heading": None, "speed": 3.0},
            },
        }

    def test_snapshot_of_a_new_driver_takes_tariff_from_profile(self):
        state = DriverState(
            status=DriverStatus.OFFLINE, tariff=None, in_zone=False, position=None, conn_id="c"
        )

        data = json.loads(
            StateSnapshotMessage.build(state, profile_tariff="economy").model_dump_json()
        )["data"]

        assert data == {
            "status": "offline",
            "tariff": "economy",
            "in_zone": False,
            "position": None,
        }

    def test_pong(self):
        assert json.loads(PongMessage().model_dump_json()) == {"type": "pong", "data": {}}

    def test_error(self):
        message = ErrorMessage.build(ErrorCode.RATE_LIMITED, "slow down")

        assert json.loads(message.model_dump_json()) == {
            "type": "error",
            "data": {"code": "rate_limited", "message": "slow down"},
        }
