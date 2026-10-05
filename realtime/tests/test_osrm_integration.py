"""Tests against a real OSRM with the Chernivtsi graph (`make osrm`, then `make up`).

Skipped when OSRM is not reachable, except with OSRM_REQUIRED=1 (CI), where that fails.
"""

import math
import os

import httpx
import pytest

from app import config
from app.services.osrm import CITY_CENTRE, NoRoute, OsrmClient, OsrmUnavailable, Point

pytestmark = pytest.mark.osrm

RAILWAY_STATION = Point(lat=48.2668, lon=25.9306)
UNIVERSITY = Point(lat=48.2963, lon=25.9241)
THEATRE_SQUARE = Point(lat=48.2930, lon=25.9343)
# Inside the Tsetsyno forest, ~500 m from the nearest road a car may use.
FOREST = Point(lat=48.315, lon=25.855)


def metres_between(a: Point, b: Point) -> float:
    # Equirectangular approximation: precise enough within a city.
    x = math.radians(b.lon - a.lon) * math.cos(math.radians((a.lat + b.lat) / 2))
    y = math.radians(b.lat - a.lat)
    return math.hypot(x, y) * 6_371_000


@pytest.fixture
async def osrm():
    client = OsrmClient(httpx.AsyncClient(base_url=config.OSRM_URL))
    try:
        await client.nearest(CITY_CENTRE)
    except OsrmUnavailable as exc:
        await client.aclose()
        message = f"OSRM is not reachable at {config.OSRM_URL} ({exc}); run `make osrm`"
        if os.environ.get("OSRM_REQUIRED") == "1":
            pytest.fail(message)
        pytest.skip(message)
    yield client
    await client.aclose()


async def test_route_from_central_square_to_railway_station(osrm):
    route = await osrm.route(CITY_CENTRE, RAILWAY_STATION, with_geometry=True)

    assert 2_000 <= route.distance_m <= 6_000
    # 2-6 km through the city: between ~3 and ~20 minutes.
    assert 180 <= route.duration_s <= 1_200
    assert metres_between(route.snapped_origin, CITY_CENTRE) < config.OSRM_SNAP_RADIUS_M
    assert metres_between(route.snapped_destination, RAILWAY_STATION) < config.OSRM_SNAP_RADIUS_M
    assert route.geometry is not None
    assert len(route.geometry) > 2


async def test_table_from_three_sources_to_one_destination(osrm):
    table = await osrm.table([CITY_CENTRE, UNIVERSITY, THEATRE_SQUARE], [RAILWAY_STATION])

    assert len(table.durations) == 3
    assert all(len(row) == 1 and row[0] is not None and row[0] > 0 for row in table.durations)
    assert len(table.snapped_sources) == 3
    assert len(table.snapped_destinations) == 1


async def test_point_far_from_roads_has_no_road_nearby(osrm):
    with pytest.raises(NoRoute) as exc_info:
        await osrm.route(FOREST, RAILWAY_STATION)

    assert exc_info.value.reason == "no_road_nearby"
