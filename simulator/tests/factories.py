"""Builders of test objects: keyword arguments only, sensible defaults."""

from sim.geo import Point
from sim.routes import Route

# Central Square of Chernivtsi.
CENTRE = Point(lat=48.2921, lon=25.9358)

# Along a meridian, one metre is this many degrees of latitude.
DEGREES_PER_METER_LAT = 1 / 111_195


def metres_north(point: Point, metres: float) -> Point:
    return Point(lat=point.lat + metres * DEGREES_PER_METER_LAT, lon=point.lon)


def make_route(*, start: Point = CENTRE, legs_m: tuple[float, ...] = (500, 500)) -> Route:
    """A route going north from `start` in legs of the given lengths; a zero-length
    leg repeats a point, as OSRM sometimes does."""
    geometry = [start]
    for leg in legs_m:
        geometry.append(metres_north(geometry[-1], leg))
    return Route(geometry=tuple(geometry))
