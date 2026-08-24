"""Pure, immutable configuration for the live Solis entity boundary.

The configuration in this module is deliberately an offline contract.  It
validates entity IDs and the intentional slot ownership, but it does not
inspect Home Assistant state or make service calls.  Runtime availability,
capabilities and provenance are validated by the later runtime adapter.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, fields
from enum import Enum
from typing import TypeAlias, cast


ConfigValue: TypeAlias = object


class BatteryPowerSign(str, Enum):
    """Meaning of a positive value from the signed battery-power sensor."""

    POSITIVE_MEANS_CHARGING = "positive_means_charging"
    POSITIVE_MEANS_DISCHARGING = "positive_means_discharging"


class SolisSlotOwner(str, Enum):
    """Intentional Home Assistant owner of a physical Solis slot direction."""

    CHEAP_CHARGING = "cheap_charging"
    FULL_SOC_CYCLING = "full_soc_cycling"
    RESERVE_EXPORT = "reserve_export"
    RESERVED = "reserved"


@dataclass(frozen=True, slots=True)
class SolisTelemetryConfig:
    """Authoritative Solis telemetry used by the controller."""

    state_of_charge_entity_id: str
    battery_power_entity_id: str
    battery_power_sign: BatteryPowerSign
    battery_voltage_entity_id: str
    device_timestamp_entity_id: str


@dataclass(frozen=True, slots=True)
class SolisPersistentControlConfig:
    """Persistent operating-policy controls owned by this controller."""

    storage_mode_entity_id: str
    allow_grid_charging_entity_id: str
    inverter_time_entity_id: str


@dataclass(frozen=True, slots=True)
class SolisProtectionControlConfig:
    """Battery protection and household-reserve controls."""

    battery_reserve_entity_id: str
    battery_reserve_soc_entity_id: str


@dataclass(frozen=True, slots=True)
class SolisCapabilityControlConfig:
    """Global capability controls whose bounds are runtime observations."""

    battery_max_charge_current_entity_id: str
    battery_max_discharge_current_entity_id: str


@dataclass(frozen=True, slots=True)
class SolisSlotDirectionConfig:
    """The four entities and owner for one charge/discharge direction."""

    enable_entity_id: str
    time_entity_id: str
    current_entity_id: str
    target_soc_entity_id: str
    owner: SolisSlotOwner


@dataclass(frozen=True, slots=True)
class SolisSlotConfig:
    """Complete physical charge and discharge mapping for one slot."""

    physical_slot: int
    charge: SolisSlotDirectionConfig
    discharge: SolisSlotDirectionConfig


@dataclass(frozen=True, slots=True)
class SolisConfig:
    """Complete typed mapping for the installed Garage Solis system."""

    telemetry: SolisTelemetryConfig
    persistent: SolisPersistentControlConfig
    protection: SolisProtectionControlConfig
    capability: SolisCapabilityControlConfig
    slots: tuple[SolisSlotConfig, ...]


_ENTITY_PATTERN = re.compile(r"(?P<domain>[a-z0-9_]+)\.[a-z0-9_]+$")
_EXPECTED_SLOT_NUMBERS = frozenset(range(1, 7))
_EXPECTED_SLOT_OWNERS = {
    (1, "charge"): SolisSlotOwner.CHEAP_CHARGING,
    (2, "charge"): SolisSlotOwner.CHEAP_CHARGING,
    (1, "discharge"): SolisSlotOwner.FULL_SOC_CYCLING,
    (3, "discharge"): SolisSlotOwner.FULL_SOC_CYCLING,
    (2, "discharge"): SolisSlotOwner.RESERVE_EXPORT,
    (4, "discharge"): SolisSlotOwner.RESERVE_EXPORT,
    (3, "charge"): SolisSlotOwner.RESERVED,
    (4, "charge"): SolisSlotOwner.RESERVED,
    (5, "charge"): SolisSlotOwner.RESERVED,
    (6, "charge"): SolisSlotOwner.RESERVED,
    (5, "discharge"): SolisSlotOwner.RESERVED,
    (6, "discharge"): SolisSlotOwner.RESERVED,
}
def from_mapping(source: Mapping[str, ConfigValue]) -> SolisConfig:
    """Parse and strictly validate a YAML-shaped ``solis`` mapping."""

    root = _mapping(source, "solis")
    _require_keys(
        root,
        {
            "telemetry",
            "persistent",
            "protection",
            "capability",
            "slot_entity_prefix",
            "slot_allocations",
        },
        "solis",
    )

    telemetry_source = _mapping(root["telemetry"], "solis.telemetry")
    _require_keys(
        telemetry_source,
        {
            "state_of_charge_entity_id",
            "battery_power_entity_id",
            "battery_power_sign",
            "battery_voltage_entity_id",
            "device_timestamp_entity_id",
        },
        "solis.telemetry",
    )
    telemetry = SolisTelemetryConfig(
        state_of_charge_entity_id=_entity(
            telemetry_source["state_of_charge_entity_id"],
            "solis.telemetry.state_of_charge_entity_id",
            "sensor",
        ),
        battery_power_entity_id=_entity(
            telemetry_source["battery_power_entity_id"],
            "solis.telemetry.battery_power_entity_id",
            "sensor",
        ),
        battery_power_sign=_power_sign(
            telemetry_source["battery_power_sign"],
            "solis.telemetry.battery_power_sign",
        ),
        battery_voltage_entity_id=_entity(
            telemetry_source["battery_voltage_entity_id"],
            "solis.telemetry.battery_voltage_entity_id",
            "sensor",
        ),
        device_timestamp_entity_id=_entity(
            telemetry_source["device_timestamp_entity_id"],
            "solis.telemetry.device_timestamp_entity_id",
            "sensor",
        ),
    )
    persistent_source = _mapping(root["persistent"], "solis.persistent")
    _require_keys(
        persistent_source,
        {
            "storage_mode_entity_id",
            "allow_grid_charging_entity_id",
            "inverter_time_entity_id",
        },
        "solis.persistent",
    )
    persistent = SolisPersistentControlConfig(
        storage_mode_entity_id=_entity(
            persistent_source["storage_mode_entity_id"],
            "solis.persistent.storage_mode_entity_id",
            "select",
        ),
        allow_grid_charging_entity_id=_entity(
            persistent_source["allow_grid_charging_entity_id"],
            "solis.persistent.allow_grid_charging_entity_id",
            "switch",
        ),
        inverter_time_entity_id=_entity(
            persistent_source["inverter_time_entity_id"],
            "solis.persistent.inverter_time_entity_id",
            "datetime",
        ),
    )

    protection_source = _mapping(root["protection"], "solis.protection")
    _require_keys(
        protection_source,
        {
            "battery_reserve_entity_id",
            "battery_reserve_soc_entity_id",
        },
        "solis.protection",
    )
    protection = SolisProtectionControlConfig(
        battery_reserve_entity_id=_entity(
            protection_source["battery_reserve_entity_id"],
            "solis.protection.battery_reserve_entity_id",
            "switch",
        ),
        battery_reserve_soc_entity_id=_entity(
            protection_source["battery_reserve_soc_entity_id"],
            "solis.protection.battery_reserve_soc_entity_id",
            "number",
        ),
    )

    capability_source = _mapping(root["capability"], "solis.capability")
    _require_keys(
        capability_source,
        {
            "battery_max_charge_current_entity_id",
            "battery_max_discharge_current_entity_id",
        },
        "solis.capability",
    )
    capability = SolisCapabilityControlConfig(
        battery_max_charge_current_entity_id=_entity(
            capability_source["battery_max_charge_current_entity_id"],
            "solis.capability.battery_max_charge_current_entity_id",
            "number",
        ),
        battery_max_discharge_current_entity_id=_entity(
            capability_source["battery_max_discharge_current_entity_id"],
            "solis.capability.battery_max_discharge_current_entity_id",
            "number",
        ),
    )

    slots = _compact_slots(root["slot_entity_prefix"], root["slot_allocations"])
    config = SolisConfig(
        telemetry=telemetry,
        persistent=persistent,
        protection=protection,
        capability=capability,
        slots=slots,
    )
    _validate_global_entity_uniqueness(config)
    return config


def _compact_slots(prefix_value: ConfigValue, allocations_value: ConfigValue) -> tuple[SolisSlotConfig, ...]:
    """Generate the complete six-slot mapping from the compact allocation."""

    if not isinstance(prefix_value, str) or re.fullmatch(r"[a-z0-9_]+", prefix_value) is None:
        raise ValueError("solis.slot_entity_prefix must be lowercase text")
    allocations = _mapping(allocations_value, "solis.slot_allocations")
    expected = {"cheap_charging", "full_soc_cycling", "reserve_export"}
    if set(allocations) != expected:
        raise ValueError("solis.slot_allocations must define exactly the three owners")
    owner_values: dict[SolisSlotOwner, tuple[int, int]] = {}
    for key, value in allocations.items():
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ValueError(f"solis.slot_allocations.{key} must contain two slots")
        if any(not isinstance(slot, int) or isinstance(slot, bool) or slot not in _EXPECTED_SLOT_NUMBERS for slot in value):
            raise ValueError(f"solis.slot_allocations.{key} contains an invalid slot")
        if value[0] == value[1]:
            raise ValueError(f"solis.slot_allocations.{key} contains a duplicate slot")
        owner_values[SolisSlotOwner(key)] = (value[0], value[1])

    def direction(slot: int, name: str) -> SolisSlotDirectionConfig:
        owner = SolisSlotOwner.RESERVED
        for candidate, slots_for_owner in owner_values.items():
            if (name == "charge" and candidate is SolisSlotOwner.CHEAP_CHARGING) or (
                name == "discharge" and candidate is not SolisSlotOwner.CHEAP_CHARGING
            ):
                if slot in slots_for_owner:
                    owner = candidate
        base = f"{prefix_value}_slot{slot}_{name}"
        return SolisSlotDirectionConfig(
            enable_entity_id=_entity(f"switch.{base}", f"slot {slot} {name} enable", "switch"),
            time_entity_id=_entity(f"text.{base}_time", f"slot {slot} {name} time", "text"),
            current_entity_id=_entity(f"number.{base}_current", f"slot {slot} {name} current", "number"),
            target_soc_entity_id=_entity(f"number.{base}_soc", f"slot {slot} {name} target", "number"),
            owner=owner,
        )

    slots = tuple(
        SolisSlotConfig(slot, direction(slot, "charge"), direction(slot, "discharge"))
        for slot in sorted(_EXPECTED_SLOT_NUMBERS)
    )
    for slot in slots:
        if slot.charge.owner is not _EXPECTED_SLOT_OWNERS[(slot.physical_slot, "charge")]:
            raise ValueError("slot allocation does not match charge owner mapping")
        if slot.discharge.owner is not _EXPECTED_SLOT_OWNERS[(slot.physical_slot, "discharge")]:
            raise ValueError("slot allocation does not match discharge owner mapping")
    return slots


def _power_sign(value: ConfigValue, name: str) -> BatteryPowerSign:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a defined power sign")
    try:
        return BatteryPowerSign(value)
    except ValueError:
        raise ValueError(f"{name} has an unknown power sign convention: {value}") from None


def _validate_global_entity_uniqueness(config: SolisConfig) -> None:
    ids: list[str] = []
    ids.extend(_values(config.telemetry))
    ids.extend(_values(config.persistent))
    ids.extend(_values(config.protection))
    ids.extend(_values(config.capability))
    for slot in config.slots:
        ids.extend(_values(slot.charge))
        ids.extend(_values(slot.discharge))
    seen: set[str] = set()
    duplicates: set[str] = set()
    for entity_id in ids:
        if entity_id in seen:
            duplicates.add(entity_id)
        seen.add(entity_id)
    if duplicates:
        raise ValueError(
            "managed Solis entity IDs must be globally unique: "
            + ", ".join(sorted(duplicates))
        )


def _values(value: object) -> list[str]:
    return [
        item
        for field in fields(value)
        if isinstance(item := getattr(value, field.name), str)
        and _ENTITY_PATTERN.fullmatch(item) is not None
    ]


def _entity(value: ConfigValue, name: str, domain: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a {domain} entity ID")
    match = _ENTITY_PATTERN.fullmatch(value)
    if match is None or match.group("domain") != domain:
        raise ValueError(f"{name} must be a {domain} entity ID")
    return value


def _mapping(value: ConfigValue, name: str) -> Mapping[str, ConfigValue]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    if not all(isinstance(key, str) for key in value):
        raise ValueError(f"{name} keys must be strings")
    return cast(Mapping[str, ConfigValue], value)


def _require_keys(
    source: Mapping[str, ConfigValue], expected: set[str], name: str
) -> None:
    actual = set(source)
    missing = expected - actual
    unknown = actual - expected
    if missing:
        raise ValueError(f"{name} is missing: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{name} contains unknown keys: {', '.join(sorted(unknown))}")
