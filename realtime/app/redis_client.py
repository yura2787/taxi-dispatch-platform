"""The Redis client of the realtime processes (the gateway and the dispatcher).

The pool blocks: when every connection is busy, a command waits up to
REDIS_POOL_TIMEOUT_S for a free one. redis-py's default pool fails at once with
"Too many connections" instead, and a burst (1000 drivers going offline together,
everyone reconnecting after a gateway restart) closed hundreds of WebSockets with
1011 and left those drivers online until the reaper.
"""

from redis.asyncio import BlockingConnectionPool, Redis

from app import config


def create_redis() -> Redis:
    """A client with its own pool, configured from app.config at call time."""
    pool = BlockingConnectionPool.from_url(
        config.REDIS_URL,
        max_connections=config.REDIS_MAX_CONNECTIONS,
        timeout=config.REDIS_POOL_TIMEOUT_S,
        socket_connect_timeout=config.REDIS_SOCKET_TIMEOUT_S,
        socket_timeout=config.REDIS_SOCKET_TIMEOUT_S,
    )
    # from_pool: the client owns the pool and closes it in aclose().
    return Redis.from_pool(pool)
