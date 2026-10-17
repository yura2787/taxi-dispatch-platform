"""Numbers of a run: percentiles, the status line every REPORT_INTERVAL_S and the
summary at the end.

Percentiles of the client are taken from the full samples, never from averages:
1000 bots x 10 min / 20 s is 30 thousand RTTs, which fit in memory easily. The
samples are kept for the whole run, so a run of many hours grows them without bound
(and every status line sorts them): runs are meant to last minutes.
"""

import os
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, fields
from datetime import datetime

from sim.driver_bot import AVAILABLE, BotStats, DriverBot


def percentile(values: Sequence[float], q: float) -> float | None:
    """The q-th percentile (0..100) with linear interpolation between the closest
    samples ("inclusive", as numpy's default); None for no samples."""
    if not 0 <= q <= 100:
        raise ValueError(f"percentile must be within 0..100, got {q}")
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q / 100
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def ms(seconds: float | None) -> str:
    return "n/a" if seconds is None else f"{seconds * 1000:.1f}ms"


@dataclass
class Totals:
    """BotStats of all bots added up."""

    locations_sent: int = 0
    errors: Counter[str] = field(default_factory=Counter)
    closes: Counter[int] = field(default_factory=Counter)
    connect_failures: int = 0
    handshake_timeouts: int = 0
    reconnects: int = 0
    unknown_messages: int = 0
    pongs_missed: int = 0
    breaks: int = 0
    drops: int = 0
    silences: int = 0
    left_online: int = 0
    osrm_failures: int = 0
    rtt_s: list[float] = field(default_factory=list)
    # Stopped before their start in the ramp-up: never even tried to connect.
    never_started: int = 0
    never_online: int = 0
    stopped: Counter[str] = field(default_factory=Counter)

    @classmethod
    def of(cls, bots: Iterable[DriverBot]) -> "Totals":
        totals = cls()
        for bot in bots:
            totals._add(bot.stats)
            if bot.stop_reason is not None:
                totals.stopped[bot.stop_reason] += 1
        return totals

    def _add(self, stats: BotStats) -> None:
        # Field by field, so a new counter in BotStats needs only its line here
        # in Totals; the flags are summed up into never_started and never_online.
        for item in fields(BotStats):
            value = getattr(stats, item.name)
            if isinstance(value, bool):
                continue
            total = getattr(self, item.name)
            if isinstance(value, Counter):
                total.update(value)
            elif isinstance(value, list):
                total.extend(value)
            else:
                setattr(self, item.name, total + value)
        self.never_started += not stats.started
        self.never_online += stats.started and not stats.went_online


class StatusLine:
    """The INFO line of every REPORT_INTERVAL_S; sent/s is over the last interval."""

    def __init__(self) -> None:
        self._sent = 0
        self._at: float | None = None

    def render(self, bots: Sequence[DriverBot], loop_lag_s: Sequence[float], now: float) -> str:
        totals = Totals.of(bots)
        rate = 0.0
        if self._at is not None and now > self._at:
            rate = (totals.locations_sent - self._sent) / (now - self._at)
        self._sent, self._at = totals.locations_sent, now
        online = sum(bot.connected and bot.status == AVAILABLE for bot in bots)
        return (
            f"bots={len(bots)} connected={sum(bot.connected for bot in bots)} online={online} "
            f"on_break={sum(bot.on_break for bot in bots)} stopped={sum(totals.stopped.values())} "
            f"sent/s={rate:.1f} errors={sum(totals.errors.values())} "
            f"reconnects={totals.reconnects} rtt_p95={ms(percentile(totals.rtt_s, 95))} "
            f"loop_lag_p99={ms(percentile(loop_lag_s, 99))}"
        )


@dataclass(frozen=True)
class Conditions:
    """What the numbers were measured under; printed first in the summary."""

    drivers: int
    duration_s: float | None
    ramp_up_s: float
    flaky_share: float
    seed: int
    url: str
    started_at: datetime
    finished_at: datetime
    cpus: int | None = field(default_factory=os.cpu_count)


def _counts(counter: Counter) -> str:
    if not counter:
        return "none"
    return ", ".join(f"{key}={count}" for key, count in sorted(counter.items(), key=str))


def _percentiles(values: Sequence[float], qs: Iterable[float]) -> str:
    parts = [f"p{q:g}={ms(percentile(values, q))}" for q in qs]
    return " ".join([*parts, f"max={ms(max(values, default=None))}", f"n={len(values)}"])


def summary(
    conditions: Conditions,
    bots: Sequence[DriverBot],
    loop_lag_s: Sequence[float],
    *,
    cancelled: int = 0,
) -> list[str]:
    """The report at the end of a run, line by line."""
    totals = Totals.of(bots)
    elapsed = (conditions.finished_at - conditions.started_at).total_seconds()
    per_second = totals.locations_sent / elapsed if elapsed > 0 else 0.0
    duration = "until stopped" if conditions.duration_s is None else f"{conditions.duration_s:g}s"
    return [
        "--- run ---",
        f"bots: {conditions.drivers}, duration: {duration}, ramp-up: "
        f"{conditions.ramp_up_s:g}s, flaky share: {conditions.flaky_share:g}, "
        f"seed: {conditions.seed}",
        f"server: {conditions.url}, CPUs: {conditions.cpus}",
        f"from {conditions.started_at:%Y-%m-%d %H:%M:%S} to "
        f"{conditions.finished_at:%Y-%m-%d %H:%M:%S} UTC ({elapsed:.0f}s)",
        "--- client ---",
        # Over the whole run, the ramp-up and the stop included.
        f"points sent: {totals.locations_sent} ({per_second:.1f}/s)",
        f"ping->pong RTT: {_percentiles(totals.rtt_s, (50, 95, 99))}, "
        f"pongs missed: {totals.pongs_missed}",
        f"event loop lag: {_percentiles(loop_lag_s, (50, 99))}",
        f"server errors: {_counts(totals.errors)}",
        f"closes by the server: {_counts(totals.closes)}",
        f"reconnects: {totals.reconnects}, failed connects: {totals.connect_failures}, "
        f"handshake timeouts: {totals.handshake_timeouts}, "
        f"unknown messages: {totals.unknown_messages}",
        f"breaks: {totals.breaks}, drops: {totals.drops}, silences: {totals.silences}, "
        f"OSRM failures: {totals.osrm_failures}",
        f"never started: {totals.never_started}, never online: {totals.never_online}, "
        f"stopped bots: {_counts(totals.stopped)}, "
        f"cancelled at the stop: {cancelled}, left online at the stop: {totals.left_online}",
    ]
