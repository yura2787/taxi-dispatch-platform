"""Relations between config constants that must hold whatever their values are."""

from app import config

# Phones send a point every 2-3 s.
FASTEST_PHONE_INTERVAL_S = 2
SLOWEST_PHONE_INTERVAL_S = 3


def test_jump_filter_recovers_before_the_reaper_takes_the_driver_offline():
    assert config.LOCATION_JUMP_RESET_S < config.DRIVER_SILENCE_S


def test_clock_model_resets_before_the_reaper_takes_the_driver_offline():
    # The reset happens on the point after LOCATION_CLOCK_RESET_AFTER rejections.
    points_until_reset = config.LOCATION_CLOCK_RESET_AFTER + 1

    assert points_until_reset * SLOWEST_PHONE_INTERVAL_S < config.DRIVER_SILENCE_S


def test_throttle_lets_every_phone_point_through():
    assert config.LOCATION_MIN_INTERVAL_S < FASTEST_PHONE_INTERVAL_S


def test_service_area_is_not_empty():
    area = config.SERVICE_AREA

    assert area.min_lat < area.max_lat
    assert area.min_lon < area.max_lon
