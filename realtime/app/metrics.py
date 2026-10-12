# All Prometheus metrics of the realtime service, exported at /rt/metrics.

from enum import StrEnum

from prometheus_client import Counter, Gauge, Histogram

OSRM_REQUEST_DURATION = Histogram(
    "osrm_request_duration_seconds",
    "Time of OSRM requests that were actually sent (retry included).",
    ["endpoint"],
)

OSRM_REQUESTS = Counter(
    "osrm_requests_total",
    "OSRM calls by outcome; circuit_open means the call was refused without a request.",
    ["endpoint", "result"],
)

WS_CONNECTIONS = Gauge(
    "ws_connections",
    "Authenticated WebSocket connections open in this process.",
    ["role"],
)

WS_CLOSES = Counter(
    "ws_closes_total",
    "WebSocket connections closed by the server, by close code; role is unknown before auth.",
    ["role", "code"],
)


class LocationResult(StrEnum):
    ACCEPTED = "accepted"
    # Accepted (the driver is alive), but outside the service area: no orders there.
    OUT_OF_ZONE = "out_of_zone"
    THROTTLED = "throttled"
    REJECTED_INVALID = "rejected_invalid"
    REJECTED_STALE = "rejected_stale"
    REJECTED_OUT_OF_ORDER = "rejected_out_of_order"
    REJECTED_JUMP = "rejected_jump"
    REJECTED_OFFLINE = "rejected_offline"
    REJECTED_STALE_CONNECTION = "rejected_stale_connection"


LOCATION_UPDATES = Counter(
    "location_updates_total",
    "Driver locations by result; accepted and out_of_zone both reached Redis.",
    ["result"],
)
# Every result is exported from the start, at 0, so rates work before the first one.
for _result in LocationResult:
    LOCATION_UPDATES.labels(_result)

# Only points that reached the Lua script (any outcome): throttled and filtered points
# never touch Redis, and their near-zero times would drag the percentiles down.
LOCATION_PROCESSING = Histogram(
    "location_processing_seconds",
    "Time from a parsed location to the answer of its Lua script.",
    buckets=(0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)
