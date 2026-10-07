"""Server time, behind one object so tests can move it instead of sleeping.

Two clocks for two jobs:
- now_ms(): wall clock, unix milliseconds. For everything written to Redis or compared
  with values from Redis (last_seen, updated_at), and for point freshness, which is
  measured against the phone's own wall clock.
- monotonic(): seconds that never go back, whatever NTP does to the wall clock. For
  intervals that live only in this process (throttle, rate limit).
"""

import time


class Clock:
    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000

    def monotonic(self) -> float:
        return time.monotonic()
