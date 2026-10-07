# All Redis keys live in this module and nowhere else.
# Add a function here for every new key instead of building key strings inline.


def driver_state(driver_id: int | str) -> str:
    """HASH: live state of a driver (status, position, connection), owned by realtime.

    driver_id may be "*" to build a SCAN pattern over all drivers.
    """
    return f"driver:{driver_id}:state"


def driver_profile(driver_id: int) -> str:
    """HASH: who the driver is and whether they may work (eligible, tariff, car)."""
    return f"driver:{driver_id}:profile"


def drivers_last_seen() -> str:
    """ZSET: online drivers by the server time (ms) of their last accepted point."""
    return "drivers:last_seen"


def drivers_geo(tariff: str) -> str:
    """GEO: drivers of this tariff who can be offered an order right now."""
    return f"drivers:geo:{tariff}"
