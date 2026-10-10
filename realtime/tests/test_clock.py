"""The real server clock: wall time in unix milliseconds and a monotonic timer."""

import time

from app.clock import Clock


def test_now_ms_is_the_wall_clock_in_milliseconds():
    before = time.time() * 1000

    now_ms = Clock().now_ms()

    assert isinstance(now_ms, int)
    assert before - 1 <= now_ms <= time.time() * 1000 + 1


def test_monotonic_never_goes_back():
    clock = Clock()

    first = clock.monotonic()

    assert clock.monotonic() >= first
