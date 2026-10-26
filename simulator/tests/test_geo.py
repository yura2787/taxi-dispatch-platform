"""Distances, bearings and areas of sim/geo.py."""

import math
import random

import pytest

from sim.geo import (
    BoundingBox,
    Point,
    bearing_deg,
    haversine_m,
    heading_for_server,
    random_point_in,
)
from tests.factories import CENTRE

# The railway station of Chernivtsi; the same pair as in realtime's tests.
STATION = Point(lat=48.2668, lon=25.9306)


class TestHaversine:
    def test_one_degree_of_a_meridian_is_about_111_km(self):
        assert haversine_m(Point(0, 0), Point(1, 0)) == pytest.approx(111_195, rel=1e-4)

    def test_distance_between_two_city_places(self):
        assert haversine_m(CENTRE, STATION) == pytest.approx(2_840, rel=0.01)

    def test_distance_is_symmetric(self):
        assert haversine_m(CENTRE, STATION) == pytest.approx(haversine_m(STATION, CENTRE))

    def test_distance_to_the_same_point_is_zero(self):
        assert haversine_m(CENTRE, CENTRE) == 0


class TestBearing:
    @pytest.mark.parametrize(
        ("target", "expected"),
        [
            (Point(48.30, 25.9358), 0),
            (Point(48.2921, 25.95), 90),
            (Point(48.28, 25.9358), 180),
            (Point(48.2921, 25.92), 270),
        ],
        ids=["north", "east", "south", "west"],
    )
    def test_compass_directions(self, target, expected):
        assert bearing_deg(CENTRE, target) == pytest.approx(expected, abs=0.1)

    def test_westward_bearing_is_positive(self):
        # atan2 gives -45 for north-west; the compass says 315.
        north_west = Point(CENTRE.lat + 0.01, CENTRE.lon - 0.01 / math.cos(math.radians(48.29)))

        assert bearing_deg(CENTRE, north_west) == pytest.approx(315, abs=0.2)

    def test_tiny_westward_angle_is_zero_not_360(self):
        # -1e-18 degrees % 360 is exactly 360.0 in floating point.
        assert bearing_deg(Point(0, 0), Point(0.01, -1e-18)) == 0.0


class TestHeadingForServer:
    @pytest.mark.parametrize(
        ("bearing", "expected"),
        [(359.96, 0.0), (359.94, 359.9), (0.04, 0.0), (90.06, 90.1)],
    )
    def test_rounded_to_a_tenth_and_never_360(self, bearing, expected):
        assert heading_for_server(bearing) == expected


class TestBoundingBox:
    BOX = BoundingBox(min_lat=48.0, min_lon=25.0, max_lat=49.0, max_lon=26.0)

    @pytest.mark.parametrize(
        ("point", "inside"),
        [
            (Point(48.5, 25.5), True),
            (Point(48.0, 25.0), True),  # edges are inside, as on the server
            (Point(49.0, 26.0), True),
            (Point(47.99, 25.5), False),
            (Point(48.5, 26.01), False),
        ],
    )
    def test_contains(self, point, inside):
        assert self.BOX.contains(point) is inside

    def test_random_point_is_always_inside(self):
        rng = random.Random(1)

        points = [random_point_in(self.BOX, rng) for _ in range(1000)]

        assert all(self.BOX.contains(point) for point in points)

    def test_random_point_depends_only_on_the_seed(self):
        first = random_point_in(self.BOX, random.Random(7))

        second = random_point_in(self.BOX, random.Random(7))

        assert second == first
