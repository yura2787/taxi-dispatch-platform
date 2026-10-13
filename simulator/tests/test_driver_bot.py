"""DriverBot against an in-memory server and OSRM, on virtual time: the handshake,
driving, pings, reactions to server errors and closes, reconnects and shutdown."""

import asyncio
import itertools
import logging
import random

import pytest

from sim import config, driver_bot
from sim.connection import Closed, ConnectFailed
from sim.driver_bot import _next_slot, reconnect_delay
from sim.geo import Point, haversine_m
from sim.osrm import NoRoute, OsrmUnavailable
from tests.conftest import FakeOsrm, FakeServer
from tests.factories import CENTRE, make_bot

FAR_AWAY = Point(50.4501, 30.5234)  # Kyiv: outside the service area


async def run(bot, clock, seconds: float) -> None:
    """Run the bot for `seconds` of virtual time, then stop it and wait until it left."""
    task = asyncio.create_task(bot.run())
    await clock.sleep(seconds)
    bot.stop()
    await task


def as_point(location: dict) -> Point:
    return Point(location["lat"], location["lon"])


def locations_of(connection) -> list[dict]:
    return [message["data"] for message in connection.sent if message["type"] == "location"]


# ---------------------------------------------------------------------------
# Handshake
# ---------------------------------------------------------------------------


class TestHandshake:
    async def test_first_message_is_auth_with_the_dev_token(self, server, osrm, clock):
        bot = make_bot(server=server, osrm=osrm, clock=clock, driver_id=42)

        await run(bot, clock, 1)

        first = server.connections[0].sent[0]
        assert first == {"type": "auth", "data": {"token": "dev:driver:42"}}

    async def test_offline_driver_goes_online_after_the_snapshot(self, server, osrm, clock):
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 1)

        assert server.connections[0].sent_types()[:2] == ["auth", "go_online"]
        assert bot.stats.went_online is True

    async def test_available_driver_does_not_go_online_again(self, server, osrm, clock):
        server.status = "available"
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 5)

        assert "go_online" not in server.connections[0].sent_types()
        assert bot.stats.went_online is True

    async def test_no_snapshot_after_auth_drops_and_reconnects(self, server, osrm, clock):
        server.silent.add("auth")
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, config.CONNECT_TIMEOUT_S + 2)

        # Dropped, not closed: a server that does not answer is not waited for.
        assert server.connections[0].aborted is True
        assert len(server.connections) >= 2
        assert bot.stats.handshake_timeouts >= 1

    async def test_no_snapshot_after_go_online_drops_and_reconnects(self, server, osrm, clock):
        server.silent.add("go_online")
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, config.CONNECT_TIMEOUT_S + 2)

        assert server.connections[0].aborted is True
        assert bot.stats.handshake_timeouts >= 1
        assert bot.stats.went_online is False

    @pytest.mark.parametrize("status", ["offered", "on_trip"])
    async def test_driver_with_an_order_stops_the_bot(self, server, osrm, clock, status):
        server.status = status
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 5)

        assert bot.stop_reason == f"status {status}"
        assert server.connections[0].closed_with == 1000
        assert len(server.connections) == 1


# ---------------------------------------------------------------------------
# Driving
# ---------------------------------------------------------------------------


class TestDriving:
    async def test_a_point_every_two_seconds(self, server, osrm, clock):
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 60)

        assert 29 <= len(server.sent("location")) <= 31

    async def test_ts_strictly_increases_and_is_an_int(self, server, osrm, clock):
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 60)

        stamps = [location["ts"] for location in server.sent("location")]
        assert all(isinstance(ts, int) for ts in stamps)
        assert all(a < b for a, b in itertools.pairwise(stamps))

    async def test_ts_increases_even_if_the_wall_clock_steps_back(
        self, server, osrm, clock, monkeypatch
    ):
        steps_back = itertools.count(1_767_225_600_000, -1_000)
        monkeypatch.setattr(clock, "now_ms", lambda: next(steps_back))
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 10)

        stamps = [location["ts"] for location in server.sent("location")]
        assert all(a < b for a, b in itertools.pairwise(stamps))

    async def test_ts_increases_even_if_the_wall_clock_stands_still(
        self, server, osrm, clock, monkeypatch
    ):
        monkeypatch.setattr(clock, "now_ms", lambda: 1_767_225_600_000)
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 10)

        stamps = [location["ts"] for location in server.sent("location")]
        assert all(a < b for a, b in itertools.pairwise(stamps))

    async def test_neighbouring_points_are_at_most_25_m_apart(self, server, osrm, clock):
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 600)

        points = [as_point(location) for location in server.sent("location")]
        # 45 km/h for 2 s, plus the rounding of coordinates to 6 decimals.
        assert max(haversine_m(a, b) for a, b in itertools.pairwise(points)) <= 25.1

    async def test_car_stands_at_the_end_of_a_route_then_drives_on_from_there(
        self, server, osrm, clock
    ):
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 900)

        locations = server.sent("location")
        speeds = [location["speed"] for location in locations]
        end = next(i for i in range(1, len(speeds)) if speeds[i] == 0 and speeds[i - 1] > 0)
        assert haversine_m(as_point(locations[end]), osrm.routes[0].geometry[-1]) < 0.5
        assert osrm.routes[1].geometry[0] == osrm.routes[0].geometry[-1]
        restart = next(i for i in range(end, len(speeds)) if speeds[i] > 0)
        assert haversine_m(as_point(locations[restart - 1]), as_point(locations[restart])) <= 25.1

    async def test_heading_is_kept_while_standing(self, server, osrm, clock):
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 900)

        stops = [
            (before, after)
            for before, after in itertools.pairwise(server.sent("location"))
            if before["speed"] > 0 and after["speed"] == 0
        ]
        assert stops
        assert all(after["heading"] == before["heading"] for before, after in stops)

    async def test_speed_is_within_the_configured_range(self, server, osrm, clock):
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 600)

        moving = [location["speed"] for location in server.sent("location") if location["speed"]]
        low, high = config.SPEED_KMH
        assert moving
        assert all(low / 3.6 - 0.01 <= speed <= high / 3.6 + 0.01 for speed in moving)

    async def test_destinations_are_in_the_city_and_far_enough(self, server, osrm, clock):
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 1800)

        assert len(osrm.routes) >= 3
        for route in osrm.routes:
            destination = route.geometry[-1]
            assert config.CITY_AREA.contains(destination)
            assert haversine_m(route.geometry[0], destination) >= config.ROUTE_MIN_DISTANCE_M

    async def test_too_close_destination_is_skipped(self, server, osrm, clock, monkeypatch):
        server.position = CENTRE
        snaps = iter([CENTRE])  # the first destination snaps to where the car stands

        async def nearest(point):
            return next(snaps, point)

        monkeypatch.setattr(osrm, "nearest", nearest)
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 5)

        assert haversine_m(CENTRE, osrm.routes[0].geometry[-1]) >= config.ROUTE_MIN_DISTANCE_M

    async def test_route_leaving_the_service_area_is_used_after_all_attempts(
        self, server, osrm, clock
    ):
        osrm.leave_area = True
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 10)

        routes_asked = [name for name, _ in osrm.calls].count("route")
        assert routes_asked == config.ROUTE_ATTEMPTS
        assert any(location["speed"] > 0 for location in server.sent("location"))

    async def test_bot_not_let_back_online_reconnects(self, server, osrm, clock):
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(10)
        server.silent.add("go_online")  # the server will not let it back
        server.status = "offline"
        sent_before = len(locations_of(server.connections[0]))

        await clock.sleep(config.CONNECT_TIMEOUT_S + 4)
        bot.stop()
        await task

        # One point finds out (not_online); then no points, and a fresh connection.
        assert len(locations_of(server.connections[0])) == sent_before + 1
        assert server.connections[0].aborted is True
        assert len(server.connections) >= 2

    async def test_offline_bot_sends_no_points(self, server, osrm, clock):
        server.silent.add("go_online")
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, config.CONNECT_TIMEOUT_S - 1)

        assert server.sent("location") == []


class TestStartPosition:
    async def test_starts_where_the_server_last_saw_the_driver(self, server, osrm, clock):
        server.position = CENTRE
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 1)

        assert osrm.routes[0].geometry[0] == CENTRE

    @pytest.mark.parametrize("position", [None, FAR_AWAY], ids=["no_position", "outside"])
    async def test_otherwise_starts_somewhere_in_the_city(self, server, osrm, clock, position):
        server.position = position
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 1)

        assert config.CITY_AREA.contains(osrm.routes[0].geometry[0])

    async def test_starts_off_the_road_when_osrm_is_down(self, server, osrm, clock):
        osrm.failure = OsrmUnavailable("down")
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 5)

        assert config.CITY_AREA.contains(bot.position)

    async def test_no_position_before_the_first_snapshot(self, server, osrm, clock):
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        assert bot.position is None

    async def test_after_a_reconnect_continues_from_its_own_position(self, server, osrm, clock):
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(30)
        last_before = as_point(locations_of(server.connections[0])[-1])
        # The server "remembers" a place 3 km away: the bot must not jump there.
        server.position = Point(CENTRE.lat + 0.03, CENTRE.lon)

        server.connections[0].drop(1011)
        await clock.sleep(10)
        bot.stop()
        await task

        first_after = as_point(locations_of(server.connections[1])[0])
        # At most ~1 s of backoff and ~2 s to the first tick, at 45 km/h.
        assert haversine_m(last_before, first_after) < 50


# ---------------------------------------------------------------------------
# OSRM down
# ---------------------------------------------------------------------------


class TestOsrmUnavailable:
    async def test_car_stands_and_keeps_sending_points(self, server, osrm, clock):
        osrm.failure = OsrmUnavailable("down")
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 30)

        locations = server.sent("location")
        assert len(locations) >= 14
        assert {location["speed"] for location in locations} == {0.0}
        assert len({(location["lat"], location["lon"]) for location in locations}) == 1

    @pytest.mark.parametrize(
        "failure", [OsrmUnavailable("down"), NoRoute("NoRoute")], ids=["unavailable", "no_route"]
    )
    async def test_retries_with_a_doubling_delay_up_to_the_maximum(
        self, server, osrm, clock, failure
    ):
        osrm.failure = failure
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 300)

        # One nearest() places the car. Then every attempt asks one destination while
        # OSRM is down, or ROUTE_ATTEMPTS of them while each has no route.
        per_attempt = config.ROUTE_ATTEMPTS if isinstance(failure, NoRoute) else 1
        attempts = [t for _, t in osrm.calls[1:]][::per_attempt]
        gaps = [b - a for a, b in itertools.pairwise(attempts)]
        base = 1.0
        for gap in gaps:
            assert base / 2 <= gap <= base  # "equal jitter"
            base = min(base * 2, config.OSRM_RETRY_MAX_S)
        assert gaps[-1] >= config.OSRM_RETRY_MAX_S / 2

    async def test_drives_once_osrm_is_back(self, server, osrm, clock):
        osrm.failure = OsrmUnavailable("down")
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(10)

        osrm.failure = None
        await clock.sleep(40)
        bot.stop()
        await task

        assert any(location["speed"] > 0 for location in server.sent("location"))


# ---------------------------------------------------------------------------
# Pings
# ---------------------------------------------------------------------------


class TestPing:
    async def test_ping_every_20_seconds(self, server, osrm, clock):
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 101)

        assert len(server.sent("ping")) == 5

    async def test_unanswered_ping_is_counted_and_replaced_by_the_next(self, server, osrm, clock):
        server.silent.add("ping")
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 101)

        assert len(server.sent("ping")) == 5
        assert bot.stats.pongs_missed == 4
        assert bot.stats.rtt_s == []

    async def test_rtt_is_measured_from_the_scheduled_moment(self, server, osrm, clock):
        server.silent.add("ping")
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(config.PING_INTERVAL_S + 3)

        server.connections[0].push("pong")
        await clock.sleep(1)
        bot.stop()
        await task

        assert bot.stats.rtt_s == [pytest.approx(3, abs=0.01)]

    async def test_unexpected_pong_is_ignored(self, server, osrm, clock):
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(5)

        server.connections[0].push("pong")
        await clock.sleep(1)
        bot.stop()
        await task

        assert bot.stats.rtt_s == []


# ---------------------------------------------------------------------------
# Server errors and unknown messages
# ---------------------------------------------------------------------------


class TestServerErrors:
    async def test_not_online_makes_the_bot_go_online_and_drive_on(self, server, osrm, clock):
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(30)
        sent_before = len(server.sent("location"))

        server.status = "offline"  # taken offline by the reaper
        await clock.sleep(30)
        bot.stop()
        await task

        assert bot.stats.errors["not_online"] == 1
        assert len(server.sent("go_online")) == 2
        # 15 ticks in 30 s; the one answered with not_online is the gap.
        assert len(server.sent("location")) - sent_before >= 13

    @pytest.mark.parametrize(
        "code",
        ["location_jump", "location_stale", "rate_limited", "invalid_message", "something_new"],
    )
    async def test_other_errors_are_counted_and_the_bot_goes_on(self, server, osrm, clock, code):
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(5)

        server.connections[0].push("error", code=code, message="...")
        await clock.sleep(5)
        bot.stop()
        await task

        assert bot.stats.errors[code] == 1
        assert bot.stop_reason is None

    @pytest.mark.parametrize("code", ["not_eligible", "busy"])
    async def test_fatal_error_stops_the_bot(self, server, osrm, clock, code):
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(5)

        server.connections[0].push("error", code=code, message="...")
        await task

        assert bot.stop_reason == f"error {code}"
        assert server.connections[0].closed_with == 1000
        assert len(server.connections) == 1

    @pytest.mark.parametrize(
        ("code", "level"),
        [
            ("not_online", logging.INFO),
            ("location_jump", logging.WARNING),
            ("rate_limited", logging.ERROR),
            ("not_eligible", logging.ERROR),
        ],
    )
    async def test_error_is_logged_at_the_level_of_the_spec(
        self, server, osrm, clock, caplog, code, level
    ):
        caplog.set_level(logging.INFO)
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(5)

        server.connections[0].push("error", code=code, message="...")
        await clock.sleep(1)
        bot.stop()
        await task

        record = next(r for r in caplog.records if code in r.getMessage())
        assert record.levelno == level

    async def test_unknown_message_type_is_counted_and_ignored(self, server, osrm, clock):
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(5)

        server.connections[0].push("offer", order_id=1)
        await clock.sleep(5)
        bot.stop()
        await task

        assert bot.stats.unknown_messages == 1
        assert bot.stop_reason is None


# ---------------------------------------------------------------------------
# Closes and reconnects
# ---------------------------------------------------------------------------


class TestCloses:
    @pytest.mark.parametrize("code", [4401, 4403, 4409, 1008, 1009])
    async def test_fatal_close_stops_the_bot_for_good(self, server, osrm, clock, code):
        server.close_after_auth = code
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 60)

        assert bot.stop_reason == f"closed {code}"
        assert server.connect_attempts == 1
        assert bot.stats.closes[code] == 1

    @pytest.mark.parametrize(("close", "counted_as"), [(4000, 4000), (1011, 1011), ("drop", 1006)])
    async def test_transient_close_during_the_handshake_reconnects(
        self, server, osrm, clock, close, counted_as
    ):
        server.close_after_auth = close
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 5)

        assert len(server.connections) >= 2
        assert bot.stats.closes[counted_as] >= 1
        assert bot.stop_reason is None

    async def test_reconnect_after_the_reaper_goes_online_again(self, server, osrm, clock):
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(10)
        server.connections[0].drop(None)
        server.status = "offline"  # reaped while the bot was away

        await clock.sleep(10)
        bot.stop()
        await task

        assert server.connections[1].sent_types()[:2] == ["auth", "go_online"]
        assert locations_of(server.connections[1])

    @pytest.mark.parametrize(
        ("code", "level"),
        [
            (4401, logging.ERROR),
            (4403, logging.ERROR),
            (4409, logging.WARNING),
            (1008, logging.ERROR),
            (1009, logging.ERROR),
            (4000, logging.WARNING),
        ],
    )
    async def test_close_is_logged_at_the_level_of_the_spec(
        self, server, osrm, clock, caplog, code, level
    ):
        server.close_after_auth = code
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 0.5)

        record = next(r for r in caplog.records if f"closed with {code}" in r.getMessage())
        assert record.levelno == level

    async def test_missing_profile_tells_how_to_fix_it(self, server, osrm, clock, caplog):
        server.close_after_auth = 4403
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 1)

        assert "make seed-drivers" in caplog.text

    @pytest.mark.parametrize("code", [4000, 4408, 1001, 1011, None], ids=str)
    async def test_transient_close_reconnects(self, server, osrm, clock, code):
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(10)

        server.connections[0].drop(code)
        await clock.sleep(10)
        bot.stop()
        await task

        assert len(server.connections) == 2
        assert bot.stats.closes[code or 1006] == 1
        assert bot.stats.reconnects == 1
        assert bot.stop_reason is None

    async def test_failed_connection_attempts_are_counted_and_retried(self, server, osrm, clock):
        server.refuse_connections = 3
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 60)

        assert bot.stats.connect_failures == 3
        assert len(server.connections) == 1
        assert bot.stats.went_online is True

    async def test_backoff_grows_and_starts_over_after_a_snapshot(
        self, server, osrm, clock, monkeypatch
    ):
        attempts: list[int] = []

        def spy(failed_attempts, rng):
            attempts.append(failed_attempts)
            return reconnect_delay(failed_attempts, rng)

        monkeypatch.setattr(driver_bot, "reconnect_delay", spy)
        server.refuse_connections = 3
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(30)

        server.connections[0].drop(1011)
        await clock.sleep(10)
        bot.stop()
        await task

        assert attempts == [0, 1, 2, 0]

    async def test_stop_during_a_reconnect_delay_returns_at_once(self, server, osrm, clock):
        server.refuse_connections = 100
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(60)
        stopped_at = clock.elapsed

        bot.stop()
        await task

        assert clock.elapsed - stopped_at < 1

    async def test_stop_while_a_connection_attempt_fails_does_not_retry(
        self, server, osrm, clock, monkeypatch
    ):
        async def slow_refusal():
            server.connect_attempts += 1
            await clock.sleep(3)
            raise ConnectFailed("timed out")

        monkeypatch.setattr(server, "connect", slow_refusal)
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 1)

        assert server.connect_attempts == 1
        assert bot.stats.reconnects == 0


class TestReconnectDelay:
    @pytest.mark.parametrize("failed_attempts", range(8))
    def test_delay_is_random_up_to_a_doubling_limit(self, failed_attempts):
        rng = random.Random(failed_attempts)
        limit = min(config.RECONNECT_MAX_S, 2**failed_attempts)

        delays = [reconnect_delay(failed_attempts, rng) for _ in range(200)]

        assert all(0 <= delay <= limit for delay in delays)
        # Full jitter: the delays spread over the whole range, not around the limit.
        assert min(delays) < limit * 0.1
        assert max(delays) > limit * 0.9


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    async def test_online_bot_goes_offline_then_closes_with_1000(self, server, osrm, clock):
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 10)

        connection = server.connections[0]
        assert connection.sent_types()[-1] == "go_offline"
        assert connection.closed_with == 1000
        assert server.status == "offline"
        assert bot.connected is False

    async def test_without_an_answer_closes_after_the_timeout(self, server, osrm, clock):
        server.silent.add("go_offline")
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(10)
        stopped_at = clock.elapsed

        bot.stop()
        await task

        assert server.connections[0].closed_with == 1000
        assert clock.elapsed - stopped_at == pytest.approx(config.SHUTDOWN_TIMEOUT_S, abs=0.1)

    async def test_stop_during_the_handshake_closes_without_going_offline(
        self, server, osrm, clock
    ):
        server.silent.add("auth")
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 1)

        connection = server.connections[0]
        assert connection.sent_types() == ["auth"]
        assert connection.closed_with == 1000

    async def test_stop_while_waiting_to_go_online(self, server, osrm, clock):
        server.silent.add("go_online")
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 1)

        assert server.connections[0].closed_with == 1000

    async def test_server_closing_during_the_shutdown_ends_it_quietly(self, server, osrm, clock):
        server.silent.add("go_offline")
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(10)
        bot.stop()
        await clock.sleep(1)  # the bot has sent go_offline and waits for the snapshot

        server.connections[0].drop(1001)
        await task

        assert bot.stop_reason is None
        assert len(server.connections) == 1
        assert bot.stats.closes[1001] == 1

    async def test_connection_already_closed_at_the_shutdown_is_only_counted(
        self, server, osrm, clock
    ):
        # The receiver saw the close just after the stop was noticed.
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        connection = await server.connect()

        async def closed_receiver():
            raise Closed(1001)

        receiver = asyncio.create_task(closed_receiver())
        await asyncio.gather(receiver, return_exceptions=True)

        await bot._shutdown(connection, receiver)

        assert connection.sent == []
        assert connection.closed_with is None
        assert bot.stats.closes[1001] == 1

    async def test_stop_while_going_back_online_still_goes_offline(self, server, osrm, clock):
        # The reaper took the bot offline and its go_online is not answered yet: the
        # server may put it online any moment, so the bot must still go offline.
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(10)
        server.silent.add("go_online")
        server.status = "offline"
        await clock.sleep(2)  # one point gets not_online, the bot sends go_online

        bot.stop()
        await task

        connection = server.connections[0]
        assert connection.sent_types()[-2:] == ["go_online", "go_offline"]
        assert connection.closed_with == 1000

    async def test_cancelled_bot_leaves_no_connection_or_task_behind(self, server, osrm, clock):
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(10)
        tasks_before = asyncio.all_tasks() - {task}

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert server.connections[0].aborted is True
        assert bot.connected is False
        # Only this test's own task is left of everything the bot started.
        leftovers = {t for t in tasks_before if not t.done()} - {asyncio.current_task()}
        assert all("FakeClock._run" in repr(t) for t in leftovers)


# ---------------------------------------------------------------------------
# Robustness and determinism
# ---------------------------------------------------------------------------


class TestDriveFailure:
    async def test_bug_in_driving_stops_the_bot_and_is_logged(
        self, server, osrm, clock, caplog, monkeypatch
    ):
        async def broken_route(origin, destination):
            raise RuntimeError("bug")

        monkeypatch.setattr(osrm, "route", broken_route)
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())

        await clock.sleep(5)
        await task

        assert bot.stop_reason == "driving failed"
        assert "driving failed" in caplog.text


class TestDeterminism:
    async def test_same_seed_and_id_make_the_same_choices(self, clock):
        choices = []
        for _ in range(2):
            osrm = FakeOsrm(clock)
            bot = make_bot(server=FakeServer(), osrm=osrm, clock=clock, seed=5, driver_id=3)
            await run(bot, clock, 1800)
            choices.append([route.geometry[-1] for route in osrm.routes])

        assert len(choices[0]) >= 3
        assert choices[0] == choices[1]

    async def test_other_bots_choose_differently(self, clock):
        destinations = []
        for driver_id in (1, 2):
            osrm = FakeOsrm(clock)
            bot = make_bot(server=FakeServer(), osrm=osrm, clock=clock, driver_id=driver_id)
            await run(bot, clock, 60)
            destinations.append(osrm.routes[0].geometry[-1])

        assert destinations[0] != destinations[1]


class TestSchedule:
    def test_next_slot_follows_the_schedule(self):
        assert _next_slot(10.0, 2.0, now=10.3) == 12.0

    def test_slots_missed_by_a_late_wake_up_are_skipped(self):
        # Woke up 5.5 s late: no burst of three ticks to catch up.
        assert _next_slot(10.0, 2.0, now=15.5) == 16.0
