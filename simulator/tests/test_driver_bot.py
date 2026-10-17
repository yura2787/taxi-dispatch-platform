"""DriverBot against an in-memory server and OSRM, on virtual time: the handshake,
driving, pings, reactions to server errors and closes, reconnects and shutdown."""

import asyncio
import itertools
import logging
import random

import pytest

from sim import config, driver_bot
from sim.connection import Closed, ConnectFailed
from sim.driver_bot import Misbehaviour, _next_slot, reconnect_delay
from sim.geo import Point, haversine_m
from sim.osrm import NoRoute, OsrmUnavailable
from tests.conftest import FakeClock, FakeConnection, FakeOsrm, FakeServer
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


def record_messages(server, clock) -> list[tuple[float, dict]]:
    """(virtual time, message) of everything the bot sends to this server, in order."""
    log: list[tuple[float, dict]] = []
    handle = server.handle

    def record(connection, message):
        log.append((clock.elapsed, message))
        handle(connection, message)

    server.handle = record
    return log


def times_of(messages: list[tuple[float, dict]], message_type: str) -> list[float]:
    return [t for t, message in messages if message["type"] == message_type]


def gaps(times: list[float]) -> list[float]:
    return [b - a for a, b in itertools.pairwise(times)]


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

    async def test_failures_are_counted_and_warned_once_per_outage(
        self, server, osrm, clock, caplog
    ):
        caplog.set_level(logging.DEBUG, logger="sim.driver_bot")
        osrm.failure = OsrmUnavailable("down")
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 60)

        # The start position and every route retry; one WARNING for the retries.
        assert bot.stats.osrm_failures >= 5
        retries = [r for r in caplog.records if "no route" in r.getMessage()]
        warnings = [r for r in retries if r.levelno == logging.WARNING]
        assert len(retries) > len(warnings) == 1
        assert any("no road for the start" in r.getMessage() for r in caplog.records)

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
    async def test_ping_every_20_seconds_from_a_phase_of_its_own(self, server, osrm, clock):
        messages = record_messages(server, clock)
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 101)

        pings = times_of(messages, "ping")
        # The first within one interval (the idle timeout is 45 s), then every 20 s.
        assert 0 < pings[0] <= config.PING_INTERVAL_S
        assert len(pings) >= 5
        assert gaps(pings) == pytest.approx([config.PING_INTERVAL_S] * (len(pings) - 1), abs=0.01)

    async def test_bots_ping_at_different_moments(self, osrm, clock):
        first_pings = set()
        for driver_id in range(1, 6):
            server = FakeServer()
            messages = record_messages(server, clock)
            bot = make_bot(server=server, osrm=osrm, clock=clock, driver_id=driver_id)
            started = clock.elapsed
            await run(bot, clock, 25)
            first_pings.add(round(times_of(messages, "ping")[0] - started, 3))

        # Spread over the interval, not all at session start + 20 s.
        assert len(first_pings) == 5

    async def test_unanswered_ping_is_counted_and_replaced_by_the_next(self, server, osrm, clock):
        server.silent.add("ping")
        messages = record_messages(server, clock)
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 101)

        # Still one ping per interval: a missing pong neither stops nor speeds them up.
        pings = times_of(messages, "ping")
        assert gaps(pings) == pytest.approx([config.PING_INTERVAL_S] * (len(pings) - 1), abs=0.01)
        assert bot.stats.pongs_missed == len(pings) - 1
        assert bot.stats.rtt_s == []

    async def test_rtt_is_measured_from_the_scheduled_moment(self, server, osrm, clock):
        server.silent.add("ping")
        messages = record_messages(server, clock)
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(config.PING_INTERVAL_S + 1)  # the first ping is out
        sent_at = times_of(messages, "ping")[-1]

        server.connections[0].push("pong")
        answered_at = clock.elapsed
        await clock.sleep(1)
        bot.stop()
        await task

        assert bot.stats.rtt_s == [pytest.approx(answered_at - sent_at, abs=0.01)]

    async def test_pong_after_a_missed_one_is_timed_from_the_newest_ping(self, server, osrm, clock):
        # Several pings go unanswered; the pong that finally comes is timed from the
        # newest one, the only one in flight, not from the first.
        server.silent.add("ping")
        messages = record_messages(server, clock)
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(2 * config.PING_INTERVAL_S + 1)
        pings = times_of(messages, "ping")

        server.connections[0].push("pong")
        answered_at = clock.elapsed
        await clock.sleep(1)
        bot.stop()
        await task

        assert len(pings) >= 2
        assert bot.stats.pongs_missed == len(pings) - 1
        assert bot.stats.rtt_s == [pytest.approx(answered_at - pings[-1], abs=0.01)]

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

    async def test_stop_during_a_reconnect_delay_goes_offline_first(self, server, osrm, clock):
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(10)
        server.connections[0].drop(1011)  # the driver stays online on the server (W4)

        bot.stop()
        await task

        assert server.connections[1].sent_types() == ["auth", "go_offline"]
        assert server.status == "offline"

    async def test_bot_that_never_got_online_does_not_say_goodbye(self, server, osrm, clock):
        server.refuse_connections = 100
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(10)
        attempts = server.connect_attempts

        bot.stop()
        await task

        assert server.connect_attempts == attempts

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

    async def test_without_an_answer_drops_the_connection_after_the_timeout(
        self, server, osrm, clock
    ):
        server.silent.add("go_offline")
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(10)
        stopped_at = clock.elapsed

        bot.stop()
        await task

        # A server that does not answer go_offline would not answer the close frame
        # either: the bot does not wait for it.
        connection = server.connections[0]
        assert (connection.aborted, connection.closed_with) == (True, None)
        assert clock.elapsed - stopped_at == pytest.approx(config.SHUTDOWN_TIMEOUT_S, abs=0.1)
        # Not confirmed: the server may hold the driver online until the reaper.
        assert bot.stats.left_online == 1

    async def test_shutdown_and_goodbye_share_one_stop_budget(self, server, osrm, clock):
        server.silent.add("go_offline")
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(10)
        stopped_at = clock.elapsed
        bot.stop()
        await clock.sleep(4)  # most of the budget waiting for the snapshot

        server.hang_connections = True  # the goodbye connection never opens
        server.connections[0].drop(1001)
        await task

        # Only what was left of the budget for the goodbye, not a budget of its own.
        assert clock.elapsed - stopped_at == pytest.approx(config.BOT_STOP_BUDGET_S, abs=0.1)
        assert bot.stats.left_online == 1

    async def test_late_shutdown_still_leaves_time_for_the_close(self, server, osrm, clock):
        server.silent.add("go_offline")
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(10)
        stopped_at = clock.elapsed

        bot.stop()
        clock.advance(3)  # the bot notices the stop late (a busy event loop)
        await task

        # It waits for the snapshot only as long as leaves CLOSE_TIMEOUT_S of the budget.
        expected = config.BOT_STOP_BUDGET_S - config.CLOSE_TIMEOUT_S
        assert clock.elapsed - stopped_at == pytest.approx(expected, abs=0.1)
        assert server.connections[0].aborted is True

    async def test_no_goodbye_once_the_stop_budget_is_spent(
        self, server, osrm, clock, monkeypatch, caplog
    ):
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(10)
        connect = server.connect

        async def slow_connect():
            await clock.sleep(config.BOT_STOP_BUDGET_S + 1)  # a busy event loop
            return await connect()

        monkeypatch.setattr(bot, "_connect", slow_connect)
        # The slow reconnect is closed right after its auth: no offline confirmed.
        server.close_after_auth = 1011
        server.connections[0].drop(1011)  # online on the server (W4), reconnecting
        # The first reconnect delay is below 1 s: the slow connect is under way.
        await clock.sleep(1)

        bot.stop()
        await task

        assert len(server.connections) == 2  # the slow reconnect, no goodbye after it
        assert bot.stats.left_online == 1
        assert "no time left" in caplog.text

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

    async def test_server_closing_during_the_shutdown_leads_to_a_goodbye(self, server, osrm, clock):
        server.silent.add("go_offline")
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(10)
        bot.stop()
        await clock.sleep(1)  # the bot has sent go_offline and waits for the snapshot

        server.connections[0].drop(1001)
        server.silent.clear()
        await task

        # The go_offline was never confirmed: one more connection just to go offline.
        assert server.connections[1].sent_types() == ["auth", "go_offline"]
        assert server.status == "offline"
        assert bot.stats.left_online == 0
        assert bot.stats.closes[1001] == 1
        assert bot.stop_reason is None

    async def test_server_closing_during_the_shutdown_and_a_refused_goodbye_leave_it_online(
        self, server, osrm, clock
    ):
        server.silent.add("go_offline")
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(10)
        bot.stop()
        await clock.sleep(1)

        server.connections[0].drop(1001)
        server.refuse_connections = 1
        await task

        assert server.status == "available"
        assert bot.stats.left_online == 1
        assert bot.stats.closes[1001] == 1

    @pytest.mark.parametrize("code", [4401, 4403, 4409, 1008, 1009])
    async def test_closed_for_good_during_the_shutdown_stops_without_a_goodbye(
        self, server, osrm, clock, code
    ):
        # A goodbye would be refused, or, after 4409, take the driver offline from the
        # connection that has just taken it over.
        server.silent.add("go_offline")
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(10)
        bot.stop()
        await clock.sleep(1)

        server.connections[0].drop(code)
        await task

        assert len(server.connections) == 1
        assert bot.stop_reason == f"closed {code}"
        assert bot.stats.closes == {code: 1}

    async def test_stop_before_the_snapshot_of_a_reconnect_still_goes_offline(
        self, server, osrm, clock
    ):
        # Online on the server (W4) while the new connection waits for its snapshot.
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(10)
        server.silent.add("auth")
        server.connections[0].drop(1011)
        await clock.sleep(3)  # reconnected (backoff < 1 s), no snapshot yet
        server.silent.clear()

        bot.stop()
        await task

        assert server.connections[1].sent_types() == ["auth"]
        assert server.connections[2].sent_types() == ["auth", "go_offline"]
        assert server.status == "offline"
        assert bot.stats.left_online == 0

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
# Breaks
# ---------------------------------------------------------------------------


@pytest.fixture
def short_breaks(monkeypatch):
    """A break after 60 s online, for 120 s."""
    monkeypatch.setattr(config, "BREAK_EVERY_S", (60, 60))
    monkeypatch.setattr(config, "BREAK_DURATION_S", (120, 120))


@pytest.fixture
def timeline(server, clock) -> list[tuple[float, str]]:
    """(virtual time, message type) of everything the bot sends; filled as it runs."""
    log: list[tuple[float, str]] = []
    handle = server.handle

    def record(connection, message):
        log.append((clock.elapsed, message["type"]))
        handle(connection, message)

    server.handle = record
    return log


def between(log, start: float, end: float) -> set[str]:
    return {kind for t, kind in log if start < t < end}


class TestBreaks:
    async def test_break_goes_offline_pings_and_comes_back(
        self, server, osrm, clock, short_breaks, timeline
    ):
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 220)

        kinds = [kind for _, kind in timeline]
        start = kinds.index("go_offline")
        end = kinds.index("go_online", start)
        offline_at, online_at = timeline[start][0], timeline[end][0]
        assert offline_at == pytest.approx(60, abs=2.1)
        assert online_at - offline_at == pytest.approx(120, abs=2.1)
        # Only pings while on the break (in the order sent, not just by time: a point
        # right after go_offline would get not_online), and still one per interval.
        assert set(kinds[start + 1 : end]) == {"ping"}
        break_pings = [t for t, _ in timeline[start + 1 : end]]
        # 120 s of break: 6 pings, or 5 when one falls right on its edges.
        expected = round(config.BREAK_DURATION_S[0] / config.PING_INTERVAL_S)
        assert len(break_pings) in (expected - 1, expected)
        assert gaps(break_pings) == pytest.approx(
            [config.PING_INTERVAL_S] * (len(break_pings) - 1), abs=0.01
        )
        assert bot.stats.breaks == 1
        assert any(t > online_at for t, kind in timeline if kind == "location")

    async def test_car_does_not_move_during_a_break(self, server, osrm, clock, short_breaks):
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(70)
        parked = bot.position

        await clock.sleep(100)
        late_in_the_break = (bot.on_break, bot.position)
        bot.stop()
        await task

        assert late_in_the_break == (True, parked)

    async def test_driving_resumes_from_the_place_of_the_break(
        self, server, osrm, clock, short_breaks
    ):
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(70)
        parked = bot.position
        routes_before = len(osrm.routes)

        await clock.sleep(120)
        bot.stop()
        await task

        assert osrm.routes[routes_before].geometry[0] == parked

    async def test_bot_that_first_connects_on_a_break_is_not_online_yet(
        self, server, osrm, clock, short_breaks
    ):
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(70)  # on the break now
        bot.stats.went_online = False  # forget the first connection

        server.connections[0].drop(1011)
        await clock.sleep(10)
        reconnected_on_break = bot.stats.went_online
        bot.stop()
        await task

        assert reconnected_on_break is False

    async def test_reconnect_during_a_break_stays_offline(self, server, osrm, clock, short_breaks):
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(80)

        server.connections[0].drop(1011)
        await clock.sleep(30)
        second = server.connections[1].sent_types()
        bot.stop()
        await task

        assert "go_online" not in second
        assert "location" not in second
        assert bot.stats.went_online is True

    async def test_stop_during_a_break_just_closes(self, server, osrm, clock, short_breaks):
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 90)

        connection = server.connections[0]
        assert connection.sent_types().count("go_offline") == 1  # the break's own
        assert connection.closed_with == 1000

    async def test_stop_as_a_break_begins_sends_go_offline_once(
        self, server, osrm, clock, short_breaks
    ):
        server.silent.add("go_offline")  # the break's go_offline is still unanswered
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(63)
        state_at_stop = (bot.on_break, bot.status)

        bot.stop()
        await task

        assert state_at_stop == (True, "available")
        assert server.connections[0].sent_types().count("go_offline") == 1
        assert bot.stats.left_online == 1  # never confirmed

    async def test_stop_after_the_offline_of_a_break_needs_no_goodbye(
        self, server, osrm, clock, short_breaks
    ):
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(70)  # on the break, offline confirmed
        server.refuse_connections = 100
        server.connections[0].drop(1011)

        bot.stop()
        await task

        assert len(server.connections) == 1
        assert bot.stats.left_online == 0

    async def test_goodbye_on_a_break_sends_go_offline_once(
        self, server, osrm, clock, short_breaks
    ):
        server.silent.add("go_offline")  # the break's go_offline is lost
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(70)
        server.refuse_connections = 100  # stopped while reconnecting
        server.connections[0].drop(1011)
        await clock.sleep(1)
        server.refuse_connections = 0
        server.silent.clear()

        bot.stop()
        await task

        # The snapshot says "available" on a break: go_offline is sent there, once.
        assert server.connections[1].sent_types() == ["auth", "go_offline"]
        assert server.status == "offline"
        assert bot.stats.left_online == 0

    async def test_lost_go_offline_of_a_break_is_sent_again_after_a_reconnect(
        self, server, osrm, clock, short_breaks
    ):
        server.silent.add("go_offline")  # lost with the connection
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(70)
        server.silent.clear()

        server.connections[0].drop(1011)
        await clock.sleep(10)
        bot.stop()
        await task

        assert "go_offline" in server.connections[1].sent_types()
        assert server.status == "offline"

    async def test_no_break_during_a_silence(self, server, osrm, clock, short_breaks, monkeypatch):
        monkeypatch.setattr(config, "FLAKY_EVERY_S", (40, 40))
        bot = flaky_bot(server, osrm, clock, monkeypatch, Misbehaviour.SILENCE)

        # Silent from ~40 to ~90: the break due at ~60 waits for the silence to end.
        await run(bot, clock, 85)

        assert bot.stats.silences == 1
        assert bot.stats.breaks == 0

    async def test_misbehaviour_due_during_a_break_waits_after_it(
        self, server, osrm, clock, short_breaks, monkeypatch
    ):
        monkeypatch.setattr(config, "FLAKY_EVERY_S", (100, 100))
        bot = flaky_bot(server, osrm, clock, monkeypatch, Misbehaviour.DROP)

        # The break runs 60-180; the drop due at ~100 comes 100 s after it, not at 180.
        await run(bot, clock, 230)

        assert bot.stats.breaks == 1
        assert bot.stats.drops == 0

    async def test_break_waits_until_the_bot_is_online(
        self, server, osrm, clock, short_breaks, timeline
    ):
        bot = make_bot(server=server, osrm=osrm, clock=clock)
        task = asyncio.create_task(bot.run())
        await clock.sleep(50)
        server.status = "offline"  # reaped just before the break is due
        server.silent.add("go_online")

        await clock.sleep(14)
        status_when_due = bot.status
        bot.stop()
        await task

        # The next point got not_online, so at 60 s the bot knows it is offline.
        assert status_when_due == "offline"
        assert bot.stats.breaks == 0


# ---------------------------------------------------------------------------
# Flaky bots
# ---------------------------------------------------------------------------


@pytest.fixture
def flaky_every_30_s(monkeypatch):
    monkeypatch.setattr(config, "FLAKY_EVERY_S", (30, 30))
    monkeypatch.setattr(config, "DROP_DURATION_S", (10, 10))


def flaky_bot(server, osrm, clock, monkeypatch, misbehaviour: Misbehaviour):
    bot = make_bot(server=server, osrm=osrm, clock=clock, flaky=True)
    monkeypatch.setattr(bot, "_choose_misbehaviour", lambda: misbehaviour)
    return bot


class TestFlakyDrop:
    async def test_drop_aborts_without_a_close_frame(
        self, server, osrm, clock, monkeypatch, flaky_every_30_s
    ):
        bot = flaky_bot(server, osrm, clock, monkeypatch, Misbehaviour.DROP)

        await run(bot, clock, 35)

        first = server.connections[0]
        assert first.aborted is True
        assert first.closed_with is None
        assert bot.stats.drops == 1

    async def test_comes_back_after_the_drop_without_a_backoff(
        self, server, osrm, clock, monkeypatch, flaky_every_30_s, timeline
    ):
        bot = flaky_bot(server, osrm, clock, monkeypatch, Misbehaviour.DROP)

        await run(bot, clock, 45)

        auths = [t for t, kind in timeline if kind == "auth"]
        assert auths[1] - auths[0] == pytest.approx(30 + 10, abs=2.1)
        assert bot.stats.reconnects == 0

    async def test_next_misbehaviour_counts_from_the_end_of_the_last(
        self, server, osrm, clock, monkeypatch, flaky_every_30_s, timeline
    ):
        bot = flaky_bot(server, osrm, clock, monkeypatch, Misbehaviour.DROP)

        await run(bot, clock, 85)

        # Dropped at ~30 for 10 s, back at ~40, dropped again ~30 s later.
        auths = [t for t, kind in timeline if kind == "auth"]
        assert [round(t / 10) * 10 for t in auths] == [0, 40, 80]
        assert bot.stats.drops == 2

    async def test_car_drives_on_while_the_connection_is_down(
        self, server, osrm, clock, monkeypatch, flaky_every_30_s
    ):
        bot = flaky_bot(server, osrm, clock, monkeypatch, Misbehaviour.DROP)

        await run(bot, clock, 50)

        last_before = as_point(locations_of(server.connections[0])[-1])
        first_after = as_point(locations_of(server.connections[1])[0])
        gap = haversine_m(last_before, first_after)
        # ~10-14 s of driving at 25-45 km/h: moved on, but no jump.
        assert 50 < gap < 200

    async def test_drop_longer_than_the_reaper_ends_with_going_online(
        self, server, osrm, clock, monkeypatch, flaky_every_30_s
    ):
        monkeypatch.setattr(config, "DROP_DURATION_S", (45, 45))
        bot = flaky_bot(server, osrm, clock, monkeypatch, Misbehaviour.DROP)
        task = asyncio.create_task(bot.run())
        await clock.sleep(40)
        server.status = "offline"  # the reaper, 30-40 s after the last point

        await clock.sleep(40)
        bot.stop()
        await task

        assert server.connections[1].sent_types()[:2] == ["auth", "go_online"]
        assert locations_of(server.connections[1])

    async def test_stop_during_a_drop_reconnects_once_to_go_offline(
        self, server, osrm, clock, monkeypatch, flaky_every_30_s
    ):
        # Away for 10 s: the server still holds the bot online (the reaper needs 30 s).
        bot = flaky_bot(server, osrm, clock, monkeypatch, Misbehaviour.DROP)

        await run(bot, clock, 34)

        goodbye = server.connections[1]
        assert goodbye.sent_types() == ["auth", "go_offline"]
        assert goodbye.closed_with == 1000
        assert server.status == "offline"
        assert bot.stats.left_online == 0

    async def test_failed_goodbye_is_counted_and_logged(
        self, server, osrm, clock, monkeypatch, flaky_every_30_s, caplog
    ):
        bot = flaky_bot(server, osrm, clock, monkeypatch, Misbehaviour.DROP)
        task = asyncio.create_task(bot.run())
        await clock.sleep(33)  # dropped at ~30, away for 10 s
        server.refuse_connections = 1

        bot.stop()
        await task

        assert bot.stats.left_online == 1
        assert "could not go offline after the stop" in caplog.text

    async def test_goodbye_to_a_server_that_never_answers_is_bounded(
        self, server, osrm, clock, monkeypatch, flaky_every_30_s
    ):
        bot = flaky_bot(server, osrm, clock, monkeypatch, Misbehaviour.DROP)
        task = asyncio.create_task(bot.run())
        await clock.sleep(33)
        server.hang_connections = True  # the goodbye connection never opens
        stopped_at = clock.elapsed

        bot.stop()
        await task

        # The bot's own stop budget ran out, before the runner would cancel it.
        assert clock.elapsed - stopped_at == pytest.approx(config.BOT_STOP_BUDGET_S, abs=0.1)
        assert bot.stats.left_online == 1

    async def test_goodbye_refused_for_good_counts_as_left_online(
        self, server, osrm, clock, monkeypatch, flaky_every_30_s
    ):
        bot = flaky_bot(server, osrm, clock, monkeypatch, Misbehaviour.DROP)
        task = asyncio.create_task(bot.run())
        await clock.sleep(33)
        server.close_after_auth = 4409  # another connection took the driver meanwhile

        bot.stop()
        await task

        assert bot.stats.left_online == 1
        assert bot.stop_reason is None

    async def test_close_during_the_goodbye_is_counted(
        self, server, osrm, clock, monkeypatch, flaky_every_30_s
    ):
        bot = flaky_bot(server, osrm, clock, monkeypatch, Misbehaviour.DROP)
        task = asyncio.create_task(bot.run())
        await clock.sleep(33)
        server.close_after_auth = 4409

        bot.stop()
        await task

        assert bot.stats.closes == {4409: 1}
        assert bot.stats.left_online == 1

    async def test_server_closing_while_the_goodbye_goes_offline_is_left_online(
        self, server, osrm, clock, monkeypatch, flaky_every_30_s
    ):
        bot = flaky_bot(server, osrm, clock, monkeypatch, Misbehaviour.DROP)
        task = asyncio.create_task(bot.run())
        await clock.sleep(33)
        handle = server.handle

        def close_on_go_offline(connection, message):
            if message["type"] == "go_offline":
                connection.drop(1011)
            else:
                handle(connection, message)

        server.handle = close_on_go_offline

        bot.stop()
        await task

        assert bot.stats.left_online == 1
        assert bot.stats.closes[1011] == 1

    async def test_goodbye_confirmed_offline_but_slow_to_close_is_not_left_online(
        self, server, osrm, clock, monkeypatch, flaky_every_30_s
    ):
        bot = flaky_bot(server, osrm, clock, monkeypatch, Misbehaviour.DROP)
        task = asyncio.create_task(bot.run())
        await clock.sleep(33)

        async def slow_close(connection, code):
            await clock.sleep(config.BOT_STOP_BUDGET_S)  # outlasts the deadline

        monkeypatch.setattr(FakeConnection, "close", slow_close)

        bot.stop()
        await task

        assert server.status == "offline"
        assert bot.stats.left_online == 0

    async def test_bug_in_the_goodbye_is_not_swallowed(
        self, server, osrm, clock, monkeypatch, flaky_every_30_s
    ):
        bot = flaky_bot(server, osrm, clock, monkeypatch, Misbehaviour.DROP)
        task = asyncio.create_task(bot.run())
        await clock.sleep(33)
        server.handle = lambda connection, message: 1 / 0  # a bug on our side

        bot.stop()

        with pytest.raises(ZeroDivisionError):
            await task
        assert bot.stats.left_online == 0


class TestFlakySilence:
    async def test_silence_sends_no_points_but_keeps_pinging(
        self, server, osrm, clock, monkeypatch, flaky_every_30_s, timeline
    ):
        bot = flaky_bot(server, osrm, clock, monkeypatch, Misbehaviour.SILENCE)

        await run(bot, clock, 85)

        locations = [t for t, kind in timeline if kind == "location"]
        gap = max(b - a for a, b in itertools.pairwise(locations))
        assert gap == pytest.approx(config.SILENCE_S, abs=2.1)
        silent_from = next(
            a for a, b in itertools.pairwise(locations) if b - a > 2 * config.LOCATION_INTERVAL_S
        )
        assert "ping" in between(timeline, silent_from, silent_from + config.SILENCE_S)
        assert bot.stats.silences == 1

    async def test_after_the_silence_not_online_brings_it_back(
        self, server, osrm, clock, monkeypatch, flaky_every_30_s
    ):
        bot = flaky_bot(server, osrm, clock, monkeypatch, Misbehaviour.SILENCE)
        task = asyncio.create_task(bot.run())
        await clock.sleep(75)
        server.status = "offline"  # the reaper takes the silent bot offline

        await clock.sleep(15)
        bot.stop()
        await task

        assert bot.stats.errors["not_online"] == 1
        assert len(server.sent("go_online")) == 2

    async def test_car_keeps_driving_during_the_silence(
        self, server, osrm, clock, monkeypatch, flaky_every_30_s
    ):
        bot = flaky_bot(server, osrm, clock, monkeypatch, Misbehaviour.SILENCE)

        await run(bot, clock, 85)

        points = [as_point(location) for location in server.sent("location")]
        gaps = [haversine_m(a, b) for a, b in itertools.pairwise(points)]
        # 50 s of silence at 25-45 km/h: the next point is far, but no jump the
        # server would reject (its jump filter resets after 15 s of silence).
        assert 300 < max(gaps) < 650


class TestFlakyChoice:
    async def test_bot_that_is_not_flaky_never_misbehaves(
        self, server, osrm, clock, flaky_every_30_s
    ):
        bot = make_bot(server=server, osrm=osrm, clock=clock)

        await run(bot, clock, 300)

        assert (bot.stats.drops, bot.stats.silences) == (0, 0)
        assert len(server.connections) == 1

    def test_drops_and_silences_are_chosen_about_equally_with_a_seeded_rng(
        self, server, osrm, clock
    ):
        bot = make_bot(server=server, osrm=osrm, clock=clock, flaky=True)

        choices = [bot._choose_misbehaviour() for _ in range(1000)]

        assert 400 < choices.count(Misbehaviour.DROP) < 600


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
    async def drive(self, *, seed: int, driver_id: int, seconds: float) -> list:
        """Everything one bot sends over a run, on a fresh clock: the moment, the type
        and the data of each message (ts left out: it is the wall clock)."""
        clock = FakeClock()
        server = FakeServer()
        messages = record_messages(server, clock)
        bot = make_bot(
            server=server, osrm=FakeOsrm(clock), clock=clock, seed=seed, driver_id=driver_id
        )
        await run(bot, clock, seconds)
        return [
            (t, message["type"], {k: v for k, v in message["data"].items() if k != "ts"})
            for t, message in messages
        ]

    async def test_same_seed_and_id_send_exactly_the_same_messages(self, monkeypatch):
        # Breaks and pauses included: every random choice of the bot is in the stream.
        monkeypatch.setattr(config, "BREAK_EVERY_S", (100, 300))
        monkeypatch.setattr(config, "BREAK_DURATION_S", (30, 90))

        first = await self.drive(seed=5, driver_id=3, seconds=900)
        second = await self.drive(seed=5, driver_id=3, seconds=900)

        assert first == second
        kinds = {kind for _, kind, _ in first}
        assert {"location", "ping", "go_offline", "go_online"} <= kinds  # a break happened

    @pytest.mark.parametrize(
        ("other_seed", "other_id"), [(5, 4), (6, 3)], ids=["other_bot", "other_seed"]
    )
    async def test_other_bot_or_seed_behaves_differently(self, other_seed, other_id):
        one = await self.drive(seed=5, driver_id=3, seconds=60)

        other = await self.drive(seed=other_seed, driver_id=other_id, seconds=60)

        points = [data for _, kind, data in one if kind == "location"]
        other_points = [data for _, kind, data in other if kind == "location"]
        assert points != other_points


class TestSchedule:
    def test_next_slot_follows_the_schedule(self):
        assert _next_slot(10.0, 2.0, now=10.3) == 12.0

    def test_slots_missed_by_a_late_wake_up_are_skipped(self):
        # Woke up 5.5 s late: no burst of three ticks to catch up.
        assert _next_slot(10.0, 2.0, now=15.5) == 16.0
