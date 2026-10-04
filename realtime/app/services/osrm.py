"""Async client for the OSRM routing server.

The public API takes and returns {lat, lon} only. OSRM wants "lon,lat", and that
conversion happens in this module and nowhere else.

The client never falls back to straight-line distance: it raises, and each caller
decides how to degrade.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from app import config
from app.metrics import OSRM_REQUEST_DURATION, OSRM_REQUESTS

logger = logging.getLogger(__name__)

Endpoint = Literal["route", "table", "nearest"]


class Point(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


# Central Square; any point on a road works for a cheap "is OSRM alive" query.
CITY_CENTRE = Point(lat=48.2921, lon=25.9358)


class Route(BaseModel):
    distance_m: int
    # Already multiplied by OSRM_DURATION_FACTOR.
    duration_s: int
    # Where the car actually stops: the input points moved onto the road.
    snapped_origin: Point
    snapped_destination: Point
    # Only when requested with with_geometry=True.
    geometry: list[Point] | None = None


class Table(BaseModel):
    # durations[i][j]: from sources[i] to destinations[j], with OSRM_DURATION_FACTOR;
    # None if there is no route between them.
    durations: list[list[int | None]]
    snapped_sources: list[Point]
    snapped_destinations: list[Point]


class OsrmError(Exception):
    pass


class OsrmUnavailable(OsrmError):
    """OSRM could not answer: down, timed out, 5xx or a malformed response."""


class NoRoute(OsrmError):
    """OSRM answered, but there is no road near a point or no route between them."""

    def __init__(self, reason: Literal["no_road_nearby", "no_route"]):
        super().__init__(reason)
        self.reason = reason


class CircuitBreaker:
    """Stops calling OSRM for a while after repeated "unavailable" errors.

    Closed: requests go through. After OSRM_BREAKER_FAILURES failures in a row it
    opens: requests fail at once, without waiting for timeouts. After
    OSRM_BREAKER_RESET_S one probe request goes through: success closes the breaker,
    failure opens it again.

    The state lives in this process only. That is enough: each process notices an
    outage on its own after a few failed requests, and sharing the state (e.g. via
    Redis) would add a dependency just to save those few requests.
    """

    def __init__(
        self,
        failures: int = config.OSRM_BREAKER_FAILURES,
        reset_s: float = config.OSRM_BREAKER_RESET_S,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._max_failures = failures
        self._reset_s = reset_s
        # A parameter, so tests can move time without sleeping.
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._probing = False

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None

    def allow(self) -> bool:
        if self._opened_at is None:
            return True
        # Exactly one probe: concurrent requests keep failing fast until it finishes.
        if self._probing or self._clock() - self._opened_at < self._reset_s:
            return False
        self._probing = True
        return True

    def record_success(self) -> None:
        if self._opened_at is not None:
            logger.info("OSRM circuit breaker closed: OSRM answers again")
        self._failures = 0
        self._opened_at = None
        self._probing = False

    def record_failure(self) -> None:
        self._failures += 1
        if self._probing or (self._opened_at is None and self._failures >= self._max_failures):
            logger.warning(
                "OSRM circuit breaker opened after %d failures, retry in %ss",
                self._failures,
                self._reset_s,
            )
            self._opened_at = self._clock()
            self._probing = False

    def release_probe(self) -> None:
        """The probe ended without an answer (e.g. cancelled): allow another one."""
        self._probing = False


class OsrmClient:
    def __init__(self, http: httpx.AsyncClient, breaker: CircuitBreaker | None = None):
        # base_url of `http` must point at the OSRM server.
        self._http = http
        self._breaker = breaker or CircuitBreaker()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def route(
        self, origin: Point, destination: Point, *, with_geometry: bool = False
    ) -> Route:
        points = [origin, destination]
        params = {"radiuses": _radiuses(points)}
        if with_geometry:
            params |= {"overview": "full", "geometries": "geojson"}
        else:
            params["overview"] = "false"

        body = await self._get("route", f"/route/v1/driving/{_coordinates(points)}", params)

        route = body["routes"][0]
        geometry = None
        if with_geometry:
            geometry = [_to_point(lon_lat) for lon_lat in route["geometry"]["coordinates"]]
        return Route(
            distance_m=round(route["distance"]),
            duration_s=_with_factor(route["duration"]),
            snapped_origin=_to_point(body["waypoints"][0]["location"]),
            snapped_destination=_to_point(body["waypoints"][1]["location"]),
            geometry=geometry,
        )

    async def table(self, sources: list[Point], destinations: list[Point]) -> Table:
        if not sources or not destinations:
            raise ValueError("table needs at least one source and one destination")
        points = sources + destinations
        if len(points) > config.OSRM_TABLE_MAX_POINTS:
            raise ValueError(
                f"table supports at most {config.OSRM_TABLE_MAX_POINTS} points, got {len(points)}"
            )

        params = {
            "sources": ";".join(str(i) for i in range(len(sources))),
            "destinations": ";".join(str(i) for i in range(len(sources), len(points))),
            "annotations": "duration",
            "radiuses": _radiuses(points),
        }
        body = await self._get("table", f"/table/v1/driving/{_coordinates(points)}", params)

        return Table(
            durations=[
                [None if seconds is None else _with_factor(seconds) for seconds in row]
                for row in body["durations"]
            ],
            snapped_sources=[_to_point(s["location"]) for s in body["sources"]],
            snapped_destinations=[_to_point(d["location"]) for d in body["destinations"]],
        )

    async def nearest(self, point: Point) -> Point:
        """The point moved onto the nearest road (within OSRM_SNAP_RADIUS_M)."""
        params = {"number": "1", "radiuses": _radiuses([point])}
        body = await self._get("nearest", f"/nearest/v1/driving/{_coordinates([point])}", params)
        return _to_point(body["waypoints"][0]["location"])

    async def _get(self, endpoint: Endpoint, path: str, params: dict[str, str]) -> dict[str, Any]:
        if not self._breaker.allow():
            OSRM_REQUESTS.labels(endpoint, "circuit_open").inc()
            raise OsrmUnavailable(f"{endpoint}: circuit breaker is open")
        try:
            with OSRM_REQUEST_DURATION.labels(endpoint).time():
                body = await self._request(endpoint, path, params)
        except OsrmUnavailable:
            OSRM_REQUESTS.labels(endpoint, "unavailable").inc()
            self._breaker.record_failure()
            raise
        except NoRoute:
            OSRM_REQUESTS.labels(endpoint, "no_route").inc()
            # OSRM answered, so it works: not a failure for the breaker.
            self._breaker.record_success()
            raise
        except BaseException:
            self._breaker.release_probe()
            raise
        OSRM_REQUESTS.labels(endpoint, "ok").inc()
        self._breaker.record_success()
        return body

    async def _request(
        self, endpoint: Endpoint, path: str, params: dict[str, str]
    ) -> dict[str, Any]:
        try:
            # httpx timeouts apply to each phase (connect, read...) separately;
            # asyncio.timeout caps the whole request, retry included.
            async with asyncio.timeout(config.OSRM_TIMEOUT_S):
                try:
                    response = await self._http.get(path, params=params)
                except httpx.ConnectError:
                    # Nothing reached the server, so one retry is safe and cheap (e.g. a
                    # container restart). Timeouts and server errors are not retried.
                    response = await self._http.get(path, params=params)
        except (TimeoutError, httpx.TimeoutException):
            raise _unavailable(endpoint, "timeout") from None
        except httpx.HTTPError as exc:
            raise _unavailable(endpoint, f"{type(exc).__name__}: {exc}") from exc

        if response.status_code >= 500:
            raise _unavailable(endpoint, f"HTTP {response.status_code}")
        # Errors like NoSegment come with HTTP 400 and a JSON body, so parse 4xx too.
        try:
            body = response.json()
        except ValueError:
            raise _unavailable(endpoint, f"invalid JSON (HTTP {response.status_code})") from None

        code = body.get("code") if isinstance(body, dict) else None
        if code == "Ok":
            return body
        if code == "NoSegment":
            raise NoRoute("no_road_nearby")
        if code == "NoRoute":
            raise NoRoute("no_route")
        raise _unavailable(endpoint, f"unexpected code {code!r} (HTTP {response.status_code})")


def _unavailable(endpoint: Endpoint, reason: str) -> OsrmUnavailable:
    logger.warning("OSRM %s unavailable: %s", endpoint, reason)
    return OsrmUnavailable(f"{endpoint}: {reason}")


def _coordinates(points: list[Point]) -> str:
    return ";".join(f"{p.lon},{p.lat}" for p in points)


def _radiuses(points: list[Point]) -> str:
    return ";".join(str(config.OSRM_SNAP_RADIUS_M) for _ in points)


def _to_point(lon_lat: list[float]) -> Point:
    lon, lat = lon_lat
    return Point(lat=lat, lon=lon)


def _with_factor(seconds: float) -> int:
    return round(seconds * config.OSRM_DURATION_FACTOR)
