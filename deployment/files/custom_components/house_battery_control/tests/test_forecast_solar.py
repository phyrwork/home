from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from custom_components.house_battery_control.dependencies import forecast_solar
from custom_components.house_battery_control.interval import TimeInterval

NOW = datetime(2026, 7, 4, tzinfo=UTC)


def test_maps_home_assistant_forecast_response() -> None:
    result = forecast_solar.to_energy_intervals(
        {
            "wh_hours": {
                NOW.isoformat(): 500,
                (NOW + timedelta(hours=1)).isoformat(): 750.5,
            }
        }
    )

    assert result[0].interval == TimeInterval(NOW, NOW + timedelta(hours=1))
    assert result[0].energy_kwh == Decimal("0.5")
    assert result[1].energy_kwh == Decimal("0.7505")


def test_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        forecast_solar.to_energy_intervals(
            {
                "wh_hours": {
                    "2026-07-04T00:00:00": 500,
                    "2026-07-04T01:00:00": 500,
                }
            }
        )
