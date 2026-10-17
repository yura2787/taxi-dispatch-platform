"""The command line: argument limits, defaults, the exit code and signals."""

import asyncio
import os
import signal

import pytest

from sim import __main__ as cli
from sim import runner
from sim.latency import LatencyRecorder
from sim.runner import RunOptions, RunResult


class TestArguments:
    def test_defaults(self):
        options = cli.parse_args(["run"])

        assert options == RunOptions(
            drivers=100,
            first_id=1,
            duration_s=None,
            ramp_up_s=10,
            flaky_share=0.05,
            seed=0,
            strict=False,
        )

    def test_all_arguments(self):
        argv = "run --drivers 20 --first-id 501 --duration 60 --ramp-up 0 --flaky-share 1"
        argv += " --seed 3 --strict"

        options = cli.parse_args(argv.split())

        assert options == RunOptions(
            drivers=20,
            first_id=501,
            duration_s=60.0,
            ramp_up_s=0.0,
            flaky_share=1.0,
            seed=3,
            strict=True,
        )

    @pytest.mark.parametrize(
        ("argument", "value"),
        [
            ("--drivers", "0"),
            ("--drivers", "many"),
            ("--first-id", "0"),
            ("--duration", "0"),
            ("--duration", "-5"),
            ("--ramp-up", "-1"),
            ("--flaky-share", "1.5"),
            ("--flaky-share", "-0.1"),
            ("--flaky-share", "nan"),
            ("--duration", "nan"),
            ("--duration", "inf"),
            ("--ramp-up", "inf"),
        ],
    )
    def test_value_out_of_range_exits_with_2(self, argument, value, capsys):
        with pytest.raises(SystemExit) as exc_info:
            cli.parse_args(["run", argument, value])

        assert exc_info.value.code == 2
        assert argument in capsys.readouterr().err

    def test_command_is_required(self):
        with pytest.raises(SystemExit) as exc_info:
            cli.parse_args([])

        assert exc_info.value.code == 2


def fake_run(
    failures: list[str],
    started: asyncio.Event | None = None,
    *,
    stopping: asyncio.Event | None = None,
    hang_after_stop: bool = False,
):
    """runner.run that waits for --duration or the stop, then returns a fixed result.
    `started` is set once it runs (the signal handlers are installed by then),
    `stopping` once it has seen the stop."""

    async def run(options, *, stop, **kwargs):
        if started is not None:
            started.set()
        if options.duration_s is None:
            await stop.wait()
            if stopping is not None:
                stopping.set()
            if hang_after_stop:
                await asyncio.Event().wait()  # bots that never finish going offline
        return RunResult(
            bots=[],
            loop_lag=LatencyRecorder(),
            report=["--- run ---", "points sent: 0"],
            failures=failures,
        )

    return run


class TestMain:
    def test_prints_the_summary_and_exits_with_0(self, monkeypatch, capsys):
        monkeypatch.setattr(runner, "run", fake_run(failures=[]))

        code = cli.main(["run", "--duration", "1"])

        assert code == 0
        assert "points sent: 0" in capsys.readouterr().out

    def test_strict_run_with_failures_exits_with_1(self, monkeypatch, caplog):
        monkeypatch.setattr(runner, "run", fake_run(failures=["2 bots never got online"]))

        code = cli.main(["run", "--duration", "1", "--strict"])

        assert code == 1
        assert "2 bots never got online" in caplog.text

    def test_failures_without_strict_are_logged_and_exit_with_0(self, monkeypatch, caplog):
        monkeypatch.setattr(runner, "run", fake_run(failures=["2 pings got no pong"]))

        code = cli.main(["run", "--duration", "1"])

        assert code == 0
        record = next(r for r in caplog.records if "2 pings got no pong" in r.getMessage())
        assert record.levelname == "WARNING"

    async def test_sigterm_stops_the_run(self, monkeypatch):
        started = asyncio.Event()
        monkeypatch.setattr(runner, "run", fake_run(failures=[], started=started))
        task = asyncio.create_task(cli.simulate(RunOptions()))
        await asyncio.wait_for(started.wait(), 2)

        os.kill(os.getpid(), signal.SIGTERM)
        code = await asyncio.wait_for(task, 2)

        assert code == 0
        assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL

    async def test_second_signal_forces_the_exit(self, monkeypatch, caplog):
        started, stopping = asyncio.Event(), asyncio.Event()
        run = fake_run(failures=[], started=started, stopping=stopping, hang_after_stop=True)
        monkeypatch.setattr(runner, "run", run)
        task = asyncio.create_task(cli.simulate(RunOptions()))
        await asyncio.wait_for(started.wait(), 2)
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(stopping.wait(), 2)  # the first signal was handled

        os.kill(os.getpid(), signal.SIGTERM)
        code = await asyncio.wait_for(task, 2)

        assert code == 1
        assert "Stopping at once" in caplog.text
        assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
