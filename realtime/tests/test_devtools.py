"""Developer command `seed-drivers`: what the profiles look like, idempotency and
the DEV_AUTH guard."""

import asyncio
import re

import pytest

from app import config, redis_keys
from app.devtools import build_profile, main, seed_drivers
from app.services.drivers import DriverStore


@pytest.fixture
def dev_auth(monkeypatch):
    monkeypatch.setattr(config, "DEV_AUTH", True)


async def profile_keys(redis) -> list[bytes]:
    return [key async for key in redis.scan_iter(match=redis_keys.driver_profile("*"))]


class TestBuildProfile:
    def test_profile_is_the_same_for_the_same_id(self):
        assert build_profile(7) == build_profile(7)

    def test_profiles_differ_between_drivers(self):
        assert len({build_profile(driver_id)["plate"] for driver_id in range(1, 101)}) > 90

    def test_about_30_percent_of_drivers_are_comfort(self):
        tariffs = [build_profile(driver_id)["tariff"] for driver_id in range(1, 1001)]

        assert 0.25 < tariffs.count("comfort") / len(tariffs) < 0.35
        assert set(tariffs) == set(config.TARIFFS)

    @pytest.mark.parametrize("driver_id", range(1, 51))
    def test_profile_has_plausible_values(self, driver_id):
        profile = build_profile(driver_id)

        assert profile["eligible"] == 1
        assert 4.5 <= profile["rating"] <= 5.0
        assert re.fullmatch(r"CE \d{4} [ABCEHIKMOPTX]{2}", profile["plate"])
        assert len(profile["name"].split()) == 2


class TestSeedDrivers:
    async def test_writes_profiles_that_may_go_online(self, redis):
        await seed_drivers(redis, 5)

        store = DriverStore(redis)
        for driver_id in range(1, 6):
            assert (await store.get_profile(driver_id)).may_go_online
        assert len(await profile_keys(redis)) == 5

    async def test_seeding_twice_changes_nothing(self, redis):
        await seed_drivers(redis, 5)
        before = await redis.hgetall(redis_keys.driver_profile(3))

        await seed_drivers(redis, 5)

        assert await redis.hgetall(redis_keys.driver_profile(3)) == before
        assert len(await profile_keys(redis)) == 5

    async def test_profiles_have_no_ttl(self, redis):
        await seed_drivers(redis, 1)

        assert await redis.ttl(redis_keys.driver_profile(1)) == -1


class TestMain:
    # main() runs its own event loop, so it is called from a worker thread.

    async def test_seeds_the_requested_number_of_drivers(self, redis, dev_auth, capsys):
        code = await asyncio.to_thread(main, ["seed-drivers", "--count", "3"])

        assert code == 0
        assert len(await profile_keys(redis)) == 3
        assert "Seeded 3 driver profiles." in capsys.readouterr().out

    async def test_refuses_without_dev_auth(self, redis, monkeypatch, capsys):
        monkeypatch.setattr(config, "DEV_AUTH", False)

        code = await asyncio.to_thread(main, ["seed-drivers", "--count", "3"])

        assert code == 1
        assert "DEV_AUTH=1" in capsys.readouterr().err
        assert await profile_keys(redis) == []

    @pytest.mark.parametrize("count", ["0", "-5", "many"])
    def test_rejects_a_count_that_is_not_a_positive_number(self, count, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(["seed-drivers", "--count", count])

        assert exc_info.value.code == 2
        assert "--count" in capsys.readouterr().err

    def test_requires_a_command(self, capsys):
        with pytest.raises(SystemExit):
            main([])
