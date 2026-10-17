"""What the realtime server saw during a run, from its Prometheus metrics.

The server does not acknowledge points, so the client cannot time them; the server
times each point that reached its Lua script (location_processing_seconds). The
runner takes a snapshot of /rt/metrics before and after the run, and the report shows
the difference: accepted and refused points, closes by code and the processing time
percentiles, estimated from the histogram buckets like Prometheus does.
"""

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, field

import httpx
from prometheus_client.parser import text_string_to_metric_families

from sim.stats import ms

logger = logging.getLogger(__name__)

# A failed fetch must not hold up the start or the summary of a run.
FETCH_TIMEOUT_S = 2

# One retry: right after 1000 bots went offline a single answer may come late.
FETCH_ATTEMPTS = 2

# Points that reached Redis; out_of_zone is accepted too (spec 11.1, W6).
ACCEPTED_RESULTS = ("accepted", "out_of_zone")


class ServerMetricsUnavailable(Exception):
    """No usable metrics: unreachable, broken, or reset by a restart during the run."""


@dataclass(frozen=True)
class Snapshot:
    # location_updates_total by result.
    updates: Mapping[str, float] = field(default_factory=dict)
    # ws_closes_total of drivers by close code.
    closes: Mapping[str, float] = field(default_factory=dict)
    # location_processing_seconds: cumulative count per upper bound, +Inf included.
    buckets: Mapping[float, float] = field(default_factory=dict)

    def minus(self, earlier: "Snapshot") -> "Snapshot":
        """What happened between the two snapshots.

        Counters only grow; one that went back means the server restarted in between
        (e.g. --reload in dev) and the difference says nothing.
        """
        for later_values, earlier_values in (
            (self.updates, earlier.updates),
            (self.closes, earlier.closes),
            (self.buckets, earlier.buckets),
        ):
            if any(later_values.get(key, 0) < value for key, value in earlier_values.items()):
                raise ServerMetricsUnavailable("the server restarted during the run")
        return Snapshot(
            updates=_minus(self.updates, earlier.updates),
            closes=_minus(self.closes, earlier.closes),
            buckets=_minus(self.buckets, earlier.buckets),
        )

    @property
    def accepted(self) -> float:
        return sum(self.updates.get(result, 0) for result in ACCEPTED_RESULTS)

    @property
    def refused_by_result(self) -> dict[str, float]:
        return {
            result: count
            for result, count in self.updates.items()
            if result.startswith("rejected") and count
        }

    @property
    def refused(self) -> float:
        return sum(self.refused_by_result.values())

    @property
    def throttled(self) -> float:
        """Points dropped for coming too often: not checked at all, so neither accepted
        nor refused (and not timed, see location_processing_seconds)."""
        return self.updates.get("throttled", 0)


def _minus[K](later: Mapping[K, float], earlier: Mapping[K, float]) -> dict[K, float]:
    return {key: value - earlier.get(key, 0) for key, value in later.items()}


def parse(text: str) -> Snapshot:
    updates: dict[str, float] = {}
    closes: dict[str, float] = {}
    buckets: dict[float, float] = {}
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            if sample.name == "location_updates_total":
                updates[sample.labels["result"]] = sample.value
            elif sample.name == "ws_closes_total" and sample.labels.get("role") == "driver":
                closes[sample.labels["code"]] = sample.value
            elif sample.name == "location_processing_seconds_bucket":
                buckets[float(sample.labels["le"])] = sample.value
    return Snapshot(updates=updates, closes=closes, buckets=buckets)


async def fetch(http: httpx.AsyncClient, url: str) -> Snapshot:
    """A snapshot of /rt/metrics; a failed request is tried once more, an answer that
    does not parse is not (it would not parse the second time either)."""
    try:
        response = await _get(http, url)
        return parse(response.text)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        raise ServerMetricsUnavailable(f"{type(exc).__name__}: {exc}") from exc


async def _get(http: httpx.AsyncClient, url: str) -> httpx.Response:
    for _ in range(FETCH_ATTEMPTS - 1):
        try:
            return await _get_once(http, url)
        except httpx.HTTPError as exc:
            logger.warning("server metrics: %s: %s, trying again", type(exc).__name__, exc)
    return await _get_once(http, url)


async def _get_once(http: httpx.AsyncClient, url: str) -> httpx.Response:
    response = await http.get(url, timeout=FETCH_TIMEOUT_S)
    response.raise_for_status()
    return response


def histogram_quantile(q: float, buckets: Mapping[float, float]) -> float | None:
    """The q-quantile (0..1) of a Prometheus histogram, as histogram_quantile() does:
    find the bucket that holds the q-th observation and interpolate linearly inside
    it. An estimate: it can be off by up to the width of that bucket. None if empty.
    """
    if not 0 <= q <= 1:
        raise ValueError(f"quantile must be within 0..1, got {q}")
    bounds = sorted(buckets)
    total = buckets.get(math.inf, 0)
    # Like Prometheus: no estimate without a finite bucket to interpolate in.
    if len(bounds) < 2 or total <= 0:
        return None
    rank = q * total
    lower, below = 0.0, 0.0
    # Cumulative counts never fall; two scrapes taken mid-update can make them look
    # like they do, so take the running maximum (Prometheus' ensureMonotonic).
    # The +Inf bucket holds every observation, so the loop always breaks.
    for upper in bounds:
        count = max(buckets[upper], below)
        # Empty buckets are skipped: the q=0 quantile is where observations start.
        if count >= rank and count > below:
            break
        lower, below = upper, count
    if math.isinf(upper):
        # Beyond the largest finite bucket nothing is known: its bound is the answer.
        return lower
    return lower + (upper - lower) * (rank - below) / (count - below)


def _quantile_text(q: float, buckets: Mapping[float, float]) -> str:
    """Beyond the largest finite bucket the estimate is only that bound: say ">"."""
    estimate = histogram_quantile(q, buckets)
    finite = [bound for bound in buckets if not math.isinf(bound)]
    if estimate is not None and finite:
        largest = max(finite)
        # Running maximum, as in histogram_quantile: a torn scrape may dip a bucket.
        if q * buckets.get(math.inf, 0) > max(buckets[bound] for bound in finite):
            return f">{ms(largest)}"
    return ms(estimate)


def report(change: Snapshot) -> list[str]:
    """The server part of the summary."""
    # Throttled points are shown next to the refused ones, outside the share.
    total = change.accepted + change.refused
    refused_share = change.refused / total if total else 0.0
    quantiles = " ".join(
        f"p{round(q * 100)}={_quantile_text(q, change.buckets)}" for q in (0.5, 0.95, 0.99)
    )
    updates = ", ".join(
        f"{result}={count:.0f}" for result, count in sorted(change.updates.items()) if count
    )
    closes = ", ".join(
        f"{code}={count:.0f}" for code, count in sorted(change.closes.items()) if count
    )
    return [
        "--- server (change of /rt/metrics over the run) ---",
        f"location updates: {updates or 'none'}",
        f"refused: {change.refused:.0f} of {total:.0f} ({refused_share:.3%}), "
        f"throttled: {change.throttled:.0f}",
        f"location_processing_seconds (bucket estimate): {quantiles}",
        f"closes of drivers by the server: {closes or 'none'}",
    ]
