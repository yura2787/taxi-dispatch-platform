"""Minimal OSRM client of the simulator: snap a point to a road, get a route.

Takes and returns (lat, lon) points; the "lon,lat" order of OSRM lives only here.
No circuit breaker and no metrics, unlike the server's client: a bot that cannot get
a route simply stands still and asks again later.
"""

import asyncio
from collections.abc import Callable
from typing import Any, TypeVar

import httpx

from sim import config
from sim.geo import Point
from sim.routes import Route

T = TypeVar("T")


class OsrmError(Exception):
    """Any failure of an OSRM call."""


class OsrmUnavailable(OsrmError):
    """OSRM could not answer: down, timed out, 5xx or a malformed response."""


class NoRoute(OsrmError):
    """OSRM answered, but there is no road near a point or no route between them."""


class OsrmClient:
    """nearest() and route(); every failure is OsrmUnavailable or NoRoute."""

    def __init__(self, http: httpx.AsyncClient):
        # base_url of `http` must point at the OSRM server.
        self._http = http

    async def nearest(self, point: Point) -> Point:
        """The point moved onto the nearest road, however far it is."""
        return await self._get(
            f"/nearest/v1/driving/{_coordinates([point])}",
            {"number": "1"},
            lambda body: _to_point(body["waypoints"][0]["location"]),
        )

    async def route(self, origin: Point, destination: Point) -> Route:
        def parse(body: dict[str, Any]) -> Route:
            coordinates = body["routes"][0]["geometry"]["coordinates"]
            return Route(geometry=tuple(_to_point(lon_lat) for lon_lat in coordinates))

        return await self._get(
            f"/route/v1/driving/{_coordinates([origin, destination])}",
            {"overview": "full", "geometries": "geojson"},
            parse,
        )

    async def _get(
        self, path: str, params: dict[str, str], parse: Callable[[dict[str, Any]], T]
    ) -> T:
        try:
            # httpx timeouts apply to each phase separately; this caps the whole request.
            async with asyncio.timeout(config.OSRM_TIMEOUT_S):
                response = await self._http.get(path, params=params)
        except (TimeoutError, httpx.TimeoutException):
            raise OsrmUnavailable("timeout") from None
        except httpx.HTTPError as exc:
            raise OsrmUnavailable(f"{type(exc).__name__}: {exc}") from exc

        if response.status_code >= 500:
            raise OsrmUnavailable(f"HTTP {response.status_code}")
        # NoSegment and NoRoute come with HTTP 400 and a JSON body, so 4xx is parsed too.
        try:
            body = response.json()
        except ValueError:
            raise OsrmUnavailable(f"invalid JSON (HTTP {response.status_code})") from None

        code = body.get("code") if isinstance(body, dict) else None
        if code in ("NoSegment", "NoRoute"):
            raise NoRoute(code)
        if code != "Ok":
            raise OsrmUnavailable(f"unexpected code {code!r} (HTTP {response.status_code})")
        try:
            return parse(body)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            # "Ok", but not the shape we expect (e.g. no routes): as broken as bad JSON.
            raise OsrmUnavailable(f"malformed response: {exc!r}") from exc


def _coordinates(points: list[Point]) -> str:
    return ";".join(f"{p.lon},{p.lat}" for p in points)


def _to_point(lon_lat: list[float]) -> Point:
    lon, lat = lon_lat
    return Point(lat=float(lat), lon=float(lon))
