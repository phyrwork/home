"""Tests for the static house-load forecast."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from custom_components.house_battery_control import load
from custom_components.house_battery_control.interval import TimeInterval

HOUR = timedelta(hours=1)
LONDON = ZoneInfo("Europe/London")


def test_forecast_selects_local_weekday_hours_and_covers_partial_boundary() -> None:
    now = datetime(2026, 7, 6, 8, 30, tzinfo=UTC)

    result = load.forecast(
        now=now,
        horizon_end=now + HOUR,
        timezone=LONDON,
    )

    assert [item.interval for item in result] == [
        TimeInterval(
            datetime(2026, 7, 6, 8, tzinfo=UTC),
            datetime(2026, 7, 6, 9, tzinfo=UTC),
        ),
        TimeInterval(
            datetime(2026, 7, 6, 9, tzinfo=UTC),
            datetime(2026, 7, 6, 10, tzinfo=UTC),
        ),
    ]
    assert [item.energy_kwh for item in result] == [
        Decimal("0.2768"),
        Decimal("0.2585"),
    ]


def test_forecast_selects_weekend_profile() -> None:
    now = datetime(2026, 7, 4, 8, tzinfo=UTC)

    result = load.forecast(
        now=now,
        horizon_end=now + HOUR,
        timezone=LONDON,
    )

    assert result[0].energy_kwh == Decimal("0.3296")


def test_profiles_retain_analyzed_daily_energy() -> None:
    weekday = load.forecast(
        now=datetime(2026, 7, 6, tzinfo=UTC),
        horizon_end=datetime(2026, 7, 7, tzinfo=UTC),
        timezone=UTC,
    )
    weekend = load.forecast(
        now=datetime(2026, 7, 4, tzinfo=UTC),
        horizon_end=datetime(2026, 7, 5, tzinfo=UTC),
        timezone=UTC,
    )

    assert sum((item.energy_kwh for item in weekday), Decimal()) == Decimal(
        "6.2770"
    )
    assert sum((item.energy_kwh for item in weekend), Decimal()) == Decimal(
        "6.7570"
    )
