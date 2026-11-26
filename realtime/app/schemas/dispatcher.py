"""WebSocket messages of the dispatcher map endpoint (/rt/ws/dispatcher).

The admin's page sends only auth and ping; everything else gets the same error codes
as on the driver endpoint (parse_with). The server pushes a full dispatcher_snapshot
every DISPATCHER_SNAPSHOT_INTERVAL_S, so a page that reconnects has the whole city at
once, without deltas to replay.

The snapshot already has its final shape (spec 12.7, widened by ТЗ 09): trips, surge,
searching and in_progress stay empty or zero until stages 3 and 6 fill them.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.schemas.ws import AuthMessage, PingMessage, parse_with
from app.services.drivers import DriverStatus

# ---------------------------------------------------------------------------
# Dispatcher -> server
# ---------------------------------------------------------------------------

DispatcherIncoming = Annotated[AuthMessage | PingMessage, Field(discriminator="type")]
_INCOMING = TypeAdapter(DispatcherIncoming)


def parse_dispatcher_message(raw: str | bytes) -> DispatcherIncoming:
    """Parse one message of the dispatcher endpoint or raise InvalidMessage."""
    return parse_with(_INCOMING, raw)


# ---------------------------------------------------------------------------
# Server -> dispatcher
# ---------------------------------------------------------------------------


class _Outgoing(BaseModel):
    # Frozen: one snapshot is shared by every page. No NaN or Infinity: they would go
    # out as null and break the map, so a bad value fails the build instead.
    model_config = ConfigDict(frozen=True, allow_inf_nan=False)


class MapDriver(_Outgoing):
    id: int
    lat: float
    lon: float
    # Turns the car icon; null while the car stands still.
    heading: float | None
    status: DriverStatus
    # A filter by car class on the map.
    tariff: str | None
    # False: the connection dropped and the reaper has not taken the driver offline
    # yet; the map greys the car out instead of freezing it.
    connected: bool


class DispatcherStats(_Outgoing):
    # In drivers:last_seen, with a position or not.
    online: int
    # Can be offered an order right now (in a GEO set), like drivers_online.
    available: int
    # Stage 3.
    searching: int = 0
    in_progress: int = 0


class DispatcherSnapshotData(_Outgoing):
    # When the snapshot was built, unix ms: the page interpolates between snapshots
    # and sees from it that they went stale (e.g. Redis is down).
    server_ms: int
    drivers: list[MapDriver]
    # Stage 3: trips in progress. Always empty for now.
    trips: tuple[()] = ()
    # Stage 6: surge multiplier per H3 cell. Always empty for now.
    surge: dict[str, float] = Field(default_factory=dict)
    stats: DispatcherStats


class DispatcherSnapshotMessage(_Outgoing):
    type: Literal["dispatcher_snapshot"] = "dispatcher_snapshot"
    data: DispatcherSnapshotData
