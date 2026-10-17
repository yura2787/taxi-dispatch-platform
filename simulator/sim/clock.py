"""Time of the simulator, behind one object so tests run on virtual time.

- now_ms(): wall clock, unix milliseconds. Only for `ts` of a location: the server
  compares it with its own wall clock.
- monotonic(): seconds that never go back. For schedules and measured latencies.
- sleep(): the waits of the bots and the runner go through it (ticks, pings, breaks,
  backoff, the handshake and stop deadlines), so a test can skip minutes of driving
  in milliseconds. Not all waits: the OSRM and server metrics request timeouts and
  the websockets connect and close timeouts run on the event loop's own clock.
"""

import asyncio
import time


class Clock:
    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
