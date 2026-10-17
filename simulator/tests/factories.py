"""Builders of test objects: keyword arguments only, sensible defaults."""

import json
import random
from types import SimpleNamespace

from sim.driver_bot import BotStats, DriverBot
from sim.geo import Point
from sim.latency import LatencyRecorder
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


def make_snapshot(*, status: str = "offline", position: Point | None = None) -> str:
    """A state_snapshot as the server sends it."""
    data = {"status": status, "tariff": "economy", "in_zone": True, "position": None}
    if position is not None:
        data["position"] = {
            "lat": position.lat,
            "lon": position.lon,
            "heading": None,
            "speed": None,
        }
    return json.dumps({"type": "state_snapshot", "data": data})


def make_latency(values: list[float]) -> LatencyRecorder:
    """A recorder with these samples, e.g. the RTTs of one bot."""
    recorder = LatencyRecorder()
    for value in values:
        recorder.record(value)
    return recorder


def make_bot_view(
    *,
    flaky: bool = False,
    stop_reason: str | None = None,
    connected: bool = True,
    status: str | None = "available",
    on_break: bool = False,
    **stats,
) -> SimpleNamespace:
    """What the report and the strict checks read from a bot: its stats (by default
    of a bot that started and got online) and a few state fields."""
    return SimpleNamespace(
        stats=BotStats(**({"started": True, "went_online": True} | stats)),
        flaky=flaky,
        stop_reason=stop_reason,
        connected=connected,
        status=status,
        on_break=on_break,
    )


def make_bot(
    *, server, osrm, clock, driver_id: int = 7, seed: int = 1, flaky: bool = False
) -> DriverBot:
    """A bot wired to the in-memory server and OSRM of tests/conftest.py."""
    return DriverBot(
        driver_id,
        connect=server.connect,
        osrm=osrm,
        clock=clock,
        rng=random.Random(f"{seed}:{driver_id}"),
        flaky=flaky,
    )
