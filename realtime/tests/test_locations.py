"""Driver location checks: service area, distance and speed, and LocationFilter
(throttle, clock-skew model with its reset, GPS jump filter, commit)."""

import pytest

from app import config
from app.services.locations import (
    AcceptedPoint,
    Location,
    LocationFilter,
    Verdict,
    haversine_m,
    in_service_area,
    speed_kmh,
)

CENTRE = Location(lat=48.2921, lon=25.9358, ts=0)
STATION = Location(lat=48.2668, lon=25.9306, ts=0)
# ~3 km north of the centre.
NORTH_3_KM = Location(lat=48.3191, lon=25.9358, ts=0)

# A phone whose clock is 3 minutes behind the server.
LAG_MS = 3 * 60 * 1000


def at(clock, place: Location = CENTRE, *, lag_ms: int = 0) -> Location:
    """A point at `place` with ts from the phone's clock, `lag_ms` behind the server."""
    return Location(lat=place.lat, lon=place.lon, ts=clock.now_ms() - lag_ms)


def accept(location_filter: LocationFilter, location: Location) -> None:
    """Check a point that must pass, and commit it as if Redis accepted it."""
    decision = location_filter.check(location)
    assert decision.verdict is Verdict.PASSED
    location_filter.commit(decision)


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


class TestServiceArea:
    def test_city_centre_is_inside(self):
        assert in_service_area(CENTRE)

    @pytest.mark.parametrize(
        ("lat", "lon"),
        [(48.22, 25.80), (48.36, 26.08), (48.22, 25.9358), (48.2921, 26.08)],
        ids=["south-west-corner", "north-east-corner", "south-edge", "east-edge"],
    )
    def test_edges_are_inside(self, lat, lon):
        assert in_service_area(Location(lat=lat, lon=lon, ts=0))

    @pytest.mark.parametrize(
        ("lat", "lon"),
        [(48.2199, 25.9358), (48.3601, 25.9358), (48.2921, 25.7999), (48.2921, 26.0801)],
        ids=["south", "north", "west", "east"],
    )
    def test_points_just_outside_each_side_are_outside(self, lat, lon):
        assert not in_service_area(Location(lat=lat, lon=lon, ts=0))

    def test_area_comes_from_config(self, monkeypatch):
        monkeypatch.setattr(config, "SERVICE_AREA", config.BoundingBox(0, 0, 1, 1))

        assert not in_service_area(CENTRE)


class TestDistance:
    def test_central_square_to_railway_station_is_about_2_84_km(self):
        assert haversine_m(CENTRE, STATION) == pytest.approx(2_840, rel=0.01)

    def test_distance_is_symmetric(self):
        assert haversine_m(CENTRE, STATION) == pytest.approx(haversine_m(STATION, CENTRE))

    def test_same_point_is_zero_metres_away(self):
        assert haversine_m(CENTRE, CENTRE) == 0

    def test_speed_is_distance_over_phone_time(self):
        # 3 km in 2 minutes: 90 km/h.
        assert speed_kmh(CENTRE, NORTH_3_KM, 120_000) == pytest.approx(90, rel=0.01)


# ---------------------------------------------------------------------------
# Throttle
# ---------------------------------------------------------------------------


class TestThrottle:
    def test_second_point_within_a_second_is_throttled(self, clock):
        location_filter = LocationFilter(clock)
        accept(location_filter, at(clock))

        clock.advance(0.5)
        decision = location_filter.check(at(clock))

        assert decision.verdict is Verdict.THROTTLED

    def test_point_a_second_later_passes(self, clock):
        location_filter = LocationFilter(clock)
        accept(location_filter, at(clock))

        clock.advance(1.0)

        accept(location_filter, at(clock))

    def test_interval_counts_from_the_last_point_that_passed_the_throttle(self, clock):
        location_filter = LocationFilter(clock)
        accept(location_filter, at(clock))
        clock.advance(0.6)
        location_filter.check(at(clock))  # throttled: does not restart the interval

        clock.advance(0.4)

        accept(location_filter, at(clock))

    def test_point_rejected_after_the_throttle_still_restarts_the_interval(self, clock):
        location_filter = LocationFilter(clock)
        accept(location_filter, at(clock))
        clock.advance(2)
        assert location_filter.check(at(clock, NORTH_3_KM)).verdict is Verdict.JUMP

        clock.advance(0.5)
        decision = location_filter.check(at(clock))

        assert decision.verdict is Verdict.THROTTLED


# ---------------------------------------------------------------------------
# Clock-skew model
# ---------------------------------------------------------------------------


class TestClockModel:
    @pytest.mark.parametrize("lag_ms", [LAG_MS, -LAG_MS], ids=["phone-behind", "phone-ahead"])
    def test_phone_clock_minutes_off_is_accepted(self, clock, lag_ms):
        location_filter = LocationFilter(clock)

        for _ in range(3):
            accept(location_filter, at(clock, lag_ms=lag_ms))
            clock.advance(2)

    def test_point_buffered_20_s_is_stale(self, clock):
        location_filter = LocationFilter(clock)
        accept(location_filter, at(clock, lag_ms=LAG_MS))
        clock.advance(30)

        # Recorded 20 s ago (still after the previous point), delivered only now.
        decision = location_filter.check(at(clock, lag_ms=LAG_MS + 20_000))

        assert decision.verdict is Verdict.STALE

    def test_point_delayed_by_exactly_the_max_age_is_accepted(self, clock):
        location_filter = LocationFilter(clock)
        accept(location_filter, at(clock))
        clock.advance(30)

        accept(location_filter, at(clock, lag_ms=config.LOCATION_MAX_AGE_S * 1000))

    def test_faster_delivery_tightens_the_model(self, clock):
        location_filter = LocationFilter(clock)
        accept(location_filter, at(clock, lag_ms=8_000))  # a slow first delivery
        clock.advance(2)
        accept(location_filter, at(clock))  # then an instant one
        clock.advance(30)

        # 8 s late is fine against the first point, but not against the instant one.
        decision = location_filter.check(at(clock, lag_ms=config.LOCATION_MAX_AGE_S * 1000 + 1))

        assert decision.verdict is Verdict.STALE

    @pytest.mark.parametrize("back_ms", [0, 1_000], ids=["same-ts", "earlier-ts"])
    def test_point_not_newer_than_the_last_accepted_is_out_of_order(self, clock, back_ms):
        location_filter = LocationFilter(clock)
        first = at(clock)
        accept(location_filter, first)
        clock.advance(2)

        decision = location_filter.check(
            Location(lat=first.lat, lon=first.lon, ts=first.ts - back_ms)
        )

        assert decision.verdict is Verdict.OUT_OF_ORDER

    def test_point_older_than_the_last_one_of_the_previous_connection_is_out_of_order(self, clock):
        last = AcceptedPoint(CENTRE.lat, CENTRE.lon, ts=clock.now_ms(), server_ms=clock.now_ms())
        clock.advance(20)
        location_filter = LocationFilter(clock, last=last)

        # Buffered before the reconnect, older than what Redis already has.
        decision = location_filter.check(at(clock, lag_ms=25_000))

        assert decision.verdict is Verdict.OUT_OF_ORDER

    def test_first_points_of_a_connection_are_not_checked_for_staleness(self, clock):
        # Known limitation: with nothing to compare to, a buffered point newer than
        # the last accepted one passes as fresh, and becomes the reference itself.
        location_filter = LocationFilter(clock)

        accept(location_filter, at(clock, lag_ms=60_000))
        clock.advance(2)
        accept(location_filter, at(clock, lag_ms=60_000))

    def test_point_refused_by_redis_is_not_remembered(self, clock):
        location_filter = LocationFilter(clock)
        accept(location_filter, at(clock))
        clock.advance(2)
        refused = location_filter.check(at(clock))  # passed here, but never committed
        assert refused.verdict is Verdict.PASSED
        clock.advance(2)

        # Older than the refused point, newer than the accepted one.
        decision = location_filter.check(at(clock, lag_ms=3_000))

        assert decision.verdict is Verdict.PASSED


class TestClockReset:
    def reject_clock(self, clock, location_filter: LocationFilter, ts: int, times: int) -> None:
        for _ in range(times):
            clock.advance(2)
            decision = location_filter.check(Location(lat=CENTRE.lat, lon=CENTRE.lon, ts=ts))
            assert decision.verdict is Verdict.OUT_OF_ORDER

    def test_after_five_rejections_in_a_row_the_next_point_resets_the_model(self, clock):
        location_filter = LocationFilter(clock)
        old_ts = clock.now_ms()
        accept(location_filter, at(clock))
        # The phone's clock is set back by 5 minutes.
        self.reject_clock(
            clock, location_filter, old_ts - 300_000, config.LOCATION_CLOCK_RESET_AFTER
        )

        clock.advance(2)
        accept(location_filter, at(clock, lag_ms=300_000))
        clock.advance(2)

        accept(location_filter, at(clock, lag_ms=300_000))

    def test_reset_point_is_not_checked_for_jumps(self, clock):
        location_filter = LocationFilter(clock)
        old_ts = clock.now_ms()
        accept(location_filter, at(clock))
        self.reject_clock(clock, location_filter, old_ts - 1, config.LOCATION_CLOCK_RESET_AFTER)

        clock.advance(2)

        # 3 km in 2 s, but the previous point belongs to the phone's old clock.
        accept(location_filter, at(clock, NORTH_3_KM, lag_ms=300_000))

    def test_point_that_passes_the_clock_check_ends_the_run(self, clock):
        location_filter = LocationFilter(clock)
        old_ts = clock.now_ms()
        accept(location_filter, at(clock))
        self.reject_clock(clock, location_filter, old_ts, config.LOCATION_CLOCK_RESET_AFTER)
        clock.advance(2)
        assert location_filter.check(at(clock, NORTH_3_KM)).verdict is Verdict.JUMP

        self.reject_clock(clock, location_filter, old_ts, 1)

    def test_throttled_point_does_not_end_the_run(self, clock):
        location_filter = LocationFilter(clock)
        old_ts = clock.now_ms()
        accept(location_filter, at(clock))
        self.reject_clock(clock, location_filter, old_ts, config.LOCATION_CLOCK_RESET_AFTER)
        clock.advance(0.5)
        assert location_filter.check(at(clock)).verdict is Verdict.THROTTLED

        clock.advance(2)

        accept(location_filter, at(clock, lag_ms=300_000))


# ---------------------------------------------------------------------------
# GPS jumps
# ---------------------------------------------------------------------------


class TestJump:
    def test_3_km_in_2_s_is_a_jump(self, clock):
        location_filter = LocationFilter(clock)
        accept(location_filter, at(clock))
        clock.advance(2)

        decision = location_filter.check(at(clock, NORTH_3_KM))

        assert decision.verdict is Verdict.JUMP

    def test_same_move_after_the_reset_pause_is_accepted(self, clock):
        location_filter = LocationFilter(clock)
        # The phone was silent, e.g. in a tunnel, and only its last point got through.
        accept(location_filter, at(clock))
        clock.advance(config.LOCATION_JUMP_RESET_S + 1)

        accept(location_filter, Location(NORTH_3_KM.lat, NORTH_3_KM.lon, ts=clock.now_ms() - 2_000))

    def test_move_right_at_the_reset_pause_is_still_checked(self, clock):
        location_filter = LocationFilter(clock)
        accept(location_filter, at(clock))
        clock.advance(config.LOCATION_JUMP_RESET_S)

        decision = location_filter.check(at(clock, NORTH_3_KM))

        assert decision.verdict is Verdict.JUMP

    def test_speed_uses_phone_time_not_arrival_time(self):
        # Two points recorded 2 s apart but delivered together: 22.2 m in 2 s is
        # 40 km/h. (In the pipeline the second one is throttled, which is fine.)
        north_22_m = Location(lat=CENTRE.lat + 0.0002, lon=CENTRE.lon, ts=0)

        assert speed_kmh(CENTRE, north_22_m, 2_000) == pytest.approx(40, rel=0.01)

    def test_jump_is_not_remembered(self, clock):
        location_filter = LocationFilter(clock)
        accept(location_filter, at(clock))
        clock.advance(2)
        assert location_filter.check(at(clock, NORTH_3_KM)).verdict is Verdict.JUMP
        clock.advance(2)

        accept(location_filter, at(clock))

    def test_point_loaded_on_connect_is_the_reference(self, clock):
        last = AcceptedPoint(CENTRE.lat, CENTRE.lon, ts=clock.now_ms(), server_ms=clock.now_ms())
        clock.advance(2)
        location_filter = LocationFilter(clock, last=last)

        decision = location_filter.check(at(clock, NORTH_3_KM))

        assert decision.verdict is Verdict.JUMP


# ---------------------------------------------------------------------------
# Decision and commit
# ---------------------------------------------------------------------------


class TestDecision:
    def test_passed_point_outside_the_area_is_not_in_zone(self, clock):
        decision = LocationFilter(clock).check(Location(lat=50.45, lon=30.52, ts=clock.now_ms()))

        assert decision.verdict is Verdict.PASSED
        assert decision.in_zone is False

    def test_passed_point_carries_what_redis_needs(self, clock):
        location = at(clock, lag_ms=LAG_MS)

        decision = LocationFilter(clock).check(location)

        assert decision.in_zone is True
        assert decision.point == AcceptedPoint(
            location.lat, location.lon, location.ts, clock.now_ms()
        )
        assert decision.min_skew_ms == LAG_MS

    def test_rejected_point_cannot_be_committed(self, clock):
        location_filter = LocationFilter(clock)
        accept(location_filter, at(clock))
        clock.advance(0.5)
        throttled = location_filter.check(at(clock))

        with pytest.raises(ValueError, match="throttled"):
            location_filter.commit(throttled)
