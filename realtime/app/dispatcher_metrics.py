"""Prometheus metrics of the dispatcher process, served on DISPATCHER_METRICS_PORT.

Kept apart from app/metrics.py: prometheus_client registers a metric when its module
is imported, so in metrics.py the gateway would export these too, as zeros.
"""

from prometheus_client import Counter, Gauge, Histogram

DRIVERS_ONLINE = Gauge(
    "drivers_online",
    "Drivers by status: online = sending points (drivers:last_seen); "
    "available = can be offered an order right now (in a drivers:geo set). "
    "Every dispatcher exports the same values: aggregate with max, not sum.",
    ["status"],
)

REAPER_REAPED = Counter(
    "reaper_reaped",
    "Silent drivers taken offline by the reaper.",
)

REAPER_ITERATION = Histogram(
    "reaper_iteration_seconds",
    "Time of one reaper pass over all silent drivers.",
)
