"""The runner on virtual time: ramp-up, duration and stop, the shutdown of every bot,
flaky bots, the status line, the loop lag probe and the --strict verdict."""

import asyncio
import logging
import math
from collections import Counter
from types import SimpleNamespace

import pytest

from sim import config, runner
from sim.driver_bot import BotStats
from sim.runner import RunOptions, flaky_ids, server_failures, strict_failures
from sim.server_metrics import ServerMetricsUnavailable, Snapshot
from sim.stats import Totals
from tests.conftest import FakeServer

INF = math.inf


class City:
    """One fake server per bot, as if each phone had the realtime server to itself.

    Also remembers when each server got its last point, and how many points came
    while the driver was offline (the server's rejected_offline).
    """

    def __init__(self, clock):
        self.clock = clock
        self.servers: dict[int, FakeServer] = {}
        self.first_connect_at: dict[int, float] = {}
        self.last_point_at: dict[int, float] = {}
        self.refused_offline = 0

    def server(self, driver_id: int) -> FakeServer:
        if driver_id not in self.servers:
            server = FakeServer()
            handle = server.handle

            def record(connection, message):
                if message["type"] == "location":
                    self.last_point_at[driver_id] = self.clock.elapsed
                    self.refused_offline += server.status != "available"
                handle(connection, message)

            server.handle = record
            self.servers[driver_id] = server
        return self.servers[driver_id]

    def connect_for(self, driver_id: int):
        async def connect():
            self.first_connect_at.setdefault(driver_id, self.clock.elapsed)
            return await self.server(driver_id).connect()

        return connect


@pytest.fixture
def city(clock) -> City:
    return City(clock)


async def simulate(city, osrm, clock, *, stop=None, metrics=None, **options) -> runner.RunResult:
    return await runner.run(
        RunOptions(**options),
        connect_for=city.connect_for,
        osrm=osrm,
        clock=clock,
        stop=stop or asyncio.Event(),
        url="ws://test",
        server_metrics_source=metrics,
    )


class FakeMetrics:
    """/rt/metrics that counts the points the fake servers took; or is down."""

    def __init__(self, city, *, down_at: frozenset[int] = frozenset()):
        self.city = city
        self.calls = 0
        self.down_at = down_at

    async def __call__(self) -> Snapshot:
        self.calls += 1
        if self.calls in self.down_at:
            raise ServerMetricsUnavailable("ConnectError: refused")
        points = sum(len(server.sent("location")) for server in self.city.servers.values())
        accepted = points - self.city.refused_offline
        return Snapshot(
            updates={"accepted": accepted, "rejected_offline": self.city.refused_offline},
            buckets={0.001: accepted, INF: accepted},
        )


async def reap(city: City, clock) -> None:
    """The dispatcher's reaper for the fake servers: every SERVER_REAPER_INTERVAL_S
    takes offline a driver without a point for SERVER_DRIVER_SILENCE_S. Like the real
    one, only points count (drivers:last_seen), not pings."""
    while True:
        await clock.sleep(config.SERVER_REAPER_INTERVAL_S)
        for driver_id, server in city.servers.items():
            last = city.last_point_at.get(driver_id)
            silent = last is not None and clock.elapsed - last > config.SERVER_DRIVER_SILENCE_S
            if server.status == "available" and silent:
                server.status = "offline"


class TestStart:
    async def test_starts_are_spread_over_the_ramp_up(self, city, osrm, clock):
        await simulate(city, osrm, clock, drivers=10, ramp_up_s=10, duration_s=15)

        starts = [city.first_connect_at[driver_id] for driver_id in range(1, 11)]
        assert starts == pytest.approx([float(n) for n in range(10)], abs=0.01)

    async def test_bots_tick_at_different_moments(self, city, osrm, clock):
        await simulate(city, osrm, clock, drivers=10, ramp_up_s=0, duration_s=10)

        first_ticks = {city.server(i).sent("location")[0]["ts"] % 2000 for i in range(1, 11)}
        assert len(first_ticks) == 10

    async def test_bots_use_the_requested_ids(self, city, osrm, clock):
        result = await simulate(city, osrm, clock, drivers=3, first_id=501, duration_s=5)

        assert [bot.driver_id for bot in result.bots] == [501, 502, 503]
        assert city.server(502).connections[0].sent[0]["data"]["token"] == "dev:driver:502"


class TestStop:
    async def test_duration_ends_the_run_and_every_bot_goes_offline(self, city, osrm, clock):
        result = await simulate(city, osrm, clock, drivers=5, ramp_up_s=1, duration_s=30)

        for server in city.servers.values():
            assert server.connections[-1].sent_types()[-1] == "go_offline"
            assert server.connections[-1].closed_with == 1000
            assert server.status == "offline"
        assert result.report[0] == "--- run ---"

    async def test_stop_signal_ends_a_run_without_a_duration(self, city, osrm, clock):
        stop = asyncio.Event()
        run = asyncio.create_task(simulate(city, osrm, clock, stop=stop, drivers=3))
        await clock.sleep(20)

        stop.set()
        result = await run

        assert all(not bot.connected for bot in result.bots)
        assert all(server.status == "offline" for server in city.servers.values())

    async def test_stop_before_a_bot_has_started_skips_it(self, city, osrm, clock):
        await simulate(city, osrm, clock, drivers=10, ramp_up_s=100, duration_s=5)

        assert len(city.first_connect_at) == 1

    async def test_bot_that_does_not_stop_in_time_is_cancelled(
        self, city, osrm, clock, monkeypatch, caplog
    ):
        hung = asyncio.Event()
        real_connect_for = city.connect_for

        def connect_for(driver_id):
            if driver_id == 2:

                async def never_connects():
                    await hung.wait()

                return never_connects
            return real_connect_for(driver_id)

        monkeypatch.setattr(city, "connect_for", connect_for)
        started = clock.elapsed

        result = await simulate(city, osrm, clock, drivers=2, ramp_up_s=0, duration_s=10)

        assert clock.elapsed - started == pytest.approx(10 + config.STOP_BUDGET_S, abs=0.1)
        assert "1 bots did not stop" in caplog.text
        assert result.cancelled == 1
        assert "1 bots did not stop in time" in result.failures
        assert "cancelled at the stop: 1" in result.report[-1]

    async def test_bots_that_stop_in_time_are_not_counted_as_cancelled(self, city, osrm, clock):
        result = await simulate(city, osrm, clock, drivers=20, ramp_up_s=0, duration_s=10)

        assert result.cancelled == 0
        assert "cancelled at the stop: 0" in result.report[-1]

    @pytest.mark.parametrize("cancels", [1, 2], ids=["second_ctrl_c", "third_ctrl_c"])
    async def test_run_cancelled_while_stopping_leaves_no_task_behind(
        self, city, osrm, clock, cancels
    ):
        # Another Ctrl+C cancels the run while the bots still wait for go_offline.
        for driver_id in (1, 2, 3):
            city.server(driver_id).silent.add("go_offline")
        stop = asyncio.Event()
        run = asyncio.create_task(simulate(city, osrm, clock, stop=stop, drivers=3, ramp_up_s=0))
        await clock.sleep(10)
        stop.set()
        await clock.sleep(1)  # the bots have sent go_offline and wait for the answer
        before = asyncio.all_tasks() - {asyncio.current_task(), run}

        for _ in range(cancels):
            run.cancel()
            await asyncio.sleep(0)
        with pytest.raises(asyncio.CancelledError):
            await run

        # Everything the run started is finished, not left for asyncio.run to reap;
        # only the virtual clock's own scheduler may still be there.
        left = {task for task in before if not task.done()}
        assert left <= {clock.scheduler}
        assert all(server.connections[-1].aborted for server in city.servers.values())


class TestHelperCrashes:
    async def test_crash_of_the_status_line_is_logged_at_once_and_fails_the_run(
        self, city, osrm, clock, monkeypatch, caplog
    ):
        calls = []

        def broken_render(self, bots, loop_lag_s, now):
            calls.append(now)
            if len(calls) > 1:  # the baseline at the start works, the first line breaks
                raise ZeroDivisionError("bug")
            return ""

        monkeypatch.setattr(runner.StatusLine, "render", broken_render)

        result = await simulate(city, osrm, clock, drivers=1, duration_s=30)

        crash = next(r for r in caplog.records if "status line crashed" in r.getMessage())
        assert crash.levelno == logging.ERROR
        assert crash.exc_info is not None  # the traceback is in the log
        assert "status line crashed: ZeroDivisionError('bug')" in result.failures

    async def test_crash_of_the_loop_lag_probe_fails_the_run(self, city, osrm, clock, monkeypatch):
        async def broken_probe(samples, clock):
            raise RuntimeError("bug")

        monkeypatch.setattr(runner, "_probe_loop_lag", broken_probe)

        result = await simulate(city, osrm, clock, drivers=1, duration_s=5)

        assert "loop lag probe crashed: RuntimeError('bug')" in result.failures


class TestFlakyBots:
    def test_share_of_flaky_bots(self):
        ids = flaky_ids(RunOptions(drivers=50, flaky_share=0.2, seed=1))

        assert len(ids) == 10
        assert ids <= set(range(1, 51))

    def test_same_seed_picks_the_same_bots(self):
        options = RunOptions(drivers=50, flaky_share=0.2, seed=1)

        assert flaky_ids(options) == flaky_ids(options)
        assert flaky_ids(options) != flaky_ids(RunOptions(drivers=50, flaky_share=0.2, seed=2))

    @pytest.mark.parametrize(("share", "count"), [(0, 0), (1, 20)])
    def test_no_or_all_bots(self, share, count):
        assert len(flaky_ids(RunOptions(drivers=20, flaky_share=share))) == count

    async def test_flaky_bots_are_marked(self, city, osrm, clock):
        options = {"drivers": 10, "flaky_share": 0.3, "seed": 4}

        result = await simulate(city, osrm, clock, duration_s=1, **options)

        flaky = {bot.driver_id for bot in result.bots if bot.flaky}
        assert flaky == flaky_ids(RunOptions(**options))


class TestReporting:
    async def test_status_line_every_report_interval(self, city, osrm, clock, caplog):
        caplog.set_level(logging.INFO, logger="sim.runner")

        await simulate(city, osrm, clock, drivers=2, ramp_up_s=0, duration_s=35)

        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("bots=")]
        assert len(lines) == 3
        assert lines[-1].startswith("bots=2 connected=2 online=2 ")
        assert "sent/s=1.0 " in lines[-1]  # two bots, a point every 2 s each

    async def test_loop_lag_is_sampled_all_along(self, city, osrm, clock):
        result = await simulate(city, osrm, clock, drivers=1, duration_s=10)

        assert len(result.loop_lag_s) == pytest.approx(10 / config.LOOP_LAG_PROBE_S, abs=2)
        assert max(result.loop_lag_s) == 0  # virtual time is never late

    async def test_report_has_client_numbers(self, city, osrm, clock):
        result = await simulate(city, osrm, clock, drivers=2, ramp_up_s=0, duration_s=20)

        text = "\n".join(result.report)
        assert "bots: 2, duration: 20s" in text
        assert "points sent: 20 " in text


class TestStrict:
    async def test_clean_run_has_no_failures(self, city, osrm, clock):
        result = await simulate(city, osrm, clock, drivers=3, duration_s=30)

        assert result.failures == []

    async def test_bot_that_never_got_online_fails_the_run(self, city, osrm, clock):
        city.server(2).silent.add("go_online")

        result = await simulate(city, osrm, clock, drivers=3, ramp_up_s=0, duration_s=4)

        assert result.failures == ["1 bots never got online"]

    async def test_bots_stopped_before_their_start_fail_the_run(self, city, osrm, clock):
        # --duration shorter than --ramp-up: most bots never even tried.
        result = await simulate(city, osrm, clock, drivers=10, ramp_up_s=100, duration_s=5)

        assert result.failures == ["9 bots never started: the run ended within the ramp-up"]
        assert "never started: 9, never online: 0" in "\n".join(result.report)

    async def test_crashing_bot_is_logged_and_fails_the_run(
        self, city, osrm, clock, monkeypatch, caplog
    ):
        def connect_for(driver_id):
            async def broken():
                raise RuntimeError("bug")

            return broken

        monkeypatch.setattr(city, "connect_for", connect_for)

        result = await simulate(city, osrm, clock, drivers=1, duration_s=5)

        assert result.bots[0].stop_reason == "crashed"
        assert "bots stopped for good: {'crashed': 1}" in result.failures
        assert "driver 1: crashed" in caplog.text

    async def test_bot_stopped_for_good_fails_the_run(self, city, osrm, clock):
        city.server(1).close_after_auth = 4403

        result = await simulate(city, osrm, clock, drivers=2, duration_s=10)

        assert "bots stopped for good: {'closed 4403': 1}" in result.failures

    @pytest.mark.parametrize(
        "code", ["location_jump", "location_stale", "rate_limited", "invalid_message"]
    )
    async def test_error_caused_by_the_bots_fails_the_run(self, city, osrm, clock, code):
        run = asyncio.create_task(simulate(city, osrm, clock, drivers=1, duration_s=10))
        await clock.sleep(5)

        city.server(1).connections[0].push("error", code=code, message="...")
        result = await run

        assert result.failures == [f"server errors caused by the bots: {{'{code}': 1}}"]

    def test_not_online_of_silent_flaky_bots_is_not_a_failure(self):
        bot = bot_view(errors=Counter(not_online=3), flaky=True)

        failures = strict_failures([bot])

        assert failures == []

    def test_not_online_of_a_steady_bot_fails_even_among_flaky_ones(self):
        flaky = bot_view(errors=Counter(not_online=3), flaky=True)
        steady = bot_view(errors=Counter(not_online=1))

        failures = strict_failures([flaky, steady])

        assert failures == ["server errors caused by the bots: {'not_online': 1}"]

    def test_bot_that_never_started_is_a_failure(self):
        bot = bot_view(started=False, went_online=False)

        failures = strict_failures([bot])

        assert failures == ["1 bots never started: the run ended within the ramp-up"]

    def test_not_online_without_flaky_bots_is_a_failure(self):
        # Nobody falls silent: the reaper had no reason to take a bot offline.
        bot = bot_view(errors=Counter(not_online=1))

        failures = strict_failures([bot])

        assert failures == ["server errors caused by the bots: {'not_online': 1}"]

    @pytest.mark.parametrize(
        ("stats", "failure"),
        [
            ({"closes": Counter({1011: 2})}, "connections closed by the server: {1011: 2}"),
            ({"connect_failures": 1}, "failed connections: 1, handshake timeouts: 0"),
            ({"handshake_timeouts": 1}, "failed connections: 0, handshake timeouts: 1"),
            ({"unknown_messages": 3}, "unknown messages from the server: 3"),
            ({"left_online": 1}, "1 bots may be left online after the stop"),
            ({"pongs_missed": 2}, "2 pings got no pong"),
            ({"osrm_failures": 3}, "3 OSRM requests failed: cars stood or started off the road"),
        ],
        ids=[
            "closes",
            "connect_failures",
            "handshake_timeouts",
            "unknown_messages",
            "left",
            "pongs",
            "osrm",
        ],
    )
    def test_anything_the_server_closed_or_did_not_answer_is_a_failure(self, stats, failure):
        bot = bot_view(flaky=True, **stats)

        failures = strict_failures([bot])

        assert failures == [failure]

    def test_clean_bot_has_no_failures(self):
        failures = strict_failures([bot_view(locations_sent=100)])

        assert failures == []


def bot_view(*, flaky: bool = False, **stats) -> SimpleNamespace:
    """What the strict checks read from a bot (by default one that started and got
    online)."""
    return SimpleNamespace(
        stats=BotStats(**({"started": True, "went_online": True} | stats)),
        stop_reason=None,
        flaky=flaky,
    )


@pytest.fixture
def often_flaky(monkeypatch):
    """A misbehaviour every 30 s of driving; drops of 10 s."""
    monkeypatch.setattr(config, "FLAKY_EVERY_S", (30, 30))
    monkeypatch.setattr(config, "DROP_DURATION_S", (10, 10))


class TestFlakyRun:
    """A whole run with flaky bots against fake servers with a reaper: what they do on
    purpose fails nothing, anything else still does."""

    OPTIONS = {"drivers": 6, "flaky_share": 0.5, "ramp_up_s": 0, "duration_s": 240}

    async def run_with_reaper(self, city, osrm, clock, *, during=None) -> runner.RunResult:
        reaper = asyncio.create_task(reap(city, clock))
        run = asyncio.create_task(
            simulate(city, osrm, clock, metrics=FakeMetrics(city), **self.OPTIONS)
        )
        if during is not None:
            await during()
        try:
            return await run
        finally:
            reaper.cancel()
            await asyncio.gather(reaper, return_exceptions=True)

    async def test_drops_and_silences_of_flaky_bots_leave_the_run_clean(
        self, city, osrm, clock, often_flaky
    ):
        result = await self.run_with_reaper(city, osrm, clock)

        totals = Totals.of(result.bots)
        # The run really had what it is about: drops, and silences ended by the reaper.
        assert totals.drops > 0
        assert totals.silences > 0
        assert totals.errors["not_online"] > 0
        assert city.refused_offline > 0
        assert result.failures == []

    async def test_not_online_of_a_steady_bot_fails_a_run_with_flaky_ones(
        self, city, osrm, clock, often_flaky
    ):
        flaky = flaky_ids(RunOptions(**self.OPTIONS))
        steady = next(i for i in RunOptions(**self.OPTIONS).driver_ids if i not in flaky)

        async def lose_the_steady_bot():
            await clock.sleep(100)
            city.server(steady).status = "offline"  # e.g. a bug in the reaper

        result = await self.run_with_reaper(city, osrm, clock, during=lose_the_steady_bot)

        assert result.failures == ["server errors caused by the bots: {'not_online': 1}"]


class TestServerFailures:
    def test_clean_change_has_no_failures(self):
        change = Snapshot(updates={"accepted": 10})

        failures = server_failures(change, [bot_view(locations_sent=10)])

        assert failures == []

    def test_refused_points_are_a_failure(self):
        change = Snapshot(updates={"accepted": 8, "rejected_jump": 1, "rejected_offline": 1})

        failures = server_failures(change, [bot_view(locations_sent=10)])

        assert failures == [
            "points refused by the server: {'rejected_jump': 1, 'rejected_offline': 1}"
        ]

    def test_offline_points_of_silent_flaky_bots_are_not_a_failure(self):
        change = Snapshot(updates={"accepted": 9, "rejected_offline": 1})
        bot = bot_view(locations_sent=10, flaky=True, errors=Counter(not_online=1))

        failures = server_failures(change, [bot])

        assert failures == []

    def test_offline_points_no_bot_heard_about_are_a_failure(self):
        # Every point refused as offline gets a not_online back; one more refusal
        # than the bots heard of (or lost in a drop) is not a silent flaky bot.
        change = Snapshot(updates={"accepted": 7, "rejected_offline": 3})
        bot = bot_view(locations_sent=10, flaky=True, errors=Counter(not_online=1), drops=1)

        failures = server_failures(change, [bot])

        assert failures == ["points refused by the server: {'rejected_offline': 3}"]

    def test_drivers_closed_by_the_server_are_a_failure(self):
        change = Snapshot(updates={"accepted": 10}, closes={"1011": 2, "4409": 0})

        failures = server_failures(change, [bot_view(locations_sent=10)])

        assert failures == ["drivers closed by the server: {'1011': 2}"]

    def test_points_that_never_reached_the_server_are_a_failure(self):
        change = Snapshot(updates={"accepted": 7})

        failures = server_failures(change, [bot_view(locations_sent=10)])

        assert failures == ["3 points sent by the bots never reached the server"]

    @pytest.mark.parametrize(
        ("lost_with", "stats", "cancelled"),
        [
            ("drops", {"drops": 2}, 0),
            ("reconnects", {"reconnects": 2}, 0),
            ("cancelled", {}, 2),
        ],
        ids=["drops", "reconnects", "cancelled"],
    )
    def test_points_lost_with_ended_connections_are_tolerated_up_to_one_each(
        self, lost_with, stats, cancelled
    ):
        # A connection ending without a goodbye may take the points in its socket.
        bot = bot_view(locations_sent=10, **stats)

        exactly_at_the_limit = server_failures(
            Snapshot(updates={"accepted": 8}), [bot], cancelled=cancelled
        )
        one_more = server_failures(Snapshot(updates={"accepted": 7}), [bot], cancelled=cancelled)

        assert exactly_at_the_limit == []
        assert one_more == ["3 points sent by the bots never reached the server"]

    def test_points_of_other_clients_can_hide_lost_points(self):
        # A known blind spot: the server counts every client, e.g. a developer's
        # browser during the run, so the surplus covers points the bots lost.
        change = Snapshot(updates={"accepted": 15})

        failures = server_failures(change, [bot_view(locations_sent=10)])

        assert failures == []

    def test_several_failures_are_all_reported(self):
        change = Snapshot(updates={"accepted": 0, "rejected_jump": 1}, closes={"1011": 1})

        failures = server_failures(change, [bot_view(locations_sent=5)])

        assert failures == [
            "the server accepted no points",
            "points refused by the server: {'rejected_jump': 1}",
            "drivers closed by the server: {'1011': 1}",
            "4 points sent by the bots never reached the server",
        ]

    def test_flaky_share_that_rounds_to_no_flaky_bot_is_strict(self):
        # --flaky-share 0.2 with 2 bots: no bot is flaky, so nothing is tolerated.
        change = Snapshot(updates={"accepted": 9, "rejected_offline": 1})

        failures = server_failures(change, [bot_view(locations_sent=10, flaky=False)])

        assert failures == ["points refused by the server: {'rejected_offline': 1}"]


class TestServerMetrics:
    async def test_report_has_the_change_of_the_server_metrics(self, city, osrm, clock):
        metrics = FakeMetrics(city)

        result = await simulate(city, osrm, clock, drivers=2, duration_s=20, metrics=metrics)

        assert metrics.calls == 2  # before and after the run
        text = "\n".join(result.report)
        assert "--- server (change of /rt/metrics over the run) ---" in text
        assert "location updates: accepted=" in text

    async def test_server_that_accepted_no_points_fails_a_strict_run(self, city, osrm, clock):
        async def nothing_accepted() -> Snapshot:
            return Snapshot(updates={"accepted": 0})

        result = await simulate(
            city, osrm, clock, drivers=1, duration_s=10, metrics=nothing_accepted
        )

        assert "the server accepted no points" in result.failures

    @pytest.mark.parametrize("down_at", [frozenset({1}), frozenset({2})], ids=["before", "after"])
    async def test_unavailable_metrics_are_reported_and_fail_a_strict_run(
        self, city, osrm, clock, down_at
    ):
        metrics = FakeMetrics(city, down_at=down_at)

        result = await simulate(city, osrm, clock, drivers=1, duration_s=10, metrics=metrics)

        assert result.report[-1] == "server metrics: unavailable (ConnectError: refused)"
        assert result.failures == [
            "server metrics unavailable: cannot check that points were accepted"
        ]

    async def test_server_restarted_during_the_run_is_reported(self, city, osrm, clock):
        snapshots = iter([Snapshot(updates={"accepted": 500}), Snapshot(updates={"accepted": 3})])

        async def restarting() -> Snapshot:
            return next(snapshots)

        result = await simulate(city, osrm, clock, drivers=1, duration_s=5, metrics=restarting)

        assert result.report[-1] == (
            "server metrics: unavailable (the server restarted during the run)"
        )

    async def test_run_without_a_metrics_source_has_no_server_part(self, city, osrm, clock):
        result = await simulate(city, osrm, clock, drivers=1, duration_s=5)

        assert "--- server (change of /rt/metrics over the run) ---" not in result.report
        assert not any(line.startswith("server metrics") for line in result.report)
