"""Developer commands for local data.

    python -m app.devtools seed-drivers --count 100 [--first-id 1]

Driver profiles come from Django from stage 2 on; until then this command writes
them to Redis so drivers can connect with dev tokens. It refuses to run without
DEV_AUTH=1: profiles of made-up drivers have no place outside development.
"""

import argparse
import asyncio
import random
import sys

from redis.asyncio import Redis

from app import config, redis_keys

# The platform is in English: names in the official Ukrainian transliteration.
FIRST_NAMES = ["Taras", "Andrii", "Oleh", "Ivan", "Dmytro", "Vasyl", "Olena", "Mariia", "Yurii"]
LAST_NAMES = ["Melnyk", "Koval", "Boiko", "Tkachuk", "Shevchuk", "Kravchuk", "Savchuk", "Hutsuliak"]
CARS = {
    "economy": ["Skoda Octavia", "Toyota Corolla", "Hyundai Elantra", "Renault Logan", "Kia Rio"],
    "comfort": ["Toyota Camry", "Skoda Superb", "Kia K5", "Volkswagen Passat", "Hyundai Sonata"],
}
COLORS = ["white", "black", "grey", "silver", "blue", "red"]
# Ukrainian plates use only the Latin letters that look the same in Cyrillic.
PLATE_LETTERS = "ABCEHIKMOPTX"
# Chernivtsi region code.
PLATE_REGION = "CE"
COMFORT_SHARE = 0.3


def build_profile(driver_id: int) -> dict[str, str | int | float]:
    """A plausible profile, always the same for the same id.

    Each driver has its own random generator seeded with the id, so driver 7 is the
    same whether 10 or 1000 drivers are seeded, and reruns change nothing.
    """
    rng = random.Random(driver_id)
    tariff = "comfort" if rng.random() < COMFORT_SHARE else "economy"
    letters = "".join(rng.choices(PLATE_LETTERS, k=2))
    return {
        "eligible": 1,
        "tariff": tariff,
        "name": f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}",
        "car": rng.choice(CARS[tariff]),
        "color": rng.choice(COLORS),
        "plate": f"{PLATE_REGION} {rng.randint(0, 9999):04d} {letters}",
        "rating": round(rng.uniform(4.5, 5.0), 2),
    }


async def seed_drivers(redis: Redis, count: int, *, first_id: int = 1) -> None:
    """Write profiles of drivers first_id..first_id + count - 1.

    Existing profiles are overwritten with the same data. Separate id ranges let two
    simulator runs coexist instead of pushing each other's drivers out with 4409.
    """
    async with redis.pipeline(transaction=False) as pipe:
        for driver_id in range(first_id, first_id + count):
            pipe.hset(redis_keys.driver_profile(driver_id), mapping=build_profile(driver_id))
        await pipe.execute()


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {number}")
    return number


async def _seed(count: int, first_id: int) -> None:
    redis = Redis.from_url(config.REDIS_URL)
    try:
        await seed_drivers(redis, count, first_id=first_id)
    finally:
        await redis.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.devtools")
    commands = parser.add_subparsers(dest="command", required=True)
    seed = commands.add_parser("seed-drivers", help="create dev driver profiles")
    seed.add_argument("--count", type=_positive_int, default=100, help="number of drivers")
    seed.add_argument("--first-id", type=_positive_int, default=1, help="id of the first driver")
    args = parser.parse_args(argv)

    if not config.DEV_AUTH:
        print("Refusing to seed: dev data is allowed only with DEV_AUTH=1.", file=sys.stderr)
        return 1
    asyncio.run(_seed(args.count, args.first_id))
    last_id = args.first_id + args.count - 1
    print(f"Seeded {args.count} driver profiles (ids {args.first_id}..{last_id}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
