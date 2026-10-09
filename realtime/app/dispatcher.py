"""The dispatcher process: background work over the shared Redis state.

    python -m app.dispatcher

Today it runs the reaper; order dispatch and surge pricing join it later (spec 3.3).
It has its own Redis client and serves its Prometheus metrics on
DISPATCHER_METRICS_PORT. SIGTERM or SIGINT finishes the current pass and exits with
code 0. The handlers matter in Docker: the process is PID 1 there, and PID 1 ignores
signals it has no handler for, so `docker stop` would wait 10 s and SIGKILL it.
"""

import asyncio
import logging
import signal

from prometheus_client import start_http_server
from redis.asyncio import Redis

from app import config
from app.clock import Clock
from app.services.drivers import DriverStore
from app.services.reaper import Reaper, run_reaper

# Not __name__: run with `python -m`, that is "__main__", which says nothing in the logs.
logger = logging.getLogger("app.dispatcher")

_STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)


async def serve() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in _STOP_SIGNALS:
        loop.add_signal_handler(sig, stop.set)
    # socket_timeout: without it a hung (not down) Redis blocks a pass forever.
    redis = Redis.from_url(config.REDIS_URL, socket_connect_timeout=2, socket_timeout=2)
    try:
        logger.info("Dispatcher started")
        await run_reaper(Reaper(DriverStore(redis), Clock()), stop)
    finally:
        await redis.aclose()
        for sig in _STOP_SIGNALS:
            loop.remove_signal_handler(sig)
        logger.info("Dispatcher stopped")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    start_http_server(config.DISPATCHER_METRICS_PORT)
    asyncio.run(serve())


if __name__ == "__main__":
    main()
