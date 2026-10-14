"""Command line of the simulator.

    python -m sim run [--drivers 100] [--first-id 1] [--duration SECONDS]
                      [--ramp-up 10] [--flaky-share 0.05] [--seed INT] [--strict]

Without --duration the bots run until SIGINT/SIGTERM (Ctrl+C). The handlers matter
in Docker: the process is PID 1 there, and PID 1 ignores signals it has no handler
for, so the bots would be killed without going offline. Exit code: 0, or 1 when
--strict finds the run unclean, 2 for bad arguments.
"""

import argparse
import asyncio
import logging
import math
import signal
import sys

import httpx

from sim import config, runner
from sim.clock import Clock
from sim.connection import open_connection
from sim.osrm import OsrmClient

# Not __name__: run with `python -m`, that is "__main__", which says nothing in the logs.
logger = logging.getLogger("sim")

_STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)


def _number(kind: type, *, low: float, low_inclusive: bool = True, high: float | None = None):
    """An argparse type: a number within limits, with a readable error otherwise."""

    def parse(value: str):
        try:
            number = kind(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not a number: {value!r}") from None
        # NaN passes every comparison below, and inf makes no sense for any of them
        # (no --duration already means "until stopped").
        if not math.isfinite(number):
            raise argparse.ArgumentTypeError(f"must be a finite number, got {value}")
        too_low = number < low if low_inclusive else number <= low
        if too_low or (high is not None and number > high):
            limits = f"{'>=' if low_inclusive else '>'} {low}"
            if high is not None:
                limits += f" and <= {high}"
            raise argparse.ArgumentTypeError(f"must be {limits}, got {value}")
        return number

    return parse


def parse_args(argv: list[str] | None) -> runner.RunOptions:
    defaults = runner.RunOptions()
    parser = argparse.ArgumentParser(prog="python -m sim")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run driver bots against the realtime server")
    run.add_argument("--drivers", type=_number(int, low=1), default=defaults.drivers)
    run.add_argument("--first-id", type=_number(int, low=1), default=defaults.first_id)
    run.add_argument(
        "--duration",
        type=_number(float, low=0, low_inclusive=False),
        help="seconds; without it the bots run until Ctrl+C",
    )
    run.add_argument("--ramp-up", type=_number(float, low=0), default=defaults.ramp_up_s)
    run.add_argument(
        "--flaky-share", type=_number(float, low=0, high=1), default=defaults.flaky_share
    )
    run.add_argument("--seed", type=int, default=defaults.seed)
    run.add_argument(
        "--strict",
        action="store_true",
        help="exit with 1 if a bot never got online, stopped for good or was refused (CI)",
    )
    args = parser.parse_args(argv)
    return runner.RunOptions(
        drivers=args.drivers,
        first_id=args.first_id,
        duration_s=args.duration,
        ramp_up_s=args.ramp_up,
        flaky_share=args.flaky_share,
        seed=args.seed,
        strict=args.strict,
    )


async def simulate(options: runner.RunOptions) -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()

    def on_signal() -> None:
        if stop.is_set():
            # A second Ctrl+C while the bots go offline: do not make the user wait.
            logger.warning("Stopping at once, without waiting for the bots")
            main_task.cancel()
        else:
            logger.info("Stopping: the bots go offline (Ctrl+C again to force)")
            stop.set()

    for sig in _STOP_SIGNALS:
        loop.add_signal_handler(sig, on_signal)
    try:
        async with httpx.AsyncClient(base_url=config.OSRM_URL) as http:
            result = await runner.run(
                options,
                connect_for=lambda _driver_id: lambda: open_connection(config.REALTIME_WS_URL),
                osrm=OsrmClient(http),
                clock=Clock(),
                stop=stop,
                url=config.REALTIME_WS_URL,
            )
    except asyncio.CancelledError:
        return 1
    finally:
        for sig in _STOP_SIGNALS:
            loop.remove_signal_handler(sig)

    # print, not log: the summary is meant to be copied (e.g. into a pull request).
    print("\n".join(result.report), flush=True)
    if options.strict and result.failures:
        for failure in result.failures:
            logger.error("Strict run failed: %s", failure)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    options = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # One INFO line per OSRM request would drown the status lines.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return asyncio.run(simulate(options))


if __name__ == "__main__":
    sys.exit(main())
