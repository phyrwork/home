from datetime import datetime, timedelta
from pathlib import Path

import pytest
from homeassistant.util import dt as dt_util

from custom_components.energy_cost_forecast.helpers import (
    ProfileSegment,
    RateWindow,
    candidate_starts,
    cost_profile,
    cost_profile_with_export_offset,
    load_profile,
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


def test_cost_profile_with_export_offset():
    start = dt_util.as_utc(datetime(2024, 1, 1, 0, 0, 0))
    rates = [
        RateWindow(
            start=start,
            end=start + timedelta(hours=1),
            value=0.2,
        )
    ]
    profile = [ProfileSegment(duration=timedelta(hours=1), power_kw=1.0)]
    cost = cost_profile_with_export_offset(
        start,
        rates,
        profile,
        export_power_kw=0.5,
        export_rate=0.05,
    )
    assert cost == 0.125


def test_candidate_starts_fixed_interval():
    now = dt_util.as_utc(datetime(2024, 1, 1, 0, 5, 0))
    rates = [
        RateWindow(
            start=dt_util.as_utc(datetime(2024, 1, 1, 0, 0, 0)),
            end=dt_util.as_utc(datetime(2024, 1, 1, 1, 0, 0)),
            value=0.2,
        )
    ]
    starts = candidate_starts(rates, now, None, True, 15)
    assert starts[0] == now.replace(microsecond=0)
    assert starts[-1] < rates[-1].end
    assert rates[-1].end - starts[-1] < timedelta(minutes=15)


@pytest.mark.asyncio
async def test_load_profile_inline(hass):
    segments, profile_input, error = await load_profile(
        hass,
        [[1500, "30m"], [200, "90m"]],
        None,
        None,
    )
    assert error is None
    assert profile_input is not None
    assert segments and len(segments) == 2


@pytest.mark.asyncio
async def test_load_profile_sensor_missing(hass):
    segments, profile_input, error = await load_profile(
        hass,
        None,
        None,
        "sensor.missing_profile",
    )
    assert segments is None
    assert profile_input is None
    assert error == "missing_profile_sensor"


@pytest.mark.asyncio
async def test_load_profile_file_invalid(hass, tmp_path: Path):
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text("not-a-list", encoding="utf-8")
    segments, profile_input, error = await load_profile(
        hass,
        None,
        str(profile_path),
        None,
    )
    assert segments is None
    assert profile_input == str(profile_path)
    assert error == "invalid_profile"
