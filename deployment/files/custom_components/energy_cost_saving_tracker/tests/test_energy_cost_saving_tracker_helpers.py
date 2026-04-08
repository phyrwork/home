from datetime import datetime, timedelta

from custom_components.energy_cost_saving_tracker.helpers import (
    integrate_left,
    parse_float,
    savings_rate,
)


def test_parse_float_handles_missing_values():
    assert parse_float("unknown") is None
    assert parse_float("1.25") == 1.25


def test_savings_rate_allows_negative_values():
    assert savings_rate(0.40, 0.20) == -0.20


def test_integrate_left_underestimates_on_delayed_updates():
    started = datetime(2024, 1, 1, 0, 0, 0)
    ended = started + timedelta(minutes=30)
    assert integrate_left(1000, started, ended, 5) == 0.5

