"""Latency samples in fixed memory: log buckets, like an HDR histogram.

`make sim` without --duration is the live city for the dispatcher's map and runs for
hours; keeping every RTT would grow without bound, and every status line would sort
it all in the event loop the bots run in. Here a sample costs O(1) to record, the
memory depends only on the range of the values (about 116 buckets per tenfold), and
recorders of many bots merge into one.

The price is precision: a percentile is taken from the middle of its bucket, within
PRECISION of the exact value from SMALLEST_S up. Count and max stay exact.
"""

import math
from collections import Counter

# Every bucket is RATIO times wider than the previous one; its geometric middle is
# within sqrt(RATIO) - 1 of any value in it.
RATIO = 1.02
PRECISION = math.sqrt(RATIO) - 1  # 0.995 %

# The lower edge of the first bucket: smaller (positive) values count as about it.
# A quiet event loop is late by a few hundred nanoseconds, so far below that.
SMALLEST_S = 1e-9

_LOG_RATIO = math.log(RATIO)


class LatencyRecorder:
    """Samples in seconds: count, max and the percentiles over them."""

    def __init__(self) -> None:
        # Bucket index -> samples in it; 0 s is kept apart, exactly.
        self._buckets: Counter[int] = Counter()
        self._zeros = 0
        self.count = 0
        # Exact; unknown (None) for an interval, see minus().
        self.max: float | None = None

    @property
    def bucket_count(self) -> int:
        """What the memory grows with: the buckets in use, the zeros included."""
        return len(self._buckets) + int(self._zeros > 0)

    def record(self, seconds: float) -> None:
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError(f"latency must be a finite number >= 0, got {seconds}")
        if seconds == 0:
            self._zeros += 1
        else:
            self._buckets[_bucket_of(seconds)] += 1
        self.count += 1
        if self.max is None or seconds > self.max:
            self.max = seconds

    def merge(self, other: "LatencyRecorder") -> None:
        """Add the samples of another recorder (e.g. of another bot) to this one."""
        self._buckets.update(other._buckets)
        self._zeros += other._zeros
        self.count += other.count
        if other.max is not None and (self.max is None or other.max > self.max):
            self.max = other.max

    def copy(self) -> "LatencyRecorder":
        """A recorder that keeps these samples while this one goes on recording."""
        recorder = LatencyRecorder()
        recorder.merge(self)
        return recorder

    def minus(self, earlier: "LatencyRecorder") -> "LatencyRecorder":
        """The samples recorded since `earlier`, a copy of this recorder taken before.
        Their max is unknown: only the counts can be taken apart."""
        later_than_earlier = (
            self.count >= earlier.count
            and self._zeros >= earlier._zeros
            and all(self._buckets[index] >= n for index, n in earlier._buckets.items())
        )
        if not later_than_earlier:
            raise ValueError("not an earlier copy of this recorder: its samples are not all here")
        interval = LatencyRecorder()
        interval._buckets = self._buckets - earlier._buckets
        interval._zeros = self._zeros - earlier._zeros
        interval.count = self.count - earlier.count
        return interval

    def percentile(self, q: float) -> float | None:
        """The q-th percentile (0..100), interpolated between the closest samples
        ("inclusive", as numpy's default), each taken as the middle of its bucket.
        None for no samples."""
        if not 0 <= q <= 100:
            raise ValueError(f"percentile must be within 0..100, got {q}")
        if self.count == 0:
            return None
        position = (self.count - 1) * q / 100
        lower = int(position)
        upper = min(lower + 1, self.count - 1)
        low, high = self._values_at(lower, upper)
        return low + (high - low) * (position - lower)

    def _values_at(self, lower: int, upper: int) -> tuple[float, float]:
        """The estimates of the lower-th and upper-th smallest samples (0-based,
        lower <= upper < count), in one walk over the buckets."""
        values: list[float] = []
        ranks = [rank for rank in (lower, upper) if rank >= self._zeros]
        values += [0.0] * (2 - len(ranks))
        seen = self._zeros
        for index in sorted(self._buckets):
            seen += self._buckets[index]
            while ranks and ranks[0] < seen:
                ranks.pop(0)
                values.append(_middle_of(index))
            if not ranks:
                break
        return values[0], values[1]


def _bucket_of(seconds: float) -> int:
    return max(math.floor(math.log(seconds / SMALLEST_S) / _LOG_RATIO), 0)


def _middle_of(index: int) -> float:
    return SMALLEST_S * RATIO ** (index + 0.5)
