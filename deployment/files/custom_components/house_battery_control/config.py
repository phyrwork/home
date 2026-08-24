"""Small, strict YAML configuration for house-battery control."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .model import FULL_SOC_PERCENT, MINIMUM_SOC_PERCENT
from .solis import SolisConfig, config_from_mapping

DOMAIN = "house_battery_control"


@dataclass(frozen=True, slots=True)
class BatteryConfig:
    capacity_kwh: Decimal
    minimum_soc_percent: Decimal
    charge_efficiency: Decimal
    discharge_efficiency: Decimal
    reserve_margin_kwh: Decimal

    @property
    def minimum_energy_kwh(self) -> Decimal:
        return self.capacity_kwh * self.minimum_soc_percent / Decimal(FULL_SOC_PERCENT)


@dataclass(frozen=True, slots=True)
class TariffConfig:
    import_rates_entity_id: str
    export_rates_entity_id: str


@dataclass(frozen=True, slots=True)
class SolarConfig:
    config_entry_id: str


@dataclass(frozen=True, slots=True)
class Config:
    battery: BatteryConfig
    tariff: TariffConfig
    solar: SolarConfig
    solis: SolisConfig
    cycle_discharge_duration_entity_id: str


def from_mapping(value: Mapping[str, Any]) -> Config:
    source = _mapping(value, "config")
    _keys(
        source,
        {
            "battery",
            "tariff",
            "solar",
            "solis",
            "cycle_discharge_duration_entity_id",
        },
        name="config",
    )

    battery = _mapping(source["battery"], "battery")
    _keys(
        battery,
        {
            "capacity_kwh",
            "minimum_soc_percent",
            "charge_efficiency",
            "discharge_efficiency",
            "reserve_margin_kwh",
        },
        name="battery",
    )
    capacity = _decimal(battery["capacity_kwh"], "battery.capacity_kwh")
    minimum_soc = _decimal(battery["minimum_soc_percent"], "battery.minimum_soc_percent")
    reserve_margin = _decimal(battery["reserve_margin_kwh"], "battery.reserve_margin_kwh")
    if capacity <= 0:
        raise ValueError("battery.capacity_kwh must be positive")
    if minimum_soc != Decimal(MINIMUM_SOC_PERCENT):
        raise ValueError("battery.minimum_soc_percent must equal MINIMUM_SOC_PERCENT")
    if reserve_margin < 0:
        raise ValueError("battery.reserve_margin_kwh must not be negative")
    battery_config = BatteryConfig(
        capacity_kwh=capacity,
        minimum_soc_percent=minimum_soc,
        charge_efficiency=_efficiency(battery["charge_efficiency"], "battery.charge_efficiency"),
        discharge_efficiency=_efficiency(battery["discharge_efficiency"], "battery.discharge_efficiency"),
        reserve_margin_kwh=reserve_margin,
    )

    tariff = _mapping(source["tariff"], "tariff")
    _keys(tariff, {"import_rates_entity_id", "export_rates_entity_id"}, name="tariff")
    tariff_config = TariffConfig(
        import_rates_entity_id=_entity(tariff["import_rates_entity_id"], "tariff.import_rates_entity_id", "sensor"),
        export_rates_entity_id=_entity(tariff["export_rates_entity_id"], "tariff.export_rates_entity_id", "sensor"),
    )

    solar = _mapping(source["solar"], "solar")
    _keys(solar, {"config_entry_id"}, name="solar")
    entry_id = solar["config_entry_id"]
    if not isinstance(entry_id, str) or not entry_id:
        raise ValueError("solar.config_entry_id must be a non-empty string")

    cycle_duration = _entity(
        source["cycle_discharge_duration_entity_id"],
        "cycle_discharge_duration_entity_id",
        "input_number",
    )
    return Config(
        battery=battery_config,
        tariff=tariff_config,
        solar=SolarConfig(entry_id),
        solis=config_from_mapping(_mapping(source["solis"], "solis")),
        cycle_discharge_duration_entity_id=cycle_duration,
    )


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be a mapping with string keys")
    return value


def _keys(source: Mapping[str, Any], required: set[str], *, name: str, optional: set[str] | None = None) -> None:
    optional = optional or set()
    missing = required - set(source)
    unknown = set(source) - required - optional
    if missing:
        raise ValueError(f"{name} is missing: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{name} contains unknown keys: {', '.join(sorted(unknown))}")


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def _efficiency(value: object, name: str) -> Decimal:
    result = _decimal(value, name)
    if not Decimal(0) < result <= Decimal(1):
        raise ValueError(f"{name} must be greater than zero and at most one")
    return result


def _entity(value: object, name: str, domain: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", value) is None:
        raise ValueError(f"{name} must be an entity ID")
    if value.split(".", 1)[0] != domain:
        raise ValueError(f"{name} must be a {domain} entity ID")
    return value


__all__ = [
    "BatteryConfig",
    "Config",
    "DOMAIN",
    "SolarConfig",
    "TariffConfig",
    "from_mapping",
]
