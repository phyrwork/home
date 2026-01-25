from datetime import datetime, timedelta

from homeassistant.util import dt as dt_util

from custom_components.energy_cost_forecast.helpers import (
    ProfileSegment,
    RateWindow,
    cost_profile,
    parse_duration,
    parse_rates,
)


def test_parse_duration():
    assert parse_duration("1h30m") == timedelta(hours=1, minutes=30)
    assert parse_duration("45m") == timedelta(minutes=45)
    assert parse_duration("") is None
    assert parse_duration("1x") is None


def test_parse_rates_sorts_and_reads_values():
    raw = [
        {
            "start": "2024-01-01T01:00:00+00:00",
            "end": "2024-01-01T02:00:00+00:00",
            "value": 0.25,
        },
        {
            "start": "2024-01-01T00:00:00+00:00",
            "end": "2024-01-01T01:00:00+00:00",
            "value_inc_vat": 0.2,
        },
    ]
    rates = parse_rates(raw)
    assert len(rates) == 2
    assert rates[0].start == dt_util.as_utc(
        dt_util.parse_datetime("2024-01-01T00:00:00+00:00")
    )
    assert rates[0].value == 0.2
    assert rates[1].value == 0.25


def test_cost_profile():
    start = dt_util.as_utc(datetime(2024, 1, 1, 0, 0, 0))
    rates = [
        RateWindow(
            start=start,
            end=start + timedelta(hours=1),
            value=0.2,
        )
    ]
    profile = [ProfileSegment(duration=timedelta(hours=1), power_kw=1.0)]
    assert cost_profile(start, rates, profile) == 0.2
