from contextlib import asynccontextmanager

import redis.asyncio as redis
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError

from app import config


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = redis.from_url(config.REDIS_URL, socket_connect_timeout=2)
    yield
    await app.state.redis.aclose()


app = FastAPI(title="RideFlow realtime", lifespan=lifespan)


@app.get("/rt/health")
async def health(request: Request):
    try:
        await request.app.state.redis.ping()
    except (RedisError, OSError):
        return JSONResponse({"status": "error", "redis": "down"}, status_code=503)
    return {"status": "ok", "redis": "up"}
