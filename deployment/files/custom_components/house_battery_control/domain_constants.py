"""Policy constants for the house-battery domain model."""

from datetime import timedelta
from decimal import Decimal

FULL_SOC_PERCENT = 100
MINIMUM_SOC_PERCENT = 10
FORCE_CHARGE_SOC_PERCENT = 7
MAXIMUM_GRID_IMPORT_POWER_KW = Decimal("0.1")
OFF_PEAK_CYCLE_DISCHARGE_DURATION = timedelta(minutes=10)
BATTERY_CYCLE_COST_PER_KWH = Decimal("0.0165")
OCTOPUS_RATE_SOURCE_MAX_AGE = timedelta(hours=26)
OCTOPUS_EXPORT_SOURCE_MAX_AGE = timedelta(hours=26)
OCTOPUS_DISPATCH_SOURCE_MAX_AGE = timedelta(minutes=10)
# Forecasts are produced by a different integration from tariff data.  Keep a
# separate, named bound so a caller cannot extend forecast validity by
# supplying an arbitrary ``fresh_until`` value.
FORECAST_SOURCE_MAX_AGE = timedelta(hours=2)
MAXIMUM_SOURCE_FUTURE_SKEW = timedelta(minutes=2)
OCTOPUS_RATE_UNIT = "GBP/kWh"
