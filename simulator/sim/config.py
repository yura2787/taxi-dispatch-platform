"""Settings of the simulator: addresses from the environment, the rest are constants.

Values that must match the server are copies, marked "keep in sync with
realtime/app/config.py": the simulator is an external client and imports nothing
from realtime.
"""

import os

from sim.geo import BoundingBox

# --- Addresses ---
# Bots connect through nginx, like a real phone, not straight to realtime.
REALTIME_WS_URL = os.environ.get("REALTIME_WS_URL", "ws://nginx/rt/ws/driver")

OSRM_URL = os.environ.get("OSRM_URL", "http://osrm:5000")

# From inside the Docker network: nginx answers 403 for /rt/metrics.
REALTIME_METRICS_URL = os.environ.get("REALTIME_METRICS_URL", "http://realtime:8001/rt/metrics")


# --- Areas ---
# Keep in sync with realtime/app/config.py. Routes that leave it are not used.
SERVICE_AREA = BoundingBox(min_lat=48.22, min_lon=25.80, max_lat=48.36, max_lon=26.08)

# Where route destinations are picked: the city with Mahala in the east and the
# northern half of Koroviia in the south. Checked against road density (OSRM
# nearest): fields and forest lie outside. At least 1 km inside SERVICE_AREA on
# every side, so a route between two of its points rarely leaves the service area.
CITY_AREA = BoundingBox(min_lat=48.23, min_lon=25.87, max_lat=48.325, max_lon=26.065)


# --- Routes (OSRM) ---
# Budget for one whole request, as in realtime: a bot without a route just waits.
OSRM_TIMEOUT_S = 2

# Retry delay while OSRM is unavailable: 1, 2, 4 ... seconds, at most this.
OSRM_RETRY_MAX_S = 30

# A destination closer than this (in a straight line) makes a "trip" of one block.
ROUTE_MIN_DISTANCE_M = 1000

# Destinations tried before a route that leaves SERVICE_AREA is taken anyway (the
# server accepts points outside it, it only offers no orders there).
ROUTE_ATTEMPTS = 5


# --- Driving ---
# Speed is drawn per route and kept along it: city traffic, well below the
# server's jump filter (SERVER_MAX_SPEED_KMH).
SPEED_KMH = (25, 45)

# A real phone sends a point every 2-3 s (spec 17.2).
LOCATION_INTERVAL_S = 2.0

# Application ping: keeps the connection alive while no points are sent (a break)
# and gives the RTT samples of the report (spec 12.1).
PING_INTERVAL_S = 20

# Standing at the end of a route (a passenger gets out), still sending points.
ROUTE_END_PAUSE_S = (0, 20)


# --- Breaks and flaky bots ---
# Every 5-20 minutes of driving a bot goes offline for 1-5 minutes.
BREAK_EVERY_S = (300, 1200)
BREAK_DURATION_S = (60, 300)

# How often a flaky bot drops its connection or falls silent.
FLAKY_EVERY_S = (120, 600)

# A dropped bot is away this long; below 30 s the server returns it to GEO with
# its first point, above it the reaper has taken it offline.
DROP_DURATION_S = (5, 60)

# A silent bot sends no points this long: longer than the reaper can wait
# (SERVER_DRIVER_SILENCE_S + SERVER_REAPER_INTERVAL_S), so it is always taken offline.
SILENCE_S = 50


# --- Connection ---
# Reconnect delay: random(0, min(RECONNECT_MAX_S, 2 ** attempt)), "full jitter".
RECONNECT_MAX_S = 30

# Opening the WebSocket, and separately waiting for state_snapshot after auth
# (the server itself gives auth WS_AUTH_TIMEOUT_S = 5 s).
CONNECT_TIMEOUT_S = 5

# On stop, an online bot waits this long for the snapshot after go_offline.
SHUTDOWN_TIMEOUT_S = 5


# --- Reporting ---
# A status line in the log this often.
REPORT_INTERVAL_S = 10

# The event loop lag probe sleeps this long and records how late it woke up.
LOOP_LAG_PROBE_S = 0.1


# --- Server limits (keep in sync with realtime/app/config.py) ---
# Only tests use them: they check that the bots stay within the server's limits.
SERVER_LOCATION_MIN_INTERVAL_S = 1.0
SERVER_WS_IDLE_TIMEOUT_S = 45
SERVER_WS_MSG_RATE_PER_S = 2
SERVER_MAX_SPEED_KMH = 150
SERVER_DRIVER_SILENCE_S = 30
SERVER_REAPER_INTERVAL_S = 10
