# All Prometheus metrics of the realtime service, exported at /rt/metrics.

from prometheus_client import Counter, Histogram

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
