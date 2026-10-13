"""Points, areas and distances on the Earth's surface, always as (lat, lon).

The "lon, lat" order of OSRM exists only inside sim/osrm.py.
"""

import math
import random
from typing import NamedTuple

EARTH_RADIUS_M = 6_371_000


class Point(NamedTuple):
    """A place in degrees; latitude first, as everywhere outside sim/osrm.py."""

    lat: float
    lon: float


class BoundingBox(NamedTuple):
    min_lat: float
    min_lon: float
    max_lat: float
    max_lon: float

    def contains(self, point: Point) -> bool:
        """Edges included, as on the server."""
        return (
            self.min_lat <= point.lat <= self.max_lat and self.min_lon <= point.lon <= self.max_lon
        )


def haversine_m(a: Point, b: Point) -> float:
    """Great-circle distance in metres."""
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    d_lat = lat2 - lat1
    d_lon = math.radians(b.lon - a.lon)
    h = math.sin(d_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(d_lon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def bearing_deg(a: Point, b: Point) -> float:
    """Initial compass direction from a to b: 0 north, 90 east, in [0, 360)."""
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    d_lon = math.radians(b.lon - a.lon)
    x = math.sin(d_lon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(d_lon)
    bearing = math.degrees(math.atan2(x, y)) % 360
    # A tiny negative angle wraps to exactly 360.0 in floating point.
    return 0.0 if bearing == 360 else bearing


def heading_for_server(bearing: float) -> float:
    """A bearing as sent in `location`: 0.1 degree, always in [0, 360).

    Rounded first, wrapped second: 359.97 rounds to 360.0, which the server rejects.
    """
    return round(bearing, 1) % 360


def random_point_in(box: BoundingBox, rng: random.Random) -> Point:
    """A uniformly random point of the box; the same rng state gives the same point."""
    return Point(
        lat=rng.uniform(box.min_lat, box.max_lat),
        lon=rng.uniform(box.min_lon, box.max_lon),
    )
