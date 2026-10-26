"""A route as a polyline, and a walker that tells where a car is after driving a
given distance along it.

The position comes from the distance driven (speed x time), never from OSRM
durations: the bot chooses its own speed.
"""

import bisect
import itertools
import math
from dataclasses import dataclass
from functools import cached_property
from typing import NamedTuple

from sim.geo import Point, bearing_deg, haversine_m, heading_for_server

# Closer than this the bot is already at the start of a new route; a farther gap
# (OSRM snapped the start to another road) is driven in a straight line first.
SAME_PLACE_M = 1.0


@dataclass(frozen=True)
class Route:
    """A polyline the car drives along, from the first point to the last."""

    geometry: tuple[Point, ...]

    def __post_init__(self) -> None:
        if not self.geometry:
            raise ValueError("a route needs at least one point")

    @cached_property
    def distance_m(self) -> float:
        """Along the polyline; RouteWalker.length_m is the same sum."""
        return sum(haversine_m(a, b) for a, b in itertools.pairwise(self.geometry))

    def starting_at(self, position: Point) -> "Route":
        """The same route, but from the bot's own position: no teleport to the road."""
        if haversine_m(position, self.geometry[0]) < SAME_PLACE_M:
            return self
        return Route(geometry=(position, *self.geometry))


class Position(NamedTuple):
    point: Point
    # Of the segment being driven, ready for the server; None if the route has no
    # segment of non-zero length.
    heading: float | None
    # The car has reached the last point; driving further keeps it there.
    finished: bool


class RouteWalker:
    """Positions along one route, computed once per route and asked every tick."""

    def __init__(self, route: Route):
        self._points = route.geometry
        # _starts[i]: distance from the start to point i.
        self._starts = [0.0]
        # _headings[i]: heading on the segment from point i to point i + 1. A segment
        # of zero length (OSRM repeats points) keeps the heading of the one before.
        self._headings: list[float | None] = []
        heading = None
        for a, b in itertools.pairwise(self._points):
            length = haversine_m(a, b)
            if length > 0:
                heading = heading_for_server(bearing_deg(a, b))
            self._starts.append(self._starts[-1] + length)
            self._headings.append(heading)

    @property
    def length_m(self) -> float:
        return self._starts[-1]

    def at(self, distance_m: float) -> Position:
        """Where the car is after driving distance_m from the start of the route."""
        if math.isnan(distance_m):
            # Only a bug in the speed x time arithmetic gets here: fail loudly.
            raise ValueError("distance is NaN")
        if distance_m >= self.length_m:
            last_heading = self._headings[-1] if self._headings else None
            return Position(self._points[-1], last_heading, finished=True)
        distance_m = max(distance_m, 0.0)
        # The segment that contains the distance; never a zero-length one, since its
        # start equals the next segment's start and bisect_right goes past it.
        index = bisect.bisect_right(self._starts, distance_m) - 1
        a, b = self._points[index], self._points[index + 1]
        share = (distance_m - self._starts[index]) / (self._starts[index + 1] - self._starts[index])
        point = Point(lat=a.lat + (b.lat - a.lat) * share, lon=a.lon + (b.lon - a.lon) * share)
        return Position(point, self._headings[index], finished=False)
