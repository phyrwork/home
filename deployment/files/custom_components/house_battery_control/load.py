"""Static non-EV house-load forecast."""

from datetime import datetime, timedelta, tzinfo
from decimal import Decimal

from .energy import EnergyInterval
from .interval import TimeInterval

_HOUR = timedelta(hours=1)

# Methodology:
#
# - Source: Home Assistant hourly long-term statistics from 29 April through
#   23 June 2026, the eight weeks preceding the identified holiday.
# - For each hour:
#     non-EV load = solar generation + grid import - grid export - EV charging
# - EV energy is reconstructed from the charger's hourly mean power. Historical
#   entity names are joined where the charger entity was renamed.
# - Incomplete days and days containing materially negative derived load are
#   discarded as meter-timing/data-quality failures.
# - Remaining days are split into weekday and weekend groups.
# - The median for each local clock hour forms the shape for its group.
# - Each shape is scaled to its group's median complete-day energy. This retains
#   loads whose timing varies between days and would otherwise disappear from
#   independent hourly medians.
#
# The resulting daily totals are 6.277 kWh on weekdays and 6.757 kWh on
# weekends. Re-run this analysis no more than weekly if the profiles are later
# refreshed; planner updates should only read the flattened values below.
_WEEKDAY_KWH = (
    Decimal("0.2546"),
    Decimal("0.2546"),
    Decimal("0.2546"),
    Decimal("0.2572"),
    Decimal("0.2520"),
    Decimal("0.2494"),
    Decimal("0.1436"),
    Decimal("0.2520"),
    Decimal("0.2468"),
    Decimal("0.2768"),
    Decimal("0.2585"),
    Decimal("0.2651"),
    Decimal("0.2677"),
    Decimal("0.2559"),
    Decimal("0.2703"),
    Decimal("0.2742"),
    Decimal("0.2468"),
    Decimal("0.2742"),
    Decimal("0.2572"),
    Decimal("0.2794"),
    Decimal("0.2990"),
    Decimal("0.3473"),
    Decimal("0.2690"),
    Decimal("0.2708"),
)
_WEEKEND_KWH = (
    Decimal("0.2773"),
    Decimal("0.2377"),
    Decimal("0.2731"),
    Decimal("0.2363"),
    Decimal("0.2363"),
    Decimal("0.2363"),
    Decimal("0.1853"),
    Decimal("0.2334"),
    Decimal("0.3070"),
    Decimal("0.3296"),
    Decimal("0.3579"),
    Decimal("0.3169"),
    Decimal("0.3042"),
    Decimal("0.2490"),
    Decimal("0.2490"),
    Decimal("0.2561"),
    Decimal("0.2646"),
    Decimal("0.2759"),
    Decimal("0.2773"),
    Decimal("0.2872"),
    Decimal("0.3381"),
    Decimal("0.4216"),
    Decimal("0.3268"),
    Decimal("0.2801"),
)


def forecast(
    *,
    now: datetime,
    horizon_end: datetime,
    timezone: tzinfo,
) -> tuple[EnergyInterval, ...]:
    """Create an hourly non-EV house-load forecast from the static profiles.

    Args:
        now: Earliest instant that the forecast must cover.
        horizon_end: Exclusive end of the required forecast horizon.
        timezone: House timezone used to select local hour and day class.

    Returns:
        Hourly energy intervals covering the requested horizon. Boundary
        intervals may extend beyond it so planner fusion can prorate them.

    Raises:
        ValueError: If a timestamp is naive.
    """
    _validate_time(now, "Forecast start")
    _validate_time(horizon_end, "Forecast end")
    if horizon_end <= now:
        return ()

    start = now.replace(minute=0, second=0, microsecond=0)
    result: list[EnergyInterval] = []
    while start < horizon_end:
        local_start = start.astimezone(timezone)
        profile = _WEEKEND_KWH if local_start.weekday() >= 5 else _WEEKDAY_KWH
        result.append(
            EnergyInterval(
                interval=TimeInterval(start=start, end=start + _HOUR),
                energy_kwh=profile[local_start.hour],
            )
        )
        start += _HOUR
    return tuple(result)


def _validate_time(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
