"""Runs N driver bots in one asyncio process and reports on them.

Starts are spread over the ramp-up (the bots' ticks are spread by their own random
phase); a status line goes to the log every REPORT_INTERVAL_S and a probe measures
how late the event loop wakes up. The run ends after --duration or on a stop signal:
every bot goes offline and closes, the ones that do not manage it in time are
cancelled, and the summary is built.
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sim import config, protocol, server_metrics
from sim.clock import Clock
from sim.driver_bot import Connect, DriverBot
from sim.osrm import OsrmClient
from sim.protocol import ErrorVerdict
from sim.server_metrics import ServerMetricsUnavailable, Snapshot
from sim.stats import Conditions, StatusLine, Totals, summary

logger = logging.getLogger("sim.runner")

# Each bot gets its own connect() (tests give each its own fake server).
ConnectFactory = Callable[[int], Connect]

# A fresh snapshot of the server's metrics, or ServerMetricsUnavailable.
MetricsSource = Callable[[], Awaitable[Snapshot]]

# Errors from the server that mean the bots did something wrong (--strict).
_BOT_FAULTS = {ErrorVerdict.REJECTED_LOCATION, ErrorVerdict.BUG}


@dataclass(frozen=True)
class RunOptions:
    drivers: int = 100
    first_id: int = 1
    duration_s: float | None = None
    ramp_up_s: float = 10
    flaky_share: float = 0.05
    seed: int = 0
    strict: bool = False

    @property
    def driver_ids(self) -> range:
        return range(self.first_id, self.first_id + self.drivers)


@dataclass
class RunResult:
    bots: list[DriverBot]
    loop_lag_s: list[float]
    report: list[str]
    # What --strict fails on; empty for a clean run.
    failures: list[str]
    # Bots that did not go offline in time and were cancelled.
    cancelled: int = 0


def bot_rng(seed: int, driver_id: int) -> random.Random:
    """Bot N makes the same choices whatever the number of bots."""
    return random.Random(f"{seed}:{driver_id}")


def flaky_ids(options: RunOptions) -> set[int]:
    """Which bots misbehave: picked by the seed, so a rerun picks the same ones."""
    count = round(options.flaky_share * options.drivers)
    return set(random.Random(f"{options.seed}:flaky").sample(options.driver_ids, count))


async def run(
    options: RunOptions,
    *,
    connect_for: ConnectFactory,
    osrm: OsrmClient,
    clock: Clock,
    stop: asyncio.Event,
    url: str,
    server_metrics_source: MetricsSource | None = None,
) -> RunResult:
    before = await _snapshot(server_metrics_source)
    started_at = datetime.now(UTC)
    flaky = flaky_ids(options)
    bots = [
        DriverBot(
            driver_id,
            connect=connect_for(driver_id),
            osrm=osrm,
            clock=clock,
            rng=bot_rng(options.seed, driver_id),
            flaky=driver_id in flaky,
        )
        for driver_id in options.driver_ids
    ]
    loop_lag_s: list[float] = []
    logger.info(
        "Starting %d bots (ids %d..%d, %d flaky) over %gs",
        len(bots),
        options.first_id,
        options.driver_ids[-1],
        len(flaky),
        options.ramp_up_s,
    )
    # Set when the run ends for any reason (duration or stop): bots still waiting
    # for their start in the ramp-up give up waiting.
    ending = asyncio.Event()
    tasks = [
        asyncio.create_task(_start(bot, options.ramp_up_s * i / len(bots), clock, ending))
        for i, bot in enumerate(bots)
    ]
    helpers = [
        asyncio.create_task(_report(bots, loop_lag_s, clock), name="status line"),
        asyncio.create_task(_probe_loop_lag(loop_lag_s, clock), name="loop lag probe"),
    ]
    for helper in helpers:
        helper.add_done_callback(_log_crash)
    try:
        await _until_done(stop, clock, options.duration_s)
    finally:
        logger.info("Stopping %d bots", len(bots))
        ending.set()
        for bot in bots:
            bot.stop()
        try:
            cancelled = await _wait_or_cancel(tasks, clock, config.STOP_BUDGET_S)
        finally:
            # Also when the run itself is cancelled meanwhile (a second Ctrl+C):
            # nothing the run started may outlive it.
            for task in (*tasks, *helpers):
                task.cancel()
            await asyncio.gather(*tasks, *helpers, return_exceptions=True)

    conditions = Conditions(
        drivers=options.drivers,
        duration_s=options.duration_s,
        ramp_up_s=options.ramp_up_s,
        flaky_share=options.flaky_share,
        seed=options.seed,
        url=url,
        started_at=started_at,
        finished_at=datetime.now(UTC),
    )
    failures = strict_failures(bots) + [
        f"{helper.get_name()} crashed: {_crash_of(helper)!r}"
        for helper in helpers
        if _crash_of(helper) is not None
    ]
    if cancelled:
        # They may still be online on the server, waiting for the reaper.
        failures.append(f"{cancelled} bots did not stop in time")
    report = summary(conditions, bots, loop_lag_s, cancelled=cancelled)
    if server_metrics_source is not None:
        server_report, server_failures = await _server_part(
            before, server_metrics_source, bots=bots, cancelled=cancelled
        )
        report += server_report
        failures += server_failures
    return RunResult(
        bots=bots,
        loop_lag_s=loop_lag_s,
        report=report,
        failures=failures,
        cancelled=cancelled,
    )


async def _snapshot(source: MetricsSource | None) -> Snapshot | ServerMetricsUnavailable | None:
    if source is None:
        return None
    try:
        return await source()
    except ServerMetricsUnavailable as exc:
        return exc


async def _server_part(
    before: Snapshot | ServerMetricsUnavailable | None,
    source: MetricsSource,
    *,
    bots: Sequence[DriverBot],
    cancelled: int,
) -> tuple[list[str], list[str]]:
    """The server lines of the report and the --strict failures (9.1 d and more).

    Without metrics the run does not fail on its own, only under --strict: then the
    one check that the server really took the points is missing.
    """
    after = await _snapshot(source)
    try:
        for snapshot in (before, after):
            if isinstance(snapshot, ServerMetricsUnavailable):
                raise snapshot
        assert isinstance(before, Snapshot) and isinstance(after, Snapshot)
        change = after.minus(before)
    except ServerMetricsUnavailable as exc:
        line = f"server metrics: unavailable ({exc})"
        return [line], ["server metrics unavailable: cannot check that points were accepted"]
    return server_metrics.report(change), server_failures(change, bots, cancelled=cancelled)


def server_failures(
    change: Snapshot, bots: Sequence[DriverBot], *, cancelled: int = 0
) -> list[str]:
    """Why a run is not clean, from the server's side (9.1 d and more): what changed
    in its metrics over the run.

    Blind spot: the server counts the points of every client, so points of others
    (e.g. a developer's browser) during the run can hide points the bots lost.
    """
    failures = []
    if change.accepted <= 0:
        failures.append("the server accepted no points")
    refused = dict(change.refused_by_result)
    if any_flaky(bots):
        # Points of silent flaky bots after the reaper took them offline.
        refused.pop("rejected_offline", None)
    if refused:
        failures.append(f"points refused by the server: {refused}")
    closes = {code: count for code, count in change.closes.items() if count}
    if closes:
        failures.append(f"drivers closed by the server: {closes}")
    totals = Totals.of(bots)
    # A connection that ends without a goodbye (a drop, a close by the server, a
    # timeout, a cancelled bot) may take the points still in its socket with it;
    # those cases are failures of their own when they are not the bots' drops.
    may_lose = totals.drops + totals.reconnects + cancelled
    missing = totals.locations_sent - sum(change.updates.values())
    if missing > may_lose:
        failures.append(f"{missing:.0f} points sent by the bots never reached the server")
    return failures


def any_flaky(bots: Sequence[DriverBot]) -> bool:
    """From the bots themselves: a share rounds to no flaky bot among a few bots."""
    return any(bot.flaky for bot in bots)


def strict_failures(bots: Sequence[DriverBot]) -> list[str]:
    """Why a run is not clean, from the bots' side (ТЗ 08, 9.1 a-c and more): anything
    the server refused, closed or did not answer. Flaky bots misbehave on purpose,
    but none of that ends in a close or a failed connection; their silences end in
    not_online, which only counts as a failure when no bot is flaky."""
    totals = Totals.of(bots)
    failures = []
    if totals.never_online:
        failures.append(f"{totals.never_online} bots never got online")
    if totals.stopped:
        failures.append(f"bots stopped for good: {dict(totals.stopped)}")
    verdicts = _BOT_FAULTS if any_flaky(bots) else _BOT_FAULTS | {ErrorVerdict.NOT_ONLINE}
    faults = {
        code: count
        for code, count in totals.errors.items()
        if protocol.classify_error(code) in verdicts
    }
    if faults:
        failures.append(f"server errors caused by the bots: {faults}")
    if totals.closes:
        failures.append(f"connections closed by the server: {dict(totals.closes)}")
    if totals.connect_failures or totals.handshake_timeouts:
        failures.append(
            f"failed connections: {totals.connect_failures}, "
            f"handshake timeouts: {totals.handshake_timeouts}"
        )
    if totals.unknown_messages:
        failures.append(f"unknown messages from the server: {totals.unknown_messages}")
    if totals.left_online:
        failures.append(f"{totals.left_online} bots may be left online after the stop")
    return failures


async def _start(bot: DriverBot, delay: float, clock: Clock, ending: asyncio.Event) -> None:
    await _first(clock.sleep(delay), ending.wait())
    # A bot stopped before its start returns at once from run().
    try:
        await bot.run()
    except Exception:
        # A bug in the bot: show it and count the bot as stopped (--strict), instead
        # of losing it silently in the gather at the end.
        logger.exception("driver %d: crashed", bot.driver_id)
        bot.stop_reason = "crashed"


async def _until_done(stop: asyncio.Event, clock: Clock, duration_s: float | None) -> None:
    if duration_s is None:
        await stop.wait()
    else:
        await _first(clock.sleep(duration_s), stop.wait())


async def _first(*coroutines: Coroutine[Any, Any, object]) -> None:
    """Wait until any of the coroutines finishes; cancel the rest."""
    tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _wait_or_cancel(tasks: list[asyncio.Task[None]], clock: Clock, seconds: float) -> int:
    """Give the bots `seconds` to go offline and close; cancel the ones that did not.
    Returns how many were cancelled."""
    pending = {task for task in tasks if not task.done()}
    if pending:
        # One wait for all of them (asyncio.wait never cancels what it waits for),
        # raced against the deadline on the bots' clock.
        everyone = asyncio.create_task(asyncio.wait(pending))
        deadline = asyncio.create_task(clock.sleep(seconds))
        try:
            await asyncio.wait({everyone, deadline}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in (everyone, deadline):
                waiter.cancel()
            await asyncio.gather(everyone, deadline, return_exceptions=True)
    # Only the bots still running now; one that finished at the deadline is not late.
    late = [task for task in tasks if not task.done()]
    if late:
        logger.warning("%d bots did not stop in %gs, cancelling them", len(late), seconds)
        for task in late:
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    return len(late)


def _crash_of(task: asyncio.Task[None]) -> BaseException | None:
    """What a finished helper task died of; None if it was only cancelled."""
    if not task.done() or task.cancelled():
        return None
    return task.exception()


def _log_crash(task: asyncio.Task[None]) -> None:
    """At once, not only in the summary: the status lines would stop without a word."""
    crash = _crash_of(task)
    if crash is not None:
        logger.error("%s crashed", task.get_name(), exc_info=crash)


async def _report(bots: list[DriverBot], loop_lag_s: list[float], clock: Clock) -> None:
    line = StatusLine()
    line.render(bots, loop_lag_s, clock.monotonic())  # the baseline for sent/s
    while True:
        await clock.sleep(config.REPORT_INTERVAL_S)
        logger.info(line.render(bots, loop_lag_s, clock.monotonic()))


async def _probe_loop_lag(samples: list[float], clock: Clock) -> None:
    """How late a short sleep wakes up: the first sign that one process is not enough
    for the bots (S4), long before the server starts rejecting stale points."""
    while True:
        due = clock.monotonic() + config.LOOP_LAG_PROBE_S
        await clock.sleep(config.LOOP_LAG_PROBE_S)
        samples.append(max(clock.monotonic() - due, 0.0))
