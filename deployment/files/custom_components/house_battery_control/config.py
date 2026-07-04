"""Typed House Battery Control configuration."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TypeAlias, cast

from . import battery

ConfigValue: TypeAlias = object


@dataclass(frozen=True, slots=True)
class BatteryConfig:
    """Defines battery installation facts and live input entities."""

    capacity_kwh: Decimal
    """Nominal energy stored at 100% state of charge."""

    minimum_state_of_charge_percent: Decimal
    """Inverter-enforced state-of-charge floor."""

    charge_efficiency: Decimal
    """Fraction of AC charging energy retained by the battery."""

    discharge_efficiency: Decimal
    """Fraction of stored energy delivered as AC energy."""

    state_of_charge_entity_id: str
    """Entity providing the current battery state of charge."""

    power_limit_entity_id: str
    """Entity providing the shared charge and discharge power limit."""

    def to_spec(self, power_limit_kw: Decimal) -> battery.Spec:
        """Build the battery specification using the current power limit."""
        if power_limit_kw < 0:
            raise ValueError("Battery power limit cannot be negative")
        return battery.Spec(
            capacity_kwh=self.capacity_kwh,
            minimum_energy_kwh=(
                self.capacity_kwh
                * self.minimum_state_of_charge_percent
                / Decimal(100)
            ),
            maximum_charge_power_kw=power_limit_kw,
            maximum_discharge_power_kw=power_limit_kw,
            charge_efficiency=self.charge_efficiency,
            discharge_efficiency=self.discharge_efficiency,
        )


@dataclass(frozen=True, slots=True)
class TariffConfig:
    """Identifies tariff input entities."""

    import_price_entity_id: str
    """Entity providing future import-price intervals."""

    export_price_entity_id: str
    """Entity providing the current export price."""


@dataclass(frozen=True, slots=True)
class SolarConfig:
    """Identifies the Forecast.Solar source."""

    config_entry_id: str
    """Home Assistant config entry containing the aggregated solar forecast."""


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    """Identifies adjustable control-policy entities."""

    reserve_margin_entity_id: str
    """Entity providing additional reserve energy for forecast uncertainty."""

    export_hysteresis_entity_id: str
    """Entity providing energy hysteresis for starting forced export."""


@dataclass(frozen=True, slots=True)
class InverterConfig:
    """Identifies the temporary inverter control boundary."""

    operating_mode_entity_id: str
    """Entity providing the confirmed inverter operating mode."""

    state_of_charge_target_entity_id: str
    """Entity providing the confirmed inverter state-of-charge target."""


@dataclass(frozen=True, slots=True)
class Config:
    """Defines all external configuration for House Battery Control."""

    battery: BatteryConfig
    """Battery installation and input configuration."""

    tariff: TariffConfig
    """Tariff input configuration."""

    solar: SolarConfig
    """Solar forecast configuration."""

    policy: PolicyConfig
    """Adjustable control-policy configuration."""

    inverter: InverterConfig
    """Inverter control-boundary configuration."""


def from_mapping(source: Mapping[str, ConfigValue]) -> Config:
    """Map validated YAML-shaped data to typed internal configuration."""
    _require_keys(source, {"battery", "tariff", "solar", "policy", "inverter"}, "config")

    battery_source = _mapping(source["battery"], "battery")
    _require_keys(
        battery_source,
        {
            "capacity_kwh",
            "minimum_state_of_charge_percent",
            "charge_efficiency",
            "discharge_efficiency",
            "state_of_charge_entity_id",
            "power_limit_entity_id",
        },
        "battery",
    )
    capacity_kwh = _positive_decimal(battery_source["capacity_kwh"], "capacity_kwh")
    minimum_state_of_charge_percent = _decimal(
        battery_source["minimum_state_of_charge_percent"],
        "minimum_state_of_charge_percent",
    )
    if not Decimal(0) <= minimum_state_of_charge_percent < Decimal(100):
        raise ValueError("minimum_state_of_charge_percent must be from 0 to below 100")

    battery_config = BatteryConfig(
        capacity_kwh=capacity_kwh,
        minimum_state_of_charge_percent=minimum_state_of_charge_percent,
        charge_efficiency=_efficiency(
            battery_source["charge_efficiency"],
            "charge_efficiency",
        ),
        discharge_efficiency=_efficiency(
            battery_source["discharge_efficiency"],
            "discharge_efficiency",
        ),
        state_of_charge_entity_id=_entity_id(
            battery_source["state_of_charge_entity_id"],
            "state_of_charge_entity_id",
        ),
        power_limit_entity_id=_entity_id(
            battery_source["power_limit_entity_id"],
            "power_limit_entity_id",
        ),
    )

    tariff_source = _mapping(source["tariff"], "tariff")
    _require_keys(
        tariff_source,
        {"import_price_entity_id", "export_price_entity_id"},
        "tariff",
    )
    tariff_config = TariffConfig(
        import_price_entity_id=_entity_id(
            tariff_source["import_price_entity_id"],
            "import_price_entity_id",
        ),
        export_price_entity_id=_entity_id(
            tariff_source["export_price_entity_id"],
            "export_price_entity_id",
        ),
    )

    solar_source = _mapping(source["solar"], "solar")
    _require_keys(solar_source, {"config_entry_id"}, "solar")
    solar_config = SolarConfig(
        config_entry_id=_string(solar_source["config_entry_id"], "config_entry_id"),
    )

    policy_source = _mapping(source["policy"], "policy")
    _require_keys(
        policy_source,
        {"reserve_margin_entity_id", "export_hysteresis_entity_id"},
        "policy",
    )
    policy_config = PolicyConfig(
        reserve_margin_entity_id=_entity_id(
            policy_source["reserve_margin_entity_id"],
            "reserve_margin_entity_id",
        ),
        export_hysteresis_entity_id=_entity_id(
            policy_source["export_hysteresis_entity_id"],
            "export_hysteresis_entity_id",
        ),
    )

    inverter_source = _mapping(source["inverter"], "inverter")
    _require_keys(
        inverter_source,
        {"operating_mode_entity_id", "state_of_charge_target_entity_id"},
        "inverter",
    )
    inverter_config = InverterConfig(
        operating_mode_entity_id=_entity_id(
            inverter_source["operating_mode_entity_id"],
            "operating_mode_entity_id",
        ),
        state_of_charge_target_entity_id=_entity_id(
            inverter_source["state_of_charge_target_entity_id"],
            "state_of_charge_target_entity_id",
        ),
    )

    return Config(
        battery=battery_config,
        tariff=tariff_config,
        solar=solar_config,
        policy=policy_config,
        inverter=inverter_config,
    )


def _require_keys(
    source: Mapping[str, ConfigValue],
    expected: set[str],
    name: str,
) -> None:
    actual = set(source)
    missing = expected - actual
    unknown = actual - expected
    if missing:
        raise ValueError(f"{name} is missing: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{name} contains unknown keys: {', '.join(sorted(unknown))}")


def _mapping(value: ConfigValue, name: str) -> Mapping[str, ConfigValue]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    if not all(isinstance(key, str) for key in value):
        raise ValueError(f"{name} keys must be strings")
    return cast(Mapping[str, ConfigValue], value)


def _decimal(value: ConfigValue, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a decimal number")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be a decimal number") from None


def _positive_decimal(value: ConfigValue, name: str) -> Decimal:
    result = _decimal(value, name)
    if result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _efficiency(value: ConfigValue, name: str) -> Decimal:
    result = _decimal(value, name)
    if not Decimal(0) < result <= Decimal(1):
        raise ValueError(f"{name} must be greater than 0 and at most 1")
    return result


def _string(value: ConfigValue, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _entity_id(value: ConfigValue, name: str) -> str:
    result = _string(value, name)
    if re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", result) is None:
        raise ValueError(f"{name} must be a Home Assistant entity ID")
    return result
