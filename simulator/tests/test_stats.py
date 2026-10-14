"""Percentiles, totals over the bots, the status line and the run summary."""

from collections import Counter
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from sim.driver_bot import BotStats
from sim.stats import Conditions, StatusLine, Totals, ms, percentile, summary

STARTED = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


def make_bot_view(*, stop_reason=None, connected=True, status="available", on_break=False, **stats):
    """What the report reads from a bot: its stats and a few state fields."""
    return SimpleNamespace(
        stats=BotStats(**stats),
        stop_reason=stop_reason,
        connected=connected,
        status=status,
        on_break=on_break,
    )


def make_conditions(**overrides) -> Conditions:
    values = {
        "drivers": 2,
        "duration_s": 60.0,
        "ramp_up_s": 10.0,
        "flaky_share": 0.05,
        "seed": 7,
        "url": "ws://nginx/rt/ws/driver",
        "started_at": STARTED,
        "finished_at": STARTED + timedelta(seconds=60),
        "cpus": 8,
    }
    return Conditions(**(values | overrides))


class TestPercentile:
    @pytest.mark.parametrize(
        ("q", "expected"), [(0, 1), (50, 50.5), (95, 95.05), (99, 99.01), (100, 100)]
    )
    def test_interpolates_between_the_closest_samples(self, q, expected):
        values = list(range(100, 0, -1))  # unsorted on purpose

        assert percentile(values, q) == pytest.approx(expected)

    def test_single_sample_is_every_percentile(self):
        assert percentile([0.003], 99) == 0.003

    def test_no_samples_have_no_percentile(self):
        assert percentile([], 50) is None

    @pytest.mark.parametrize("q", [-1, 101])
    def test_percentile_outside_0_to_100_is_an_error(self, q):
        with pytest.raises(ValueError):
            percentile([1.0], q)

    @pytest.mark.parametrize(("seconds", "text"), [(0.0123, "12.3ms"), (None, "n/a")])
    def test_milliseconds(self, seconds, text):
        assert ms(seconds) == text


class TestTotals:
    def test_bot_stats_are_added_up(self):
        # Only a bot that started and never got online counts as never online.
        bots = [
            make_bot_view(
                locations_sent=10,
                errors=Counter(location_jump=1),
                rtt_s=[0.1],
                started=True,
                went_online=True,
            ),
            make_bot_view(
                locations_sent=5,
                errors=Counter(location_jump=2, rate_limited=1),
                closes=Counter({4000: 1}),
                reconnects=1,
                rtt_s=[0.2],
                started=True,
                stop_reason="closed 4403",
            ),
            make_bot_view(),  # stopped during the ramp-up, before it started
        ]

        totals = Totals.of(bots)

        assert totals.locations_sent == 15
        assert totals.errors == Counter(location_jump=3, rate_limited=1)
        assert totals.closes == Counter({4000: 1})
        assert totals.reconnects == 1
        assert sorted(totals.rtt_s) == [0.1, 0.2]
        assert totals.never_online == 1
        assert totals.stopped == Counter({"closed 4403": 1})


class TestStatusLine:
    def test_counts_bots_by_state(self):
        bots = [
            make_bot_view(),
            make_bot_view(on_break=True, status="offline"),
            make_bot_view(connected=False, stop_reason="closed 4409"),
        ]

        line = StatusLine().render(bots, [0.001], now=0)

        assert line.startswith("bots=3 connected=2 online=1 on_break=1 stopped=1 sent/s=0.0 ")

    def test_sent_per_second_is_over_the_last_interval(self):
        bot = make_bot_view(locations_sent=100)
        status = StatusLine()
        status.render([bot], [], now=0)
        bot.stats.locations_sent = 150

        line = status.render([bot], [], now=10)

        assert "sent/s=5.0 " in line

    def test_shows_rtt_and_loop_lag_percentiles(self):
        bot = make_bot_view(rtt_s=[0.002] * 99 + [0.5])

        line = StatusLine().render([bot], [0.001, 0.004], now=0)

        assert line.endswith("rtt_p95=2.0ms loop_lag_p99=4.0ms")


class TestSummary:
    def test_starts_with_the_conditions_of_the_run(self):
        report = summary(make_conditions(), [], [])

        text = "\n".join(report)
        assert "bots: 2, duration: 60s, ramp-up: 10s, flaky share: 0.05, seed: 7" in text
        assert "server: ws://nginx/rt/ws/driver, CPUs: 8" in text
        assert "from 2026-10-14 12:00:00 to 2026-10-14 12:01:00 UTC (60s)" in text

    def test_run_without_a_duration_says_so(self):
        report = summary(make_conditions(duration_s=None), [], [])

        assert "duration: until stopped" in report[1]

    def test_client_numbers(self):
        bots = [
            make_bot_view(
                locations_sent=600,
                rtt_s=[0.001, 0.002, 0.003],
                errors=Counter(location_jump=1),
                closes=Counter({1006: 2}),
                breaks=1,
            )
        ]

        text = "\n".join(summary(make_conditions(), bots, [0.0, 0.01]))

        assert "points sent: 600 (10.0/s)" in text
        assert "ping->pong RTT: p50=2.0ms p95=2.9ms p99=3.0ms max=3.0ms n=3" in text
        assert "event loop lag: p50=5.0ms p99=9.9ms max=10.0ms n=2" in text
        assert "server errors: location_jump=1" in text
        assert "closes by the server: 1006=2" in text
        assert "breaks: 1, drops: 0, silences: 0" in text

    def test_empty_run_has_no_division_by_zero(self):
        conditions = make_conditions(finished_at=STARTED)

        text = "\n".join(summary(conditions, [], []))

        assert "points sent: 0 (0.0/s)" in text
        assert "server errors: none" in text
        assert "p50=n/a" in text
