"""Reaper: takes offline the drivers who stopped sending points.

A driver whose phone died or lost the network never says goodbye. Each pass finds
the drivers silent for longer than DRIVER_SILENCE_S and runs driver_reap for each;
the script checks the silence again, atomically, so a point that arrives between the
listing and the script keeps the driver online. That also makes it safe to run
several dispatchers at once: a driver is reaped by exactly one of them, the others
get "gone".

Known limitation: last_seen is written with the gateway's clock and the cutoff is
computed with the dispatcher's; on separate hosts both need NTP.
"""

import asyncio
import logging

from app import config
from app.clock import Clock
from app.dispatcher_metrics import DRIVERS_ONLINE, REAPER_ITERATION, REAPER_REAPED
from app.services.drivers import DriverStore, Outcome

logger = logging.getLogger(__name__)


class Reaper:
    def __init__(self, store: DriverStore, clock: Clock):
        self._store = store
        self._clock = clock

    async def run_once(self) -> int:
        """One pass: reap every silent driver, then update drivers_online.

        Returns how many drivers were taken offline.
        """
        with REAPER_ITERATION.time():
            cutoff_ms = self._clock.now_ms() - config.DRIVER_SILENCE_S * 1000
            reaped = 0
            # Every listed driver leaves drivers:last_seen or moves past the cutoff
            # (a fresh point), so each batch is new and the loop ends.
            while True:
                silent = await self._store.silent_drivers(cutoff_ms, config.REAPER_BATCH)
                for driver_id in silent:
                    if await self._store.reap(driver_id, cutoff_ms) is Outcome.REAPED:
                        reaped += 1
                        logger.info(
                            "Driver %d reaped: silent for over %ds",
                            driver_id,
                            config.DRIVER_SILENCE_S,
                        )
                if len(silent) < config.REAPER_BATCH:
                    break
            counts = await self._store.count_drivers()
        REAPER_REAPED.inc(reaped)
        DRIVERS_ONLINE.labels("online").set(counts.online)
        DRIVERS_ONLINE.labels("available").set(counts.available)
        return reaped


async def run_reaper(reaper: Reaper, stop: asyncio.Event) -> None:
    """Run reaper passes every REAPER_INTERVAL_S until `stop` is set.

    A failed pass (e.g. Redis restarting) is logged and the loop goes on: the next
    pass catches up on everything this one missed.
    """
    while not stop.is_set():
        try:
            await reaper.run_once()
        except Exception:
            logger.warning("Reaper pass failed", exc_info=True)
        try:
            async with asyncio.timeout(config.REAPER_INTERVAL_S):
                await stop.wait()
        except TimeoutError:
            pass
