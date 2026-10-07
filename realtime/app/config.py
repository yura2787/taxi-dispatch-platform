import os

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
