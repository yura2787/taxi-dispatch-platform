import os
from typing import NamedTuple

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# --- Authentication ---
# Dev tokens ("dev:driver:42") carry no signature, so anyone could be any user.
# They are accepted only with DEV_AUTH=1, which only the dev docker-compose sets.
# Exactly "1": any other value, a typo included, leaves them off.
DEV_AUTH = os.environ.get("DEV_AUTH") == "1"

# --- OSRM (routes along Chernivtsi streets) ---
OSRM_URL = os.environ.get("OSRM_URL", "http://osrm:5000")

# Budget for one whole request (connect + response), not per network phase.
OSRM_TIMEOUT_S = 2

# OSRM knows speed limits but not traffic; real city trips take ~30% longer.
OSRM_DURATION_FACTOR = 1.3

# A point is snapped to the nearest road within this radius; farther away it is
# treated as "not near a road" instead of being dragged to some distant street.
OSRM_SNAP_RADIUS_M = 200

# osrm-routed's default --max-table-size: sources + destinations per /table request.
OSRM_TABLE_MAX_POINTS = 100

# Circuit breaker: after this many "unavailable" errors in a row, stop calling OSRM
# for OSRM_BREAKER_RESET_S seconds and fail fast, then let one probe request through.
OSRM_BREAKER_FAILURES = 5
OSRM_BREAKER_RESET_S = 30


# --- WebSocket ---
# The client must send {"type": "auth"} within this time after connecting (spec 12.1).
WS_AUTH_TIMEOUT_S = 5

# A connection with no message for this long is closed (4000). Clients ping every
# ~20 s; dead TCP connections are also dropped by uvicorn's protocol-level pings.
WS_IDLE_TIMEOUT_S = 45

# Any real message is well under 1 KB (a JWT included). Keep in sync with
# --ws-max-size in docker-compose.yml and the Dockerfile.
WS_MAX_MESSAGE_BYTES = 4096

# Token bucket per connection, for every message: a phone needs ~0.5/s (a point every
# 2-3 s plus pings), so 2/s with bursts of 10 never touches a sane client.
WS_MSG_RATE_PER_S = 2
WS_MSG_BURST = 10

# This many messages refused in a row is a broken or hostile client: close with 1008.
WS_RATE_LIMIT_CLOSE_AFTER = 20

# At most one rate_limited error per this interval: the answer to a flood must not
# become a flood itself.
WS_RATE_LIMIT_ERROR_INTERVAL_S = 1.0


# --- Driver locations ---
class BoundingBox(NamedTuple):
    min_lat: float
    min_lon: float
    max_lat: float
    max_lon: float


# Chernivtsi with its outskirts, edges included. A point outside is still accepted (the
# driver is alive), but the driver is not offered orders until they are back inside.
SERVICE_AREA = BoundingBox(min_lat=48.22, min_lon=25.80, max_lat=48.36, max_lon=26.08)

# Phones send a point every 2-3 s. More than one per second adds no information, only
# Redis writes, so extra points are dropped before any other check.
LOCATION_MIN_INTERVAL_S = 1.0

# A point that took this much longer to arrive than the connection's fastest point was
# recorded earlier (e.g. buffered in a tunnel) and no longer shows where the car is.
LOCATION_MAX_AGE_S = 10

# Faster than any car in the city: such a move between two points is GPS noise.
LOCATION_MAX_SPEED_KMH = 150

# After this long without an accepted point the previous one says nothing about the
# next, so the speed check is skipped. Must stay below DRIVER_SILENCE_S: a driver whose
# GPS really jumped has to recover before the reaper takes them offline.
LOCATION_JUMP_RESET_S = 15

# After this many points in a row rejected as stale or out of order, the connection's
# clock model is assumed wrong (the phone's clock was set back) and starts over.
LOCATION_CLOCK_RESET_AFTER = 5

# --- Drivers ---
# Car classes; each has its own GEO set of drivers who can be offered an order.
TARIFFS = ("economy", "comfort")

# A driver online without an accepted point for this long is taken offline by the reaper.
DRIVER_SILENCE_S = 30

# The state of an offline driver is kept this long for a quick return (last position),
# then expires so drivers who left for good do not pile up in Redis.
OFFLINE_STATE_TTL_S = 24 * 60 * 60

# --- Dispatcher process ---
# The reaper looks for silent drivers this often, so a driver is taken offline
# DRIVER_SILENCE_S to DRIVER_SILENCE_S + REAPER_INTERVAL_S after their last point.
REAPER_INTERVAL_S = 10

# Silent drivers fetched per Redis query; a pass repeats it until none are left, so a
# mass disconnect (e.g. a gateway restart) is cleared in one pass without one huge reply.
REAPER_BATCH = 500

# Prometheus metrics of the dispatcher. Not published outside the Docker network.
DISPATCHER_METRICS_PORT = 8002
