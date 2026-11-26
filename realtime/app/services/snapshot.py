"""Building the dispatcher_snapshot message from the live drivers.

A pure function: no Redis, no clock. The broadcaster reads the drivers, takes the
time, calls this and serializes the result once for every page that watches.
"""

from app.schemas.dispatcher import (
    DispatcherSnapshotData,
    DispatcherSnapshotMessage,
    DispatcherStats,
    MapDriver,
)
from app.services.drivers import LiveDrivers

# 6 decimal places is ~0.1 m: more only makes the JSON of 1000 drivers bigger.
COORDINATE_DIGITS = 6


def build_dispatcher_snapshot(live: LiveDrivers, server_ms: int) -> DispatcherSnapshotMessage:
    """The snapshot of the city at server_ms; drivers keep the order of `live` (by id)."""
    return DispatcherSnapshotMessage(
        data=DispatcherSnapshotData(
            server_ms=server_ms,
            drivers=[
                MapDriver(
                    id=driver.id,
                    lat=round(driver.lat, COORDINATE_DIGITS),
                    lon=round(driver.lon, COORDINATE_DIGITS),
                    heading=driver.heading,
                    status=driver.status,
                    tariff=driver.tariff,
                    connected=driver.connected,
                )
                for driver in live.drivers
            ],
            stats=DispatcherStats(online=live.online, available=live.available),
        )
    )
