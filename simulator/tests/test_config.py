"""Relations between config constants: whatever the exact values, the bots must stay
within the server's limits and pick destinations inside the service area."""

import math

from sim import config

METERS_PER_DEGREE_LAT = 111_195


def margin_m(inner: config.BoundingBox, outer: config.BoundingBox) -> float:
    """The smallest distance from an edge of `inner` to the same edge of `outer`."""
    meters_per_degree_lon = METERS_PER_DEGREE_LAT * math.cos(math.radians(outer.max_lat))
    return min(
        (inner.min_lat - outer.min_lat) * METERS_PER_DEGREE_LAT,
        (outer.max_lat - inner.max_lat) * METERS_PER_DEGREE_LAT,
        (inner.min_lon - outer.min_lon) * meters_per_degree_lon,
        (outer.max_lon - inner.max_lon) * meters_per_degree_lon,
    )


class TestServerLimits:
    def test_no_point_is_throttled_even_with_jitter(self):
        assert config.LOCATION_INTERVAL_S >= 2 * config.SERVER_LOCATION_MIN_INTERVAL_S

    def test_pings_keep_an_idle_connection_open_with_room_to_spare(self):
        assert config.PING_INTERVAL_S * 2 <= config.SERVER_WS_IDLE_TIMEOUT_S

    def test_bot_uses_less_than_half_of_the_message_rate_limit(self):
        per_second = 1 / config.LOCATION_INTERVAL_S + 1 / config.PING_INTERVAL_S

        assert per_second < config.SERVER_WS_MSG_RATE_PER_S / 2

    def test_silent_bot_is_always_taken_offline_by_the_reaper(self):
        assert config.SILENCE_S > config.SERVER_DRIVER_SILENCE_S + config.SERVER_REAPER_INTERVAL_S

    def test_fastest_bot_is_far_below_the_jump_filter(self):
        assert max(config.SPEED_KMH) < config.SERVER_MAX_SPEED_KMH / 2


class TestAreas:
    def test_city_area_lies_at_least_1_km_inside_the_service_area(self):
        assert margin_m(config.CITY_AREA, config.SERVICE_AREA) >= 1000


class TestRanges:
    def test_ranges_are_ordered(self):
        ranges = [
            config.SPEED_KMH,
            config.ROUTE_END_PAUSE_S,
            config.BREAK_EVERY_S,
            config.BREAK_DURATION_S,
            config.FLAKY_EVERY_S,
            config.DROP_DURATION_S,
        ]

        assert all(low <= high for low, high in ranges)
