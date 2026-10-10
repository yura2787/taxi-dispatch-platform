"""Driver state in Redis: profiles, live state and the Lua scripts that change it.

Every change of a driver's state is a Lua script (app/lua/), so it is atomic: no
race between a point, a disconnect, a second connection and the reaper can leave
the GEO sets and the state out of step. The scripts get every key through KEYS and
the current time through ARGV, so tests control both.

Values come back from Redis as bytes (the client does not decode responses) and
are turned into typed objects here, so the rest of the code never sees raw hashes.
"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from redis.asyncio import Redis
from redis.commands.core import AsyncScript

from app import config, redis_keys
from app.services.locations import AcceptedPoint

_LUA_DIR = Path(__file__).resolve().parent.parent / "lua"


class DriverStatus(StrEnum):
    OFFLINE = "offline"
    AVAILABLE = "available"
    OFFERED = "offered"
    ON_TRIP = "on_trip"


class Outcome(StrEnum):
    """What a script did; each script returns only some of these (see its header)."""

    OK = "ok"
    STALE_CONNECTION = "stale_connection"
    NOT_ONLINE = "not_online"
    NOT_ELIGIBLE = "not_eligible"
    BUSY = "busy"
    NOT_OWNER = "not_owner"
    REAPED = "reaped"
    ALIVE = "alive"
    GONE = "gone"


@dataclass(frozen=True, slots=True)
class DriverProfile:
    eligible: bool
    tariff: str
    name: str
    car: str
    color: str
    plate: str
    rating: float | None

    @property
    def may_go_online(self) -> bool:
        return self.eligible and self.tariff in config.TARIFFS


@dataclass(frozen=True, slots=True)
class Position:
    lat: float
    lon: float
    # None when the phone did not report it (e.g. heading while standing still).
    heading: float | None
    speed: float | None
    # Phone time of the point and server time it was accepted at, unix ms.
    ts: int
    updated_at: int

    def as_accepted_point(self) -> AcceptedPoint:
        return AcceptedPoint(lat=self.lat, lon=self.lon, ts=self.ts, server_ms=self.updated_at)


@dataclass(frozen=True, slots=True)
class DriverState:
    status: DriverStatus
    # None until the driver first goes online.
    tariff: str | None
    in_zone: bool
    # None until the first accepted point.
    position: Position | None
    conn_id: str | None


@dataclass(frozen=True, slots=True)
class DriverCounts:
    # Sending points: in drivers:last_seen.
    online: int
    # Can be offered an order right now: in a GEO set.
    available: int


@dataclass(frozen=True, slots=True)
class LocationUpdate:
    """A point that passed LocationFilter, with what Redis stores besides it."""

    point: AcceptedPoint
    heading: float | None
    speed: float | None
    in_zone: bool


class DriverStore:
    def __init__(self, redis: Redis):
        self._redis = redis
        # register_script sends nothing to Redis: a call runs EVALSHA and, if Redis
        # does not know the script yet (first call, restart, SCRIPT FLUSH), loads it.
        self._connect = redis.register_script(_lua("driver_connect"))
        self._go_online = redis.register_script(_lua("driver_go_online"))
        self._go_offline = redis.register_script(_lua("driver_go_offline"))
        self._location = redis.register_script(_lua("driver_location"))
        self._disconnect = redis.register_script(_lua("driver_disconnect"))
        self._reap = redis.register_script(_lua("driver_reap"))

    async def get_profile(self, driver_id: int) -> DriverProfile | None:
        fields = _decode(await self._redis.hgetall(redis_keys.driver_profile(driver_id)))
        if not fields:
            return None
        rating = fields.get("rating")
        return DriverProfile(
            eligible=fields.get("eligible") == "1",
            tariff=fields.get("tariff", ""),
            name=fields.get("name", ""),
            car=fields.get("car", ""),
            color=fields.get("color", ""),
            plate=fields.get("plate", ""),
            rating=float(rating) if rating else None,
        )

    async def get_state(self, driver_id: int) -> DriverState:
        return _parse_state(_decode(await self._redis.hgetall(redis_keys.driver_state(driver_id))))

    async def connect(self, driver_id: int, conn_id: str) -> DriverState:
        """Bind the driver to this connection; older connections become stale."""
        flat = await self._connect(
            keys=[redis_keys.driver_state(driver_id)],
            args=[conn_id, config.OFFLINE_STATE_TTL_S],
        )
        return _parse_state(_decode(dict(zip(flat[::2], flat[1::2], strict=True))))

    async def go_online(self, driver_id: int, conn_id: str, now_ms: int) -> Outcome:
        return await self._run(
            self._go_online,
            keys=[
                redis_keys.driver_state(driver_id),
                redis_keys.driver_profile(driver_id),
                redis_keys.drivers_last_seen(),
            ],
            args=[driver_id, conn_id, now_ms, *config.TARIFFS],
        )

    async def go_offline(self, driver_id: int, conn_id: str) -> Outcome:
        return await self._run(
            self._go_offline,
            keys=[
                redis_keys.driver_state(driver_id),
                redis_keys.drivers_last_seen(),
                *_geo_keys(),
            ],
            args=[driver_id, conn_id, config.OFFLINE_STATE_TTL_S],
        )

    async def update_location(
        self, driver_id: int, conn_id: str, update: LocationUpdate
    ) -> Outcome:
        point = update.point
        return await self._run(
            self._location,
            keys=[
                redis_keys.driver_state(driver_id),
                redis_keys.drivers_last_seen(),
                *_geo_keys(),
            ],
            args=[
                driver_id,
                conn_id,
                point.server_ms,
                point.lat,
                point.lon,
                _optional(update.heading),
                _optional(update.speed),
                point.ts,
                int(update.in_zone),
                *config.TARIFFS,
            ],
        )

    async def disconnect(self, driver_id: int, conn_id: str) -> Outcome:
        return await self._run(
            self._disconnect,
            keys=[redis_keys.driver_state(driver_id), *_geo_keys()],
            args=[driver_id, conn_id],
        )

    async def reap(self, driver_id: int, cutoff_ms: int) -> Outcome:
        """Take the driver offline if their last accepted point is older than cutoff_ms."""
        return await self._run(
            self._reap,
            keys=[
                redis_keys.driver_state(driver_id),
                redis_keys.drivers_last_seen(),
                *_geo_keys(),
            ],
            args=[driver_id, cutoff_ms, config.OFFLINE_STATE_TTL_S],
        )

    async def silent_drivers(self, cutoff_ms: int, limit: int) -> list[int]:
        """Online drivers whose last accepted point is older than cutoff_ms, oldest first."""
        members = await self._redis.zrange(
            redis_keys.drivers_last_seen(),
            "-inf",
            f"({cutoff_ms}",  # "(" excludes the cutoff itself, as reap() does
            byscore=True,
            offset=0,
            num=limit,
        )
        return [int(member) for member in members]

    async def count_drivers(self) -> DriverCounts:
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.zcard(redis_keys.drivers_last_seen())
            for key in _geo_keys():
                pipe.zcard(key)
            online, *available = await pipe.execute()
        return DriverCounts(online=online, available=sum(available))

    @staticmethod
    async def _run(script: AsyncScript, *, keys: list[str], args: list) -> Outcome:
        return Outcome((await script(keys=keys, args=args)).decode())


def _lua(name: str) -> str:
    return (_LUA_DIR / f"{name}.lua").read_text()


def _geo_keys() -> list[str]:
    # The same order as config.TARIFFS: the scripts pair the keys with the names.
    return [redis_keys.drivers_geo(tariff) for tariff in config.TARIFFS]


def _optional(value: float | None) -> float | str:
    # Lua gets no nil from ARGV; an empty string stands for "not reported".
    return "" if value is None else value


def _decode(raw: dict[bytes, bytes]) -> dict[str, str]:
    return {key.decode(): value.decode() for key, value in raw.items()}


def _parse_state(fields: dict[str, str]) -> DriverState:
    position = None
    if "lat" in fields:
        heading, speed = fields.get("heading"), fields.get("speed")
        position = Position(
            lat=float(fields["lat"]),
            lon=float(fields["lon"]),
            heading=float(heading) if heading is not None else None,
            speed=float(speed) if speed is not None else None,
            ts=int(fields["ts"]),
            updated_at=int(fields["updated_at"]),
        )
    return DriverState(
        # No state at all, or one without a status, is an offline driver.
        status=DriverStatus(fields.get("status", DriverStatus.OFFLINE)),
        tariff=fields.get("tariff"),
        in_zone=fields.get("in_zone") == "1",
        position=position,
        conn_id=fields.get("conn_id"),
    )
