"""Policy constants for the house-battery domain model."""

from datetime import timedelta
from decimal import Decimal

FULL_SOC_PERCENT = 100
MINIMUM_SOC_PERCENT = 10
FORCE_CHARGE_SOC_PERCENT = 7
MAXIMUM_GRID_IMPORT_POWER_KW = Decimal("0.1")
OFF_PEAK_CYCLE_DISCHARGE_DURATION = timedelta(minutes=10)
BATTERY_CYCLE_COST_PER_KWH = Decimal("0.0165")
