"""Policy constants for the house-battery domain model."""

from datetime import timedelta
from decimal import Decimal

FULL_SOC_PERCENT = 100
MINIMUM_SOC_PERCENT = 10
FORCE_CHARGE_SOC_PERCENT = 7
MAXIMUM_GRID_IMPORT_POWER_KW = Decimal("0.1")
BATTERY_CYCLE_COST_PER_KWH = Decimal("0.0165")
OCTOPUS_RATE_SOURCE_MAX_AGE = timedelta(hours=26)
OCTOPUS_EXPORT_SOURCE_MAX_AGE = timedelta(hours=26)
OCTOPUS_DISPATCH_SOURCE_MAX_AGE = timedelta(minutes=10)
MAXIMUM_SOURCE_FUTURE_SKEW = timedelta(minutes=2)
OCTOPUS_RATE_UNIT = "GBP/kWh"
