"""The dispatcher endpoint's messages: the dispatcher_snapshot built from the live
drivers (its JSON contract, rounding, order, an empty city) and the parsing of what
the admin's page may send. Unit tests: no Redis, no app."""

import json

import pytest
from pydantic import ValidationError

from app.schemas.dispatcher import parse_dispatcher_message
from app.schemas.ws import AuthMessage, ErrorCode, InvalidMessage, PingMessage
from app.services.drivers import DriverStatus, LiveDriver, LiveDrivers
from app.services.snapshot import build_dispatcher_snapshot

SERVER_MS = 1_767_225_600_000


def live_driver(driver_id: int = 1, **fields: object) -> LiveDriver:
    values = {
        "id": driver_id,
        "lat": 48.2921,
        "lon": 25.9358,
        "heading": 90.0,
        "status": DriverStatus.AVAILABLE,
        "tariff": "economy",
        "connected": True,
    }
    return LiveDriver(**(values | fields))


def snapshot_json(*drivers: LiveDriver, online: int = 0, available: int = 0) -> dict:
    live = LiveDrivers(drivers=drivers, online=online, available=available)
    return json.loads(build_dispatcher_snapshot(live, SERVER_MS).model_dump_json())


# ---------------------------------------------------------------------------
# The snapshot
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_json_has_the_agreed_shape(self):
        driver = live_driver(1, heading=None, tariff="comfort", connected=False)

        snapshot = snapshot_json(driver, online=2, available=1)

        assert snapshot == {
            "type": "dispatcher_snapshot",
            "data": {
                "server_ms": SERVER_MS,
                "drivers": [
                    {
                        "id": 1,
                        "lat": 48.2921,
                        "lon": 25.9358,
                        "heading": None,
                        "status": "available",
                        "tariff": "comfort",
                        "connected": False,
                    }
                ],
                "trips": [],
                "surge": {},
                "stats": {"online": 2, "available": 1, "searching": 0, "in_progress": 0},
            },
        }

    def test_coordinates_are_rounded_to_six_places(self):
        driver = live_driver(lat=48.292_123_456_7, lon=25.935_876_543_2)

        (shown,) = snapshot_json(driver)["data"]["drivers"]

        assert (shown["lat"], shown["lon"]) == (48.292123, 25.935877)

    def test_drivers_keep_the_order_of_the_read(self):
        # live_drivers() returns them by id; the builder must not reorder.
        drivers = [live_driver(driver_id) for driver_id in (1, 2, 3)]

        shown = snapshot_json(*drivers)["data"]["drivers"]

        assert [driver["id"] for driver in shown] == [1, 2, 3]

    @pytest.mark.parametrize("status", list(DriverStatus))
    def test_status_is_sent_as_its_name(self, status):
        (shown,) = snapshot_json(live_driver(status=status))["data"]["drivers"]

        assert shown["status"] == status.value

    def test_counters_come_from_the_read_not_from_the_drivers_shown(self):
        # Drivers online without a point yet are counted but not on the map.
        stats = snapshot_json(live_driver(1), online=5, available=3)["data"]["stats"]

        assert (stats["online"], stats["available"]) == (5, 3)

    @pytest.mark.parametrize("value", [float("nan"), float("inf")])
    def test_non_finite_coordinate_fails_the_build_instead_of_sending_null(self, value):
        live = LiveDrivers(drivers=(live_driver(lat=value),), online=1, available=1)

        with pytest.raises(ValidationError):
            build_dispatcher_snapshot(live, SERVER_MS)

    def test_empty_city_has_no_drivers_and_zero_counters(self):
        data = snapshot_json()["data"]

        assert data["drivers"] == []
        assert data["stats"] == {"online": 0, "available": 0, "searching": 0, "in_progress": 0}


# ---------------------------------------------------------------------------
# Dispatcher -> server
# ---------------------------------------------------------------------------


def envelope(message_type: str, **data) -> str:
    return json.dumps({"type": message_type, "data": data})


class TestIncomingMessages:
    def test_auth_is_parsed(self):
        parsed = parse_dispatcher_message(envelope("auth", token="dev:admin:1"))

        assert isinstance(parsed, AuthMessage)
        assert parsed.data.token == "dev:admin:1"

    def test_ping_is_parsed(self):
        assert isinstance(parse_dispatcher_message(envelope("ping")), PingMessage)

    def test_bytes_are_parsed_like_text(self):
        assert isinstance(parse_dispatcher_message(envelope("ping").encode()), PingMessage)

    @pytest.mark.parametrize("message_type", ["location", "go_online", "go_offline", "nope"])
    def test_driver_and_unknown_messages_are_unknown_types(self, message_type):
        # Valid on the driver endpoint, but a dispatcher never sends them.
        with pytest.raises(InvalidMessage) as exc_info:
            parse_dispatcher_message(envelope(message_type))

        assert exc_info.value.code is ErrorCode.UNKNOWN_TYPE

    @pytest.mark.parametrize(
        ("raw", "code"),
        [
            ("not json", ErrorCode.INVALID_JSON),
            ('{"data": {}}', ErrorCode.INVALID_MESSAGE),
            ('{"type": "ping", "data": {"extra": 1}}', ErrorCode.INVALID_MESSAGE),
            ('{"type": "auth", "data": {"token": ""}}', ErrorCode.INVALID_MESSAGE),
            ('{"type": "auth"}', ErrorCode.INVALID_MESSAGE),
            ('{"type": "auth", "data": "dev:admin:1"}', ErrorCode.INVALID_MESSAGE),
        ],
        ids=["not json", "no type", "extra field", "empty token", "no data", "data not object"],
    )
    def test_broken_message_gets_the_shared_error_code(self, raw, code):
        with pytest.raises(InvalidMessage) as exc_info:
            parse_dispatcher_message(raw)

        assert exc_info.value.code is code
