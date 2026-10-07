"""Validation of driver locations: is a point fresh, plausible and inside the city.

Pure functions (service area, distance) and LocationFilter, the per-connection state
that decides whether a point goes on to Redis. Everything here works with {lat, lon};
the "lon, lat" order Redis GEO wants is built only in the Redis access layer.
"""

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app import config
from app.clock import Clock

# Mean Earth radius; the error against the ellipsoid (~0.3%) is irrelevant for a
# speed limit.
EARTH_RADIUS_M = 6_371_000


class LatLon(Protocol):
    @property
    def lat(self) -> float: ...

    @property
    def lon(self) -> float: ...


def in_service_area(point: LatLon) -> bool:
    area = config.SERVICE_AREA
    return area.min_lat <= point.lat <= area.max_lat and area.min_lon <= point.lon <= area.max_lon


def haversine_m(a: LatLon, b: LatLon) -> float:
    """Great-circle distance in metres."""
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    d_lat = lat2 - lat1
    d_lon = math.radians(b.lon - a.lon)
    h = math.sin(d_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(d_lon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def speed_kmh(a: LatLon, b: LatLon, elapsed_ms: int) -> float:
    """Average speed between two points; elapsed_ms must be positive."""
    return haversine_m(a, b) / (elapsed_ms / 1000) * 3.6


@dataclass(frozen=True, slots=True)
class Location:
    """A point as the phone reported it."""

    lat: float
    lon: float
    # Phone's wall clock, unix ms; it may be minutes off from the server's.
    ts: int


@dataclass(frozen=True, slots=True)
class AcceptedPoint:
    """The last point that made it into Redis."""

    lat: float
    lon: float
    # Phone time of the point.
    ts: int
    # Server time it was accepted at (updated_at in Redis).
    server_ms: int


class Verdict(StrEnum):
    # Passed every check here; Redis may still refuse it (e.g. the driver is offline).
    PASSED = "passed"
    THROTTLED = "throttled"
    OUT_OF_ORDER = "out_of_order"
    STALE = "stale"
    JUMP = "jump"


@dataclass(frozen=True, slots=True)
class Decision:
    verdict: Verdict
    # The fields below are set only for PASSED.
    in_zone: bool = False
    # What the connection remembers once Redis accepts the point (LocationFilter.commit).
    point: AcceptedPoint | None = None
    min_skew_ms: int | None = None


class LocationFilter:
    """Throttle, clock-skew model and GPS jump filter of one driver connection.

    check() decides on a point without changing what the connection knows about the
    driver; commit() records the point once Redis has accepted it. A point refused by
    Redis therefore never becomes the reference for the next one. Only the throttle
    and the run of clock rejections are updated by check() itself: they are about
    attempts, not about accepted points.

    Clock-skew model. A phone's clock can be minutes off, so a point's ts is never
    compared with the server time directly. skew = server time - ts is the clock offset
    plus the delivery delay; its minimum over the connection is the offset plus the
    best delivery seen. A point whose skew exceeds that minimum by more than
    LOCATION_MAX_AGE_S spent that much longer on its way: it was buffered and is stale.

    Known limitation: min_skew is not carried over between connections, so a buffered
    point sent first on a new connection has nothing to be compared with and passes as
    fresh, as long as it is newer than the last accepted point. Keeping the skew across
    connections would be worse: after the phone's clock is corrected, a stored skew
    would reject every fresh point until the driver gave up.
    """

    def __init__(self, clock: Clock, last: AcceptedPoint | None = None):
        # `last` comes from Redis on connect, so out-of-order and jump checks work from
        # the first point; there is never a second connection of the same driver.
        self._clock = clock
        self._last = last
        self._min_skew_ms: int | None = None
        self._throttle_passed_at: float | None = None
        self._clock_rejections = 0

    def check(self, location: Location) -> Decision:
        now = self._clock.monotonic()
        if (
            self._throttle_passed_at is not None
            and now - self._throttle_passed_at < config.LOCATION_MIN_INTERVAL_S
        ):
            return Decision(Verdict.THROTTLED)
        self._throttle_passed_at = now

        now_ms = self._clock.now_ms()
        last, min_skew_ms = self._last, self._min_skew_ms
        rejection = _clock_rejection(location, now_ms, last, min_skew_ms)
        if rejection is not None:
            if self._clock_rejections < config.LOCATION_CLOCK_RESET_AFTER:
                self._clock_rejections += 1
                return Decision(rejection)
            # Too many in a row: the phone's clock moved, not the points. Start over as
            # if this were the first point, without the previous one: its ts belongs to
            # the old clock, so a speed computed from it would be meaningless. The same
            # idea as LOCATION_JUMP_RESET_S: a pause or a run of rejections means the
            # old assumptions no longer hold. It lets 5 bad points bypass the jump
            # filter, which is fine: the filter is against GPS noise, not fraud.
            last, min_skew_ms = None, None
        self._clock_rejections = 0

        if last is not None and now_ms - last.server_ms <= config.LOCATION_JUMP_RESET_S * 1000:
            # ts > last.ts here: _clock_rejection rejects anything else.
            if speed_kmh(last, location, location.ts - last.ts) > config.LOCATION_MAX_SPEED_KMH:
                return Decision(Verdict.JUMP)

        skew_ms = now_ms - location.ts
        return Decision(
            Verdict.PASSED,
            in_zone=in_service_area(location),
            point=AcceptedPoint(location.lat, location.lon, location.ts, now_ms),
            min_skew_ms=skew_ms if min_skew_ms is None else min(min_skew_ms, skew_ms),
        )

    def commit(self, decision: Decision) -> None:
        """Remember a point Redis has accepted."""
        if decision.verdict is not Verdict.PASSED:
            raise ValueError(f"only a passed point can be committed, got {decision.verdict}")
        self._last = decision.point
        self._min_skew_ms = decision.min_skew_ms


def _clock_rejection(
    location: Location, now_ms: int, last: AcceptedPoint | None, min_skew_ms: int | None
) -> Verdict | None:
    if last is not None and location.ts <= last.ts:
        return Verdict.OUT_OF_ORDER
    skew_ms = now_ms - location.ts
    if min_skew_ms is not None and skew_ms - min_skew_ms > config.LOCATION_MAX_AGE_S * 1000:
        return Verdict.STALE
    return None
