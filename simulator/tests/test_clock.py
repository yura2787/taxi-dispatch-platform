"""The real clock, and the virtual one (FakeClock) that the tests of the bots and the
runner rely on."""

import asyncio

import pytest

from sim.clock import Clock


class TestClock:
    def test_wall_clock_is_unix_milliseconds(self):
        # 2025-01-01 .. 2100-01-01: milliseconds, not seconds or nanoseconds.
        assert 1_735_689_600_000 < Clock().now_ms() < 4_102_444_800_000

    def test_monotonic_clock_does_not_go_back(self):
        clock = Clock()

        first = clock.monotonic()

        assert clock.monotonic() >= first

    async def test_sleep_waits(self):
        clock = Clock()
        start = clock.monotonic()

        await clock.sleep(0.01)

        # The loop may wake a hair early (its clock resolution).
        assert clock.monotonic() - start >= 0.009


class TestFakeClock:
    async def test_sleep_moves_both_clocks_by_the_delay(self, clock):
        wall, mono = clock.now_ms(), clock.monotonic()

        await clock.sleep(2.5)

        assert clock.now_ms() == wall + 2500
        assert clock.monotonic() == mono + 2.5

    async def test_advance_moves_the_time_without_sleeping(self, clock):
        clock.advance(10)

        assert clock.elapsed == 10
        assert clock.sleeps == []

    async def test_requested_delays_are_recorded(self, clock):
        await clock.sleep(1)
        await clock.sleep(4)

        assert clock.sleeps == [1, 4]

    async def test_concurrent_sleepers_wake_in_deadline_order(self, clock):
        woke: list[tuple[str, float]] = []

        async def every(name: str, interval: float, times: int) -> None:
            for _ in range(times):
                await clock.sleep(interval)
                woke.append((name, clock.elapsed))

        await asyncio.gather(every("tick", 2, 10), every("ping", 19, 1))

        # The ping wakes between the 9th and the 10th tick, not right after the first.
        assert woke[8:] == [("tick", 18), ("ping", 19), ("tick", 20)]

    async def test_many_concurrent_sleepers_each_see_their_own_schedule(self, clock):
        seen: dict[float, list[float]] = {}

        async def every(interval: float) -> None:
            for _ in range(5):
                await clock.sleep(interval)
                seen.setdefault(interval, []).append(clock.elapsed)

        intervals = [0.5 * n for n in range(1, 21)]
        await asyncio.gather(*(every(interval) for interval in intervals))

        for interval in intervals:
            assert seen[interval] == [interval * n for n in range(1, 6)]

    @pytest.mark.parametrize("offset_rounds", [0, 5, 10, 15, 19])
    async def test_woken_sleeper_reacts_before_the_time_moves(self, clock, offset_rounds):
        # The bot falls asleep at some phase of the other sleeper's waiting and needs
        # a few loop rounds to react once woken (parse a reply, send a point).
        reacted_at: list[float] = []

        async def bot() -> None:
            for _ in range(offset_rounds):
                await asyncio.sleep(0)
            await clock.sleep(1)
            for _ in range(10):
                await asyncio.sleep(0)
            reacted_at.append(clock.elapsed)

        await asyncio.gather(clock.sleep(5), bot())

        assert reacted_at == [1]

    async def test_ready_task_runs_before_the_time_moves(self, clock):
        replies: asyncio.Queue[float] = asyncio.Queue()

        async def server() -> None:
            # Answers through a few hops, like a reply that has to be parsed first.
            for _ in range(3):
                await asyncio.sleep(0)
            replies.put_nowait(clock.elapsed)

        task = asyncio.create_task(server())
        await clock.sleep(5)
        await task

        assert replies.get_nowait() == 0

    async def test_cancelled_sleeper_does_not_hold_back_the_others(self, clock):
        long_sleep = asyncio.create_task(clock.sleep(100))
        await asyncio.sleep(0)

        long_sleep.cancel()
        await asyncio.gather(long_sleep, return_exceptions=True)
        await clock.sleep(1)

        assert clock.elapsed == 1

    async def test_negative_delay_does_not_move_the_time_back(self, clock):
        clock.advance(5)

        await clock.sleep(-1)

        assert clock.elapsed == 5
