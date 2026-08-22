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
    PRE_DISCHARGE = "pre_discharge"
    RESERVED = "reserved"


class MaximumGridImportPolicy(str, Enum):
    """How the unavailable Solis Peak Shaving limit is commissioned."""

    MANUAL_COMMISSIONING = "manual_commissioning"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class SolisTelemetryConfig:
    """Authoritative Solis telemetry and explicitly unresolved facts."""

    state_of_charge_entity_id: str
    battery_power_entity_id: str | None
    battery_power_sign: BatteryPowerSign | None
    device_timestamp_entity_id: str | None


@dataclass(frozen=True, slots=True)
class SolisPersistentControlConfig:
    """Persistent operating-policy controls owned by this controller."""

    storage_mode_entity_id: str
    allow_grid_charging_entity_id: str
    allow_export_entity_id: str
    grid_peak_shaving_entity_id: str
    inverter_on_off_entity_id: str
    inverter_time_entity_id: str


@dataclass(frozen=True, slots=True)
class SolisProtectionControlConfig:
    """Battery protection and household-reserve controls."""

    battery_over_discharge_soc_entity_id: str
    battery_force_charge_soc_entity_id: str
    battery_recovery_soc_entity_id: str
    battery_max_charge_soc_entity_id: str
    battery_reserve_entity_id: str
    battery_reserve_soc_entity_id: str


@dataclass(frozen=True, slots=True)
class SolisCapabilityControlConfig:
    """Global capability controls whose bounds are runtime observations."""

    battery_max_charge_current_entity_id: str
    battery_max_discharge_current_entity_id: str
    max_output_power_entity_id: str
    max_export_power_entity_id: str


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
    maximum_grid_import_policy: MaximumGridImportPolicy
    slots: tuple[SolisSlotConfig, ...]


_ENTITY_PATTERN = re.compile(r"(?P<domain>[a-z0-9_]+)\.[a-z0-9_]+$")
_EXPECTED_SLOT_NUMBERS = frozenset(range(1, 7))
_EXPECTED_SLOT_OWNERS = {
    (1, "charge"): SolisSlotOwner.CHEAP_CHARGING,
    (1, "discharge"): SolisSlotOwner.FULL_SOC_CYCLING,
    (2, "charge"): SolisSlotOwner.RESERVED,
    (2, "discharge"): SolisSlotOwner.PRE_DISCHARGE,
    (3, "charge"): SolisSlotOwner.RESERVED,
    (3, "discharge"): SolisSlotOwner.RESERVED,
    (4, "charge"): SolisSlotOwner.RESERVED,
    (4, "discharge"): SolisSlotOwner.RESERVED,
    (5, "charge"): SolisSlotOwner.RESERVED,
    (5, "discharge"): SolisSlotOwner.RESERVED,
    (6, "charge"): SolisSlotOwner.RESERVED,
    (6, "discharge"): SolisSlotOwner.RESERVED,
}
_LEGACY_STUB_IDS = frozenset(
    {
        "input_number.house_battery_state_of_charge",
        "input_number.house_battery_power_limit",
        "input_select.house_battery_operating_mode",
        "input_number.house_battery_state_of_charge_target",
    }
)


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
            "maximum_grid_import_policy",
            "slots",
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
        battery_power_entity_id=_optional_entity(
            telemetry_source["battery_power_entity_id"],
            "solis.telemetry.battery_power_entity_id",
            "sensor",
        ),
        battery_power_sign=_power_sign(
            telemetry_source["battery_power_sign"],
            "solis.telemetry.battery_power_sign",
        ),
        device_timestamp_entity_id=_optional_entity(
            telemetry_source["device_timestamp_entity_id"],
            "solis.telemetry.device_timestamp_entity_id",
            "sensor",
        ),
    )
    if (telemetry.battery_power_entity_id is None) != (
        telemetry.battery_power_sign is None
    ):
        raise ValueError(
            "solis.telemetry.battery_power_entity_id and "
            "battery_power_sign must both be null or both be configured"
        )

    persistent_source = _mapping(root["persistent"], "solis.persistent")
    _require_keys(
        persistent_source,
        {
            "storage_mode_entity_id",
            "allow_grid_charging_entity_id",
            "allow_export_entity_id",
            "grid_peak_shaving_entity_id",
            "inverter_on_off_entity_id",
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
        allow_export_entity_id=_entity(
            persistent_source["allow_export_entity_id"],
            "solis.persistent.allow_export_entity_id",
            "switch",
        ),
        grid_peak_shaving_entity_id=_entity(
            persistent_source["grid_peak_shaving_entity_id"],
            "solis.persistent.grid_peak_shaving_entity_id",
            "switch",
        ),
        inverter_on_off_entity_id=_entity(
            persistent_source["inverter_on_off_entity_id"],
            "solis.persistent.inverter_on_off_entity_id",
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
            "battery_over_discharge_soc_entity_id",
            "battery_force_charge_soc_entity_id",
            "battery_recovery_soc_entity_id",
            "battery_max_charge_soc_entity_id",
            "battery_reserve_entity_id",
            "battery_reserve_soc_entity_id",
        },
        "solis.protection",
    )
    protection = SolisProtectionControlConfig(
        battery_over_discharge_soc_entity_id=_entity(
            protection_source["battery_over_discharge_soc_entity_id"],
            "solis.protection.battery_over_discharge_soc_entity_id",
            "number",
        ),
        battery_force_charge_soc_entity_id=_entity(
            protection_source["battery_force_charge_soc_entity_id"],
            "solis.protection.battery_force_charge_soc_entity_id",
            "number",
        ),
        battery_recovery_soc_entity_id=_entity(
            protection_source["battery_recovery_soc_entity_id"],
            "solis.protection.battery_recovery_soc_entity_id",
            "number",
        ),
        battery_max_charge_soc_entity_id=_entity(
            protection_source["battery_max_charge_soc_entity_id"],
            "solis.protection.battery_max_charge_soc_entity_id",
            "number",
        ),
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
            "max_output_power_entity_id",
            "max_export_power_entity_id",
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
        max_output_power_entity_id=_entity(
            capability_source["max_output_power_entity_id"],
            "solis.capability.max_output_power_entity_id",
            "number",
        ),
        max_export_power_entity_id=_entity(
            capability_source["max_export_power_entity_id"],
            "solis.capability.max_export_power_entity_id",
            "number",
        ),
    )

    policy = root["maximum_grid_import_policy"]
    if policy != MaximumGridImportPolicy.MANUAL_COMMISSIONING.value:
        raise ValueError(
            "solis.maximum_grid_import_policy must be exactly manual_commissioning"
        )

    slots = _slots(root["slots"])
    config = SolisConfig(
        telemetry=telemetry,
        persistent=persistent,
        protection=protection,
        capability=capability,
        maximum_grid_import_policy=MaximumGridImportPolicy.MANUAL_COMMISSIONING,
        slots=slots,
    )
    _validate_global_entity_uniqueness(config)
    return config


def _slots(value: ConfigValue) -> tuple[SolisSlotConfig, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("solis.slots must be a list")
    if len(value) != 6:
        raise ValueError("solis.slots must contain exactly six slot groups")

    parsed: list[SolisSlotConfig] = []
    seen: set[int] = set()
    for index, raw_slot in enumerate(value):
        slot_source = _mapping(raw_slot, f"solis.slots[{index}]")
        _require_keys(
            slot_source,
            {"physical_slot", "charge", "discharge"},
            f"solis.slots[{index}]",
        )
        physical_slot = slot_source["physical_slot"]
        if (
            not isinstance(physical_slot, int)
            or isinstance(physical_slot, bool)
            or physical_slot not in _EXPECTED_SLOT_NUMBERS
        ):
            raise ValueError(
                f"solis.slots[{index}].physical_slot must be in the range 1 through 6"
            )
        if physical_slot in seen:
            raise ValueError(f"duplicate Solis physical slot {physical_slot}")
        seen.add(physical_slot)
        charge = _slot_direction(
            slot_source["charge"], physical_slot, "charge"
        )
        discharge = _slot_direction(
            slot_source["discharge"], physical_slot, "discharge"
        )
        expected_charge_owner = _EXPECTED_SLOT_OWNERS[(physical_slot, "charge")]
        expected_discharge_owner = _EXPECTED_SLOT_OWNERS[(physical_slot, "discharge")]
        if charge.owner is not expected_charge_owner:
            raise ValueError(
                f"Solis slot {physical_slot} charge owner must be "
                f"{expected_charge_owner.value}"
            )
        if discharge.owner is not expected_discharge_owner:
            raise ValueError(
                f"Solis slot {physical_slot} discharge owner must be "
                f"{expected_discharge_owner.value}"
            )
        parsed.append(
            SolisSlotConfig(
                physical_slot=physical_slot,
                charge=charge,
                discharge=discharge,
            )
        )

    if seen != _EXPECTED_SLOT_NUMBERS:
        missing = sorted(_EXPECTED_SLOT_NUMBERS - seen)
        raise ValueError(f"solis.slots is missing physical slots: {missing}")
    return tuple(sorted(parsed, key=lambda slot: slot.physical_slot))


def _slot_direction(
    value: ConfigValue, physical_slot: int, direction: str
) -> SolisSlotDirectionConfig:
    name = f"solis.slots[{physical_slot}].{direction}"
    source = _mapping(value, name)
    _require_keys(
        source,
        {
            "enable_entity_id",
            "time_entity_id",
            "current_entity_id",
            "target_soc_entity_id",
            "owner",
        },
        name,
    )
    return SolisSlotDirectionConfig(
        enable_entity_id=_entity(
            source["enable_entity_id"], f"{name}.enable_entity_id", "switch"
        ),
        time_entity_id=_entity(
            source["time_entity_id"], f"{name}.time_entity_id", "text"
        ),
        current_entity_id=_entity(
            source["current_entity_id"], f"{name}.current_entity_id", "number"
        ),
        target_soc_entity_id=_entity(
            source["target_soc_entity_id"],
            f"{name}.target_soc_entity_id",
            "number",
        ),
        owner=_owner(source["owner"], f"{name}.owner"),
    )


def _owner(value: ConfigValue, name: str) -> SolisSlotOwner:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a Solis slot owner")
    try:
        return SolisSlotOwner(value)
    except ValueError:
        raise ValueError(f"{name} has an unknown Solis slot owner: {value}") from None


def _power_sign(value: ConfigValue, name: str) -> BatteryPowerSign | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be null or a defined power sign")
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
    legacy = set(_LEGACY_STUB_IDS.intersection(ids))
    legacy.update(
        entity_id
        for entity_id in ids
        if entity_id.startswith("input_number.house_battery_")
        or entity_id.startswith("input_select.house_battery_")
    )
    if legacy:
        raise ValueError(
            "legacy stub entity IDs are not allowed in solis mapping: "
            + ", ".join(sorted(legacy))
        )
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
    if (
        value in _LEGACY_STUB_IDS
        or value.startswith("input_number.house_battery_")
        or value.startswith("input_select.house_battery_")
    ):
        raise ValueError(f"{name} must not reference a legacy stub entity ID")
    match = _ENTITY_PATTERN.fullmatch(value)
    if match is None or match.group("domain") != domain:
        raise ValueError(f"{name} must be a {domain} entity ID")
    return value


def _optional_entity(value: ConfigValue, name: str, domain: str) -> str | None:
    if value is None:
        return None
    return _entity(value, name, domain)


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
