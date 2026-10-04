import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
import redis.asyncio as redis
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.exceptions import RedisError

from app import config
from app.services.osrm import CITY_CENTRE, NoRoute, OsrmClient, OsrmUnavailable

# Uvicorn configures only its own loggers; without this, INFO from app modules is lost.
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
# httpx logs every request at INFO; OSRM calls are counted in metrics instead.
logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # socket_timeout: without it a hung (not down) Redis blocks requests forever.
    app.state.redis = redis.from_url(config.REDIS_URL, socket_connect_timeout=2, socket_timeout=2)
    app.state.osrm = OsrmClient(httpx.AsyncClient(base_url=config.OSRM_URL))
    yield
    await app.state.osrm.aclose()
    await app.state.redis.aclose()


app = FastAPI(title="RideFlow realtime", lifespan=lifespan)


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
