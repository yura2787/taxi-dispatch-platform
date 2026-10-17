"""Route polylines and RouteWalker: where a car is after driving a distance."""

import itertools
import math

import pytest

from sim.geo import Point, haversine_m
from sim.routes import Route, RouteWalker
from tests.factories import CENTRE, make_route, metres_north

# An L-shaped route: 500 m north, then ~740 m east.
CORNER = metres_north(CENTRE, 500)
EAST_END = Point(CORNER.lat, CORNER.lon + 0.01)
L_ROUTE = Route(geometry=(CENTRE, CORNER, EAST_END))


class TestRoute:
    def test_distance_is_the_sum_of_its_segments(self):
        route = make_route(legs_m=(300, 0, 700))

        assert route.distance_m == pytest.approx(1000, rel=1e-3)

    def test_route_needs_a_point(self):
        with pytest.raises(ValueError):
            Route(geometry=())

    def test_route_snapped_away_from_the_bot_starts_at_the_bot(self):
        # OSRM moved the start onto a road 150 m away: the gap is driven, not jumped.
        route = make_route(start=metres_north(CENTRE, 150))

        from_bot = route.starting_at(CENTRE)

        assert from_bot.geometry == (CENTRE, *route.geometry)

    def test_route_from_where_the_bot_stands_is_kept(self):
        route = make_route()

        from_bot = route.starting_at(CENTRE)

        assert from_bot is route


class TestRouteWalker:
    def test_position_in_the_middle_of_a_segment(self):
        walker = RouteWalker(make_route(legs_m=(1000,)))

        position = walker.at(250)

        assert haversine_m(CENTRE, position.point) == pytest.approx(250, rel=1e-3)
        assert position.heading == 0.0  # north
        assert position.finished is False

    def test_on_a_vertex_the_car_is_there_and_heads_along_the_next_segment(self):
        walker = RouteWalker(L_ROUTE)

        position = walker.at(haversine_m(CENTRE, CORNER))

        assert haversine_m(position.point, CORNER) < 0.01
        assert position.heading == pytest.approx(90, abs=0.2)

    @pytest.mark.parametrize(
        ("share", "expected_heading"),
        [(0.0, 0), (0.2, 0), (0.7, 90), (1.0, 90)],
        ids=["start", "first_leg", "second_leg", "end"],
    )
    def test_heading_follows_the_turns(self, share, expected_heading):
        walker = RouteWalker(L_ROUTE)

        position = walker.at(walker.length_m * share)

        assert position.heading == pytest.approx(expected_heading, abs=0.2)

    def test_distance_to_the_end_finishes_the_route(self):
        walker = RouteWalker(L_ROUTE)

        position = walker.at(walker.length_m)

        assert position == (EAST_END, pytest.approx(90, abs=0.2), True)

    def test_distance_past_the_end_stays_at_the_last_point(self):
        walker = RouteWalker(L_ROUTE)

        position = walker.at(walker.length_m + 100)

        assert position.point == EAST_END
        assert position.finished is True

    def test_negative_distance_is_the_start(self):
        walker = RouteWalker(L_ROUTE)

        position = walker.at(-5)

        assert position == (CENTRE, 0.0, False)

    def test_nan_distance_is_an_error(self):
        walker = RouteWalker(L_ROUTE)

        with pytest.raises(ValueError):
            walker.at(math.nan)

    @pytest.mark.parametrize("share", [0.0, 0.5, 0.999, 1.0])
    def test_zero_length_segments_keep_the_previous_heading(self, share):
        east = Point(CENTRE.lat, CENTRE.lon + 0.01)
        walker = RouteWalker(Route(geometry=(CENTRE, east, east, east)))

        position = walker.at(walker.length_m * share)

        assert position.heading == pytest.approx(90, abs=0.2)
        assert not math.isnan(position.point.lat)

    def test_leading_zero_length_segment_is_skipped(self):
        walker = RouteWalker(make_route(legs_m=(0, 500)))

        position = walker.at(0)

        assert position == (CENTRE, 0.0, False)

    @pytest.mark.parametrize(
        "geometry", [(CENTRE,), (CENTRE, CENTRE, CENTRE)], ids=["one_point", "same_points"]
    )
    def test_route_without_length_is_finished_and_has_no_heading(self, geometry):
        walker = RouteWalker(Route(geometry=geometry))

        position = walker.at(0)

        assert position == (CENTRE, None, True)

    def test_heading_close_to_north_is_sent_as_zero(self):
        # A segment whose bearing is ~359.96: rounded to 360.0, it must wrap to 0.0.
        almost_north = Point(CENTRE.lat + 0.01, CENTRE.lon - 0.00001)
        walker = RouteWalker(Route(geometry=(CENTRE, almost_north)))

        position = walker.at(10)

        assert position.heading == 0.0

    def test_two_seconds_at_top_speed_move_the_car_at_most_25_m(self):
        walker = RouteWalker(L_ROUTE)
        step_m = 45 / 3.6 * 2  # 45 km/h for 2 s

        positions = [walker.at(step_m * n).point for n in range(60)]

        largest_step = max(haversine_m(a, b) for a, b in itertools.pairwise(positions))
        assert largest_step <= 25 + 0.01  # float noise of the projection
