"""LatencyRecorder: percentiles from log buckets within 1 % of the exact ones, fixed
memory however many samples, merging bots and taking the last interval."""

import math
import random

import pytest

from sim.latency import PRECISION, RATIO, SMALLEST_S, LatencyRecorder
from tests.factories import make_latency as recorded


def exact_percentile(values: list[float], q: float) -> float:
    """The reference: linear interpolation between the closest samples ("inclusive",
    as numpy's default)."""
    ordered = sorted(values)
    position = (len(ordered) - 1) * q / 100
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def samples(kind: str, n: int = 20_000) -> list[float]:
    rng = random.Random(kind)
    if kind == "rtt":  # a few ms with a long tail
        return [rng.lognormvariate(math.log(0.003), 0.6) for _ in range(n)]
    if kind == "uniform":
        return [rng.uniform(0.0005, 0.5) for _ in range(n)]
    # Loop lag: mostly exactly 0, now and then late by up to 100 ms.
    return [0.0 if rng.random() < 0.7 else rng.uniform(0, 0.1) for _ in range(n)]


QS = (0, 1, 50, 90, 95, 99, 99.9, 100)


class TestPercentiles:
    @pytest.mark.parametrize("kind", ["rtt", "uniform", "loop_lag"])
    @pytest.mark.parametrize("q", QS)
    def test_within_the_precision_of_the_exact_percentile(self, kind, q):
        values = samples(kind)

        estimate = recorded(values).percentile(q)

        exact = exact_percentile(values, q)
        assert estimate == pytest.approx(exact, rel=PRECISION, abs=0)

    def test_precision_is_at_most_one_percent(self):
        assert PRECISION <= 0.01

    @pytest.mark.parametrize("k", [0, 1, 100, 500, 1000, 1500])
    def test_values_right_on_a_bucket_edge_are_within_the_precision(self, k):
        # The worst place: the bottom of a bucket, farthest from its middle.
        value = SMALLEST_S * RATIO**k

        estimate = recorded([value]).percentile(50)

        assert estimate == pytest.approx(value, rel=PRECISION + 1e-12, abs=0)

    @pytest.mark.parametrize("value", [3e-9, 2e-7, 5e-6, 120.0, 3600.0])
    def test_from_nanoseconds_to_an_hour(self, value):
        # A quiet loop is late by a few hundred ns; a stalled ping by minutes.
        assert recorded([value]).percentile(50) == pytest.approx(value, rel=PRECISION, abs=0)

    @pytest.mark.parametrize("q", [0, 25, 50, 75, 100])
    def test_two_samples_a_decade_apart(self, q):
        values = [0.001, 0.01]

        estimate = recorded(values).percentile(q)

        assert estimate == pytest.approx(exact_percentile(values, q), rel=PRECISION, abs=0)

    def test_single_sample_is_every_percentile(self):
        recorder = recorded([0.003])

        assert recorder.percentile(1) == recorder.percentile(99)
        assert recorder.percentile(99) == pytest.approx(0.003, rel=PRECISION)

    @pytest.mark.parametrize("k", [100, 1000, 2000])
    def test_no_percentile_exceeds_the_exact_max(self, k):
        # A sample right on a bucket's lower edge sits below the bucket's middle: the
        # report must not say "p99=10.4ms max=10.2ms".
        on_the_edge = SMALLEST_S * RATIO**k
        recorder = recorded([on_the_edge / 2, on_the_edge])

        percentiles = [recorder.percentile(q) for q in (50, 99, 100)]

        assert all(p <= recorder.max for p in percentiles)
        assert recorder.percentile(100) == recorder.max

    def test_max_above_its_bucket_middle_stays_within_the_precision(self):
        # Just below the next bucket edge: the middle is lower than the sample.
        near_the_top = SMALLEST_S * RATIO**1001 * 0.999
        recorder = recorded([near_the_top])

        top = recorder.percentile(100)

        assert top <= recorder.max
        assert top == pytest.approx(recorder.max, rel=PRECISION)

    def test_percentiles_of_an_interval_are_not_capped(self):
        # minus() gives no max: nothing to cap at, the bucket estimate stands.
        earlier = recorded([0.001])
        later = recorded([0.001, 0.010])

        interval = later.minus(earlier)

        assert interval.max is None
        assert interval.percentile(100) == pytest.approx(0.010, rel=PRECISION)

    def test_zero_is_kept_exactly(self):
        # The loop lag probe is often right on time.
        assert recorded([0.0, 0.0, 0.0]).percentile(99) == 0.0

    def test_no_samples_have_no_percentile(self):
        recorder = LatencyRecorder()

        assert recorder.percentile(50) is None
        assert (recorder.count, recorder.max) == (0, None)

    def test_max_and_count_are_exact(self):
        recorder = recorded([0.001, 0.25, 0.002])

        assert (recorder.count, recorder.max) == (3, 0.25)

    @pytest.mark.parametrize("q", [-1, 101])
    def test_percentile_outside_0_to_100_is_an_error(self, q):
        with pytest.raises(ValueError):
            recorded([1.0]).percentile(q)

    @pytest.mark.parametrize("value", [-0.001, math.inf, math.nan])
    def test_negative_or_not_finite_latency_is_an_error(self, value):
        with pytest.raises(ValueError):
            LatencyRecorder().record(value)


class TestMemory:
    def test_buckets_depend_on_the_range_not_on_the_samples(self):
        # Hours of a live city: the samples pile up, the buckets do not.
        recorder = LatencyRecorder()
        rng = random.Random(1)

        for _ in range(300_000):
            recorder.record(rng.uniform(0.001, 1.0))

        # 1 ms .. 1 s spans log(1000) / log(RATIO) buckets, however many samples.
        assert recorder.bucket_count <= math.ceil(math.log(1000) / math.log(RATIO)) + 1
        assert recorder.count == 300_000


class TestCombining:
    def test_merged_recorders_equal_one_recorder_of_all_samples(self):
        first, second = samples("rtt")[:5000], samples("uniform")[:5000]
        merged = recorded(first)

        merged.merge(recorded(second))

        together = recorded(first + second)
        assert [merged.percentile(q) for q in QS] == [together.percentile(q) for q in QS]
        assert (merged.count, merged.max) == (together.count, together.max)

    def test_last_interval_is_the_later_minus_the_earlier(self):
        before, during = samples("uniform")[:3000], samples("rtt")[:3000]
        recorder = recorded(before)
        earlier = recorder.copy()
        for value in during:
            recorder.record(value)

        interval = recorder.minus(earlier)

        alone = recorded(during)
        # The same buckets; only the interval knows no max to cap its percentiles at,
        # so it is never lower, and equal once capped.
        assert all(interval.percentile(q) >= alone.percentile(q) for q in QS)
        capped = [min(interval.percentile(q), alone.max) for q in QS]
        assert capped == [alone.percentile(q) for q in QS]
        assert interval.count == len(during)

    @pytest.mark.parametrize(
        "earlier", [[0.5, 0.5], [0.0], [0.001, 0.001, 0.001]], ids=["bucket", "zero", "count"]
    )
    def test_minus_something_that_is_not_an_earlier_copy_is_an_error(self, earlier):
        # E.g. stats reset in between: the interval would be nonsense, not an estimate.
        with pytest.raises(ValueError):
            recorded([0.001, 0.5]).minus(recorded(earlier))

    def test_copy_does_not_change_with_the_original(self):
        recorder = recorded([0.001])
        copy = recorder.copy()

        recorder.record(0.5)

        assert (copy.count, copy.max) == (1, 0.001)
