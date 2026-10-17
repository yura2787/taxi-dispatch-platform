"""Server metrics of a run: parsing /rt/metrics, the change over the run, the
histogram_quantile estimate and the server part of the report."""

import math

import httpx
import pytest

from sim.server_metrics import (
    ServerMetricsUnavailable,
    Snapshot,
    fetch,
    histogram_quantile,
    parse,
    report,
)

INF = math.inf

METRICS_TEXT = """\
# HELP location_updates_total Driver locations by result.
# TYPE location_updates_total counter
location_updates_total{result="accepted"} 182.0
location_updates_total{result="out_of_zone"} 3.0
location_updates_total{result="rejected_jump"} 1.0
# HELP ws_closes_total WebSocket connections closed by the server.
# TYPE ws_closes_total counter
ws_closes_total{code="4409",role="driver"} 2.0
ws_closes_total{code="4408",role="unknown"} 5.0
# HELP location_processing_seconds Time from a parsed location to its Lua answer.
# TYPE location_processing_seconds histogram
location_processing_seconds_bucket{le="0.001"} 124.0
location_processing_seconds_bucket{le="0.005"} 181.0
location_processing_seconds_bucket{le="+Inf"} 186.0
location_processing_seconds_count 186.0
location_processing_seconds_sum 0.17
# HELP osrm_requests_total OSRM calls by outcome.
# TYPE osrm_requests_total counter
osrm_requests_total{endpoint="route",result="ok"} 7.0
"""


class TestParse:
    def test_reads_updates_driver_closes_and_buckets(self):
        snapshot = parse(METRICS_TEXT)

        assert snapshot.updates == {"accepted": 182, "out_of_zone": 3, "rejected_jump": 1}
        # Closes before auth (role unknown) are not about the drivers.
        assert snapshot.closes == {"4409": 2}
        assert snapshot.buckets == {0.001: 124, 0.005: 181, INF: 186}

    def test_accepted_includes_points_out_of_the_zone(self):
        snapshot = parse(METRICS_TEXT)

        assert snapshot.accepted == 185
        assert snapshot.refused == 1


class TestChange:
    def test_change_is_after_minus_before(self):
        before = Snapshot(updates={"accepted": 100}, closes={}, buckets={0.001: 90, INF: 100})
        after = Snapshot(
            updates={"accepted": 150, "rejected_stale": 2},
            closes={"4000": 1},
            buckets={0.001: 130, INF: 152},
        )

        change = after.minus(before)

        # rejected_stale appeared during the run: it counts from 0.
        assert change.updates == {"accepted": 50, "rejected_stale": 2}
        assert change.closes == {"4000": 1}
        assert change.buckets == {0.001: 40, INF: 52}

    @pytest.mark.parametrize(
        "after",
        [
            Snapshot(updates={"accepted": 20}),
            Snapshot(updates={}),  # a result the restarted server has not exported yet
            Snapshot(updates={"accepted": 100}, buckets={INF: 3}),
        ],
        ids=["counter_went_back", "label_gone", "bucket_went_back"],
    )
    def test_server_restarted_during_the_run_makes_the_change_unavailable(self, after):
        before = Snapshot(updates={"accepted": 100}, buckets={INF: 100})

        with pytest.raises(ServerMetricsUnavailable, match="restarted"):
            after.minus(before)


class TestHistogramQuantile:
    BUCKETS = {0.001: 50, 0.002: 90, 0.01: 100, INF: 100}

    @pytest.mark.parametrize(
        ("q", "expected"),
        [
            (0.5, 0.001),  # exactly at a bucket edge
            (0.25, 0.0005),  # inside the first bucket: from 0 up to its bound
            (0.7, 0.0015),  # halfway through the second bucket
            (0.95, 0.006),  # halfway through the third
            (1.0, 0.01),
        ],
    )
    def test_interpolates_inside_the_bucket_of_the_quantile(self, q, expected):
        assert histogram_quantile(q, self.BUCKETS) == pytest.approx(expected)

    def test_observations_beyond_the_largest_bucket_give_its_bound(self):
        buckets = {0.001: 10, 0.01: 20, INF: 100}

        assert histogram_quantile(0.99, buckets) == 0.01

    def test_empty_histogram_has_no_quantile(self):
        assert histogram_quantile(0.5, {0.001: 0, INF: 0}) is None
        assert histogram_quantile(0.5, {}) is None

    def test_quantile_of_zero_starts_where_the_observations_start(self):
        assert histogram_quantile(0.0, {0.001: 0, 0.01: 5, INF: 5}) == 0.001

    def test_histogram_with_only_the_inf_bucket_has_no_quantile(self):
        # Nothing to interpolate in; Prometheus gives NaN here.
        assert histogram_quantile(0.5, {INF: 5}) is None

    def test_buckets_that_seem_to_fall_are_taken_as_flat(self):
        # Two scrapes taken mid-update: 0.005 looks lower than 0.001.
        buckets = {0.001: 60, 0.005: 58, 0.01: 100, INF: 100}

        assert histogram_quantile(0.5, buckets) == pytest.approx(0.001 * 50 / 60)
        assert histogram_quantile(0.8, buckets) == pytest.approx(0.005 + 0.005 * 20 / 40)

    @pytest.mark.parametrize("q", [-0.1, 1.5, 99])
    def test_quantile_outside_0_to_1_is_an_error(self, q):
        with pytest.raises(ValueError):
            histogram_quantile(q, self.BUCKETS)


def serve(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestFetch:
    async def test_snapshot_of_the_metrics_endpoint(self):
        async with serve(lambda request: httpx.Response(200, text=METRICS_TEXT)) as http:
            snapshot = await fetch(http, "http://realtime:8001/rt/metrics")

        assert snapshot.accepted == 185

    @pytest.mark.parametrize(
        "response",
        [httpx.Response(403, text="forbidden"), httpx.Response(500, text="oops")],
        ids=["through_nginx", "server_error"],
    )
    async def test_error_response_is_unavailable(self, response):
        async with serve(lambda request: response) as http:
            with pytest.raises(ServerMetricsUnavailable):
                await fetch(http, "http://realtime:8001/rt/metrics")

    async def test_connection_error_is_unavailable(self):
        def refuse(request):
            raise httpx.ConnectError("refused")

        async with serve(refuse) as http:
            with pytest.raises(ServerMetricsUnavailable):
                await fetch(http, "http://realtime:8001/rt/metrics")

    async def test_url_without_a_scheme_is_unavailable(self):
        # A typo in REALTIME_METRICS_URL must not crash the run before it starts.
        async with httpx.AsyncClient() as http:
            with pytest.raises(ServerMetricsUnavailable):
                await fetch(http, "realtime:8001/rt/metrics")

    async def test_garbage_is_unavailable(self):
        garbage = 'location_updates_total{result="accepted" 1\n'

        async with serve(lambda request: httpx.Response(200, text=garbage)) as http:
            with pytest.raises(ServerMetricsUnavailable):
                await fetch(http, "http://realtime:8001/rt/metrics")


class TestReport:
    def test_server_part_of_the_summary(self):
        change = Snapshot(
            updates={"accepted": 998, "rejected_jump": 2, "throttled": 7},
            closes={"4409": 1, "1008": 0},
            buckets={0.001: 500, 0.005: 990, 0.01: 1000, INF: 1000},
        )

        lines = report(change)

        assert lines[0].startswith("--- server")
        assert lines[1] == "location updates: accepted=998, rejected_jump=2, throttled=7"
        # Throttled points are neither accepted nor refused: not in the share.
        assert lines[2] == "refused: 2 of 1000 (0.200%), throttled: 7"
        assert lines[3] == (
            "location_processing_seconds (bucket estimate): p50=1.0ms p95=4.7ms p99=5.0ms"
        )
        assert lines[4] == "closes of drivers by the server: 4409=1"

    def test_quantile_beyond_the_largest_bucket_says_so(self):
        # 2 of 100 points took longer than the largest bucket (1 s): p99 is not 1 s.
        change = Snapshot(updates={"accepted": 100}, buckets={0.5: 90, 1.0: 98, INF: 100})

        lines = report(change)

        assert lines[3] == (
            "location_processing_seconds (bucket estimate): p50=277.8ms p95=812.5ms p99=>1000.0ms"
        )

    def test_run_without_points(self):
        lines = report(Snapshot())

        assert lines[1] == "location updates: none"
        assert lines[2] == "refused: 0 of 0 (0.000%), throttled: 0"
        assert "p50=n/a" in lines[3]
        assert lines[4] == "closes of drivers by the server: none"
