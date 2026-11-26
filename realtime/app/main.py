import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.exceptions import RedisError

from app import config
from app.auth import warn_if_dev_auth
from app.clock import Clock
from app.redis_client import create_redis
from app.services.drivers import DriverStore
from app.services.osrm import CITY_CENTRE, NoRoute, OsrmClient, OsrmUnavailable
from app.ws.broadcaster import SnapshotBroadcaster
from app.ws.driver import router as driver_ws_router
from app.ws.manager import ConnectionRegistry

# Uvicorn configures only its own loggers; without this, INFO from app modules is lost.
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)
# httpx logs every request at INFO; OSRM calls are counted in metrics instead.
logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Every client is closed on the way out, also when the app stops with an exception
    # or startup fails halfway: only what was created, in reverse order, and a close
    # that fails does not skip the ones after it.
    async with AsyncExitStack() as cleanup:
        app.state.redis = create_redis()
        cleanup.push_async_callback(app.state.redis.aclose)
        # The HTTP client is what holds connections: it is closed even if wrapping it fails.
        osrm_http = httpx.AsyncClient(base_url=config.OSRM_URL)
        cleanup.push_async_callback(osrm_http.aclose)
        app.state.osrm = OsrmClient(osrm_http)
        app.state.drivers = DriverStore(app.state.redis)
        app.state.driver_connections = ConnectionRegistry()
        app.state.clock = Clock()
        app.state.snapshots = SnapshotBroadcaster(app.state.drivers, app.state.clock)
        # Registered after the clients, so it stops before they close (reverse order):
        # a tick never runs against a closed Redis.
        stop_snapshots = asyncio.Event()
        snapshot_loop = asyncio.create_task(
            app.state.snapshots.run(stop_snapshots), name="dispatcher snapshots"
        )
        snapshot_loop.add_done_callback(_log_crash)
        cleanup.push_async_callback(_stop_task, snapshot_loop, stop_snapshots)
        warn_if_dev_auth()
        yield


def _log_crash(task: asyncio.Task) -> None:
    """A background loop that died must not go unnoticed: its pages would just freeze."""
    if not task.cancelled() and (exc := task.exception()) is not None:
        logger.error("Background task %s crashed", task.get_name(), exc_info=exc)


async def _stop_task(task: asyncio.Task, stop: asyncio.Event) -> None:
    """Ask a background loop to stop, wait DISPATCHER_STOP_TIMEOUT_S, then cancel it.

    A loop that crashed earlier was already logged by _log_crash; its error is not
    raised again here, so the rest of the shutdown goes on.
    """
    stop.set()
    try:
        done, _ = await asyncio.wait({task}, timeout=config.DISPATCHER_STOP_TIMEOUT_S)
    except asyncio.CancelledError:
        # The shutdown itself was cancelled: the loop must not outlive the clients.
        task.cancel()
        raise
    if task not in done:
        logger.warning("Background task %s did not stop in time; cancelling it", task.get_name())
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


app = FastAPI(title="RideFlow realtime", lifespan=lifespan)
app.include_router(driver_ws_router)


async def _redis_status(request: Request) -> str:
    try:
        await request.app.state.redis.ping()
    except (RedisError, OSError):
        return "down"
    return "up"


async def _osrm_status(request: Request) -> str:
    try:
        await request.app.state.osrm.nearest(CITY_CENTRE)
    except NoRoute:
        pass  # OSRM answered, so it is up.
    except OsrmUnavailable:
        return "down"
    return "up"


# Only Redis is critical: without OSRM the service still works, with degraded routing.
@app.get("/rt/health")
async def health(request: Request):
    redis_status, osrm_status = await asyncio.gather(_redis_status(request), _osrm_status(request))
    if redis_status == "down":
        return JSONResponse(
            {"status": "error", "redis": redis_status, "osrm": osrm_status}, status_code=503
        )
    return {"status": "ok", "redis": redis_status, "osrm": osrm_status}


# Closed to the outside in Nginx; Prometheus scrapes realtime:8001 directly.
@app.get("/rt/metrics", include_in_schema=False)
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
