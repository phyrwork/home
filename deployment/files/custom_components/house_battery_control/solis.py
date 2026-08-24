"""Narrow state and write boundary for the commissioned Solis inverter."""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, replace
from datetime import datetime, time, timedelta, timezone as dt_timezone, tzinfo
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from enum import Enum
from types import MappingProxyType
from typing import Any

from .domain_constants import FULL_SOC_PERCENT, MINIMUM_SOC_PERCENT
from .model import (
    ControllerHealth,
    LogicalIntent,
    ObservedCapability,
    RuntimeCapabilities,
    SlotDirection,
    SlotIntent,
    SlotOwner,
    StorageMode,
)

MAXIMUM_TELEMETRY_AGE = timedelta(minutes=30)
MAXIMUM_FUTURE_CLOCK_SKEW = timedelta(minutes=1)
MAXIMUM_INVERTER_CLOCK_SKEW = timedelta(minutes=1)
SERVICE_TIMEOUT = timedelta(seconds=10)
READBACK_TIMEOUT = timedelta(seconds=15)
MIDNIGHT_END_OPTIONS = frozenset(("24:00", "23:59"))
_UNKNOWN = frozenset(("unknown", "unavailable"))
_TIME_PATTERN = re.compile(
    r"^(?P<start>(?:[01][0-9]|2[0-3]):[0-5][0-9])-"
    r"(?P<end>(?:(?:[01][0-9]|2[0-3]):[0-5][0-9]|24:00))$"
)
_ENTITY_PATTERN = re.compile(r"(?P<domain>[a-z0-9_]+)\.[a-z0-9_]+$")


class BatteryPowerSign(str, Enum):
    POSITIVE_MEANS_CHARGING = "positive_means_charging"
    POSITIVE_MEANS_DISCHARGING = "positive_means_discharging"


@dataclass(frozen=True, slots=True)
class SolisTelemetryConfig:
    state_of_charge_entity_id: str
    battery_power_entity_id: str
    battery_power_sign: BatteryPowerSign
    battery_voltage_entity_id: str
    device_timestamp_entity_id: str


@dataclass(frozen=True, slots=True)
class SolisPersistentConfig:
    storage_mode_entity_id: str
    allow_grid_charging_entity_id: str
    inverter_time_entity_id: str


@dataclass(frozen=True, slots=True)
class SolisProtectionConfig:
    battery_reserve_entity_id: str
    battery_reserve_soc_entity_id: str


@dataclass(frozen=True, slots=True)
class SolisCapabilityConfig:
    battery_max_charge_current_entity_id: str
    battery_max_discharge_current_entity_id: str


@dataclass(frozen=True, slots=True)
class SolisDirectionConfig:
    enable_entity_id: str
    time_entity_id: str
    current_entity_id: str
    target_soc_entity_id: str
    owner: SlotOwner | None


@dataclass(frozen=True, slots=True)
class SolisSlotConfig:
    physical_slot: int
    charge: SolisDirectionConfig
    discharge: SolisDirectionConfig


@dataclass(frozen=True, slots=True)
class SolisConfig:
    telemetry: SolisTelemetryConfig
    persistent: SolisPersistentConfig
    protection: SolisProtectionConfig
    capability: SolisCapabilityConfig
    slots: tuple[SolisSlotConfig, ...]
    midnight_end: str

    def direction(self, key: "SlotKey") -> SolisDirectionConfig:
        slot = self.slots[key.physical_slot - 1]
        return slot.charge if key.direction is SlotDirection.CHARGE else slot.discharge

    def allocation(self, owner: SlotOwner) -> tuple["SlotKey", "SlotKey"]:
        direction = (
            SlotDirection.CHARGE
            if owner is SlotOwner.CHEAP_CHARGING
            else SlotDirection.DISCHARGE
        )
        keys = tuple(
            SlotKey(slot.physical_slot, direction)
            for slot in self.slots
            if (slot.charge if direction is SlotDirection.CHARGE else slot.discharge).owner is owner
        )
        if len(keys) != 2:
            raise ValueError(f"owner {owner.value} does not have two configured slots")
        return keys  # type: ignore[return-value]


def config_from_mapping(source: Mapping[str, object]) -> SolisConfig:
    """Parse the strict compact Solis mapping."""

    root = _mapping(source, "solis")
    _keys(
        root,
        {"telemetry", "persistent", "protection", "capability", "slot_entity_prefix", "slot_allocations", "midnight_end"},
        "solis",
    )
    telemetry = _mapping(root["telemetry"], "solis.telemetry")
    _keys(telemetry, {"state_of_charge_entity_id", "battery_power_entity_id", "battery_power_sign", "battery_voltage_entity_id", "device_timestamp_entity_id"}, "solis.telemetry")
    persistent = _mapping(root["persistent"], "solis.persistent")
    _keys(persistent, {"storage_mode_entity_id", "allow_grid_charging_entity_id", "inverter_time_entity_id"}, "solis.persistent")
    protection = _mapping(root["protection"], "solis.protection")
    _keys(protection, {"battery_reserve_entity_id", "battery_reserve_soc_entity_id"}, "solis.protection")
    capability = _mapping(root["capability"], "solis.capability")
    _keys(capability, {"battery_max_charge_current_entity_id", "battery_max_discharge_current_entity_id"}, "solis.capability")
    power_sign = _enum(BatteryPowerSign, telemetry["battery_power_sign"], "solis.telemetry.battery_power_sign")
    midnight_end = root["midnight_end"]
    if midnight_end not in MIDNIGHT_END_OPTIONS:
        raise ValueError("solis.midnight_end must be 24:00 or 23:59")
    result = SolisConfig(
        telemetry=SolisTelemetryConfig(
            _entity(telemetry["state_of_charge_entity_id"], "sensor", "state of charge"),
            _entity(telemetry["battery_power_entity_id"], "sensor", "battery power"),
            power_sign,
            _entity(telemetry["battery_voltage_entity_id"], "sensor", "battery voltage"),
            _entity(telemetry["device_timestamp_entity_id"], "sensor", "device timestamp"),
        ),
        persistent=SolisPersistentConfig(
            _entity(persistent["storage_mode_entity_id"], "select", "storage mode"),
            _entity(persistent["allow_grid_charging_entity_id"], "switch", "allow grid charging"),
            _entity(persistent["inverter_time_entity_id"], "datetime", "inverter time"),
        ),
        protection=SolisProtectionConfig(
            _entity(protection["battery_reserve_entity_id"], "switch", "battery reserve"),
            _entity(protection["battery_reserve_soc_entity_id"], "number", "battery reserve SOC"),
        ),
        capability=SolisCapabilityConfig(
            _entity(capability["battery_max_charge_current_entity_id"], "number", "maximum charge current"),
            _entity(capability["battery_max_discharge_current_entity_id"], "number", "maximum discharge current"),
        ),
        slots=_slots(root["slot_entity_prefix"], root["slot_allocations"]),
        midnight_end=str(midnight_end),
    )
    _unique_entities(result)
    return result


def _slots(prefix_value: object, allocations_value: object) -> tuple[SolisSlotConfig, ...]:
    if not isinstance(prefix_value, str) or re.fullmatch(r"[a-z0-9_]+", prefix_value) is None:
        raise ValueError("solis.slot_entity_prefix must be lowercase text")
    source = _mapping(allocations_value, "solis.slot_allocations")
    expected = {owner.value for owner in SlotOwner}
    if set(source) != expected:
        raise ValueError("solis.slot_allocations must define exactly the three owners")
    allocations: dict[SlotOwner, tuple[int, int]] = {}
    for owner in SlotOwner:
        values = source[owner.value]
        if not isinstance(values, (list, tuple)) or len(values) != 2:
            raise ValueError(f"solis.slot_allocations.{owner.value} must contain two slots")
        if any(type(value) is not int or not 1 <= value <= 6 for value in values) or values[0] == values[1]:
            raise ValueError(f"solis.slot_allocations.{owner.value} contains an invalid slot")
        allocations[owner] = (values[0], values[1])

    def direction(slot: int, kind: SlotDirection) -> SolisDirectionConfig:
        owner = next(
            (
                candidate
                for candidate, allocated in allocations.items()
                if slot in allocated
                and (
                    (kind is SlotDirection.CHARGE and candidate is SlotOwner.CHEAP_CHARGING)
                    or (kind is SlotDirection.DISCHARGE and candidate is not SlotOwner.CHEAP_CHARGING)
                )
            ),
            None,
        )
        base = f"{prefix_value}_slot{slot}_{kind.value}"
        return SolisDirectionConfig(
            f"switch.{base}", f"text.{base}_time", f"number.{base}_current",
            f"number.{base}_soc", owner,
        )

    slots = tuple(
        SolisSlotConfig(slot, direction(slot, SlotDirection.CHARGE), direction(slot, SlotDirection.DISCHARGE))
        for slot in range(1, 7)
    )
    for owner in SlotOwner:
        direction_kind = SlotDirection.CHARGE if owner is SlotOwner.CHEAP_CHARGING else SlotDirection.DISCHARGE
        found = tuple(
            slot.physical_slot for slot in slots
            if (slot.charge if direction_kind is SlotDirection.CHARGE else slot.discharge).owner is owner
        )
        if found != allocations[owner]:
            raise ValueError(f"solis slot allocation for {owner.value} is not ordered and unique")
    return slots


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be a mapping with string keys")
    return value


def _keys(source: Mapping[str, object], expected: set[str], name: str) -> None:
    missing = expected - set(source)
    unknown = set(source) - expected
    if missing:
        raise ValueError(f"{name} is missing: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{name} contains unknown keys: {', '.join(sorted(unknown))}")


def _entity(value: object, domain: str, name: str) -> str:
    if not isinstance(value, str) or (match := _ENTITY_PATTERN.fullmatch(value)) is None or match.group("domain") != domain:
        raise ValueError(f"solis {name} must be a {domain} entity ID")
    return value


def _enum(enum: type[Enum], value: object, name: str):
    try:
        return enum(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} has an unknown value: {value}") from None


def _unique_entities(config: SolisConfig) -> None:
    values: list[str] = []
    for group in (config.telemetry, config.persistent, config.protection, config.capability):
        values.extend(
            value for field in fields(group)
            if isinstance(value := getattr(group, field.name), str) and "." in value
        )
    for slot in config.slots:
        for direction in (slot.charge, slot.discharge):
            values.extend(
                value for field in fields(direction)
                if isinstance(value := getattr(direction, field.name), str) and "." in value
            )
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise ValueError("managed Solis entity IDs must be globally unique: " + ", ".join(duplicates))


@dataclass(frozen=True, slots=True)
class Revision:
    entity_id: str
    state: str
    last_updated: datetime
    context_id: str | None


@dataclass(frozen=True, slots=True)
class SolisIssue:
    code: str
    entity_id: str | None
    message: str


@dataclass(frozen=True, slots=True)
class SolisTelemetry:
    state_of_charge_percent: Decimal
    battery_power_kw: Decimal
    battery_voltage_v: Decimal
    device_timestamp: datetime


@dataclass(frozen=True, slots=True)
class SolisPersistentState:
    storage_mode: str
    allow_grid_charging: bool
    inverter_time: datetime
    battery_reserve: bool
    battery_reserve_soc: ObservedCapability


@dataclass(frozen=True, slots=True)
class NativeSchedule:
    start_minute: int
    end_minute: int
    midnight_end: bool = False

    def ranges(self) -> tuple[tuple[int, int], ...]:
        if self.start_minute == self.end_minute:
            return ()
        if self.midnight_end or self.start_minute < self.end_minute:
            return ((self.start_minute, self.end_minute),)
        return ((self.start_minute, 1440), (0, self.end_minute))


@dataclass(frozen=True, slots=True)
class SolisDirectionState:
    key: "SlotKey"
    owner: SlotOwner | None
    enabled: bool | None
    time_text: str | None
    schedule: NativeSchedule | None
    current: ObservedCapability | None
    target_soc: ObservedCapability | None


@dataclass(frozen=True, slots=True)
class SolisSlotState:
    physical_slot: int
    charge: SolisDirectionState
    discharge: SolisDirectionState


@dataclass(frozen=True, slots=True)
class SolisState:
    health: ControllerHealth
    telemetry: SolisTelemetry | None
    persistent: SolisPersistentState | None
    capabilities: RuntimeCapabilities | None
    slots: tuple[SolisSlotState, ...]
    revisions: Mapping[str, Revision]
    issues: tuple[SolisIssue, ...]
    observed_at: datetime

    def direction(self, key: "SlotKey") -> SolisDirectionState:
        slot = self.slots[key.physical_slot - 1]
        return slot.charge if key.direction is SlotDirection.CHARGE else slot.discharge

    def revision(self, entity_id: str) -> Revision | None:
        return self.revisions.get(entity_id)


@dataclass(frozen=True, slots=True)
class SlotKey:
    physical_slot: int
    direction: SlotDirection

    def __post_init__(self) -> None:
        if type(self.physical_slot) is not int or not 1 <= self.physical_slot <= 6:
            raise ValueError("physical_slot must be from 1 through 6")
        if type(self.direction) is not SlotDirection:
            raise ValueError("direction must be charge or discharge")


class _Reader:
    def __init__(self, hass: object, config: SolisConfig, now: datetime) -> None:
        _aware(now, "now")
        self.states = getattr(hass, "states", hass)
        self.config = config
        self.now = now
        self.issues: list[SolisIssue] = []
        self.revisions: dict[str, Revision] = {}

    def read(self) -> SolisState:
        telemetry = self._telemetry()
        persistent = self._persistent()
        charge = self._capability(self.config.capability.battery_max_charge_current_entity_id, "A", "maximum charge current")
        discharge = self._capability(self.config.capability.battery_max_discharge_current_entity_id, "A", "maximum discharge current")
        slots = tuple(
            SolisSlotState(
                slot.physical_slot,
                self._direction(slot.physical_slot, SlotDirection.CHARGE, slot.charge),
                self._direction(slot.physical_slot, SlotDirection.DISCHARGE, slot.discharge),
            )
            for slot in self.config.slots
        )
        capabilities = None
        if charge is not None and discharge is not None:
            capabilities = RuntimeCapabilities(charge, discharge)
        complete = (
            telemetry is not None and persistent is not None and capabilities is not None
            and all(
                direction.enabled is not None and direction.schedule is not None
                and direction.current is not None and direction.target_soc is not None
                for slot in slots for direction in (slot.charge, slot.discharge)
            )
        )
        return SolisState(
            ControllerHealth.HEALTHY if complete and not self.issues else ControllerHealth.DEGRADED,
            telemetry, persistent, capabilities, slots, MappingProxyType(dict(self.revisions)),
            tuple(self.issues), self.now,
        )

    def _telemetry(self) -> SolisTelemetry | None:
        source = self.config.telemetry
        soc = self._number(source.state_of_charge_entity_id, "SOC")
        self._observation(self._state(source.state_of_charge_entity_id), source.state_of_charge_entity_id, "SOC")
        if soc is not None and not Decimal(0) <= soc <= Decimal(FULL_SOC_PERCENT):
            self._issue("soc_out_of_range", source.state_of_charge_entity_id, "SOC must be between 0 and 100 percent")
            soc = None
        power = self._number(source.battery_power_entity_id, "battery power")
        power_state = self._state(source.battery_power_entity_id)
        self._observation(power_state, source.battery_power_entity_id, "battery power")
        power_unit = _attribute(power_state, "unit_of_measurement")
        if power is not None:
            if isinstance(power_unit, str) and power_unit.lower() == "w":
                power /= Decimal(1000)
            elif not isinstance(power_unit, str) or power_unit.lower() != "kw":
                self._issue("battery_power_unit_unknown", source.battery_power_entity_id, f"unsupported battery-power unit: {power_unit}")
                power = None
            if power is not None and source.battery_power_sign is BatteryPowerSign.POSITIVE_MEANS_DISCHARGING:
                power = -power
        voltage = self._number(source.battery_voltage_entity_id, "battery voltage")
        voltage_state = self._state(source.battery_voltage_entity_id)
        if voltage is not None and (
            not isinstance(_attribute(voltage_state, "unit_of_measurement"), str)
            or str(_attribute(voltage_state, "unit_of_measurement")).lower() != "v"
            or voltage <= 0
        ):
            self._issue("battery_voltage_invalid", source.battery_voltage_entity_id, "battery voltage must be positive V")
            voltage = None
        timestamp = _parse_datetime(_state_value(self._state(source.device_timestamp_entity_id)))
        if timestamp is None:
            self._issue("device_timestamp_invalid", source.device_timestamp_entity_id, "device timestamp is invalid or naive")
        else:
            age = self.now - timestamp
            if age < -MAXIMUM_FUTURE_CLOCK_SKEW:
                self._issue("device_timestamp_future", source.device_timestamp_entity_id, "device timestamp is too far in the future")
            elif age > MAXIMUM_TELEMETRY_AGE:
                self._issue("device_timestamp_stale", source.device_timestamp_entity_id, "device timestamp is stale")
        if None in (soc, power, voltage, timestamp):
            return None
        return SolisTelemetry(soc, power, voltage, timestamp)  # type: ignore[arg-type]

    def _persistent(self) -> SolisPersistentState | None:
        source = self.config.persistent
        mode_state = self._state(source.storage_mode_entity_id, revision=True)
        mode = _state_value(mode_state)
        options = _attribute(mode_state, "options")
        if not isinstance(options, (list, tuple)) or not {StorageMode.SELF_USE.value, StorageMode.FEED_IN_PRIORITY.value}.issubset(options):
            self._issue("storage_mode_options_invalid", source.storage_mode_entity_id, "storage mode must advertise Self-Use and Feed-In Priority")
            mode = None
        elif mode not in options or mode in _UNKNOWN:
            self._issue("storage_mode_invalid", source.storage_mode_entity_id, "storage mode is unavailable or unadvertised")
            mode = None
        allow_grid = self._switch(source.allow_grid_charging_entity_id)
        inverter_state = self._state(source.inverter_time_entity_id, revision=True)
        sampled = _parse_datetime(_state_value(inverter_state))
        observed = _updated(inverter_state)
        inverter_time = None
        if sampled is None or observed is None:
            self._issue("inverter_datetime_invalid", source.inverter_time_entity_id, "inverter time or observation revision is invalid")
        elif observed > self.now:
            self._issue("inverter_datetime_observation_future", source.inverter_time_entity_id, "inverter time observation is in the future")
        else:
            inverter_time = sampled + (self.now - observed)
            if abs(inverter_time.astimezone(dt_timezone.utc) - self.now.astimezone(dt_timezone.utc)) > MAXIMUM_INVERTER_CLOCK_SKEW:
                self._issue("inverter_datetime_skew", source.inverter_time_entity_id, "extrapolated inverter time differs from Home Assistant")
                inverter_time = None
        reserve = self._switch(self.config.protection.battery_reserve_entity_id)
        reserve_soc = self._capability(self.config.protection.battery_reserve_soc_entity_id, "%", "battery reserve SOC")
        if mode is None or allow_grid is None or inverter_time is None or reserve is None or reserve_soc is None:
            return None
        return SolisPersistentState(str(mode), allow_grid, inverter_time, reserve, reserve_soc)

    def _direction(self, physical_slot: int, kind: SlotDirection, config: SolisDirectionConfig) -> SolisDirectionState:
        enabled = self._switch(config.enable_entity_id)
        time_state = self._state(config.time_entity_id, revision=True)
        time_text = _state_value(time_state)
        schedule = _parse_schedule(time_text)
        if schedule is None or (enabled is True and not schedule.ranges()):
            self._issue("slot_time_invalid", config.time_entity_id, "slot time must be an ordered HH:MM-HH:MM interval")
        current = self._capability(config.current_entity_id, "A", f"slot {physical_slot} {kind.value} current")
        target = self._capability(config.target_soc_entity_id, "%", f"slot {physical_slot} {kind.value} target SOC")
        if enabled and config.owner is None:
            self._issue("reserved_slot_enabled", config.enable_entity_id, "an unallocated Solis direction is enabled")
        return SolisDirectionState(SlotKey(physical_slot, kind), config.owner, enabled, time_text, schedule, current, target)

    def _switch(self, entity_id: str) -> bool | None:
        value = _state_value(self._state(entity_id, revision=True))
        if value == "on":
            return True
        if value == "off":
            return False
        self._issue("switch_state_invalid", entity_id, "switch state must be exactly on or off")
        return None

    def _capability(self, entity_id: str, unit: str, name: str) -> ObservedCapability | None:
        state = self._state(entity_id, revision=True)
        values = tuple(_decimal(value) for value in (
            _state_value(state), _attribute(state, "min"), _attribute(state, "max"), _attribute(state, "step")
        ))
        observed_unit = _attribute(state, "unit_of_measurement")
        if any(value is None for value in values) or not isinstance(observed_unit, str) or observed_unit.lower() not in _unit_aliases(unit):
            self._issue("capability_invalid", entity_id, f"{name} capability or unit is invalid")
            return None
        try:
            capability = ObservedCapability(*values, observed_unit)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            self._issue("capability_invalid", entity_id, f"{name} capability is invalid: {exc}")
            return None
        if unit == "%" and not Decimal(0) <= capability.minimum <= capability.current_value <= capability.maximum <= Decimal(100):
            self._issue("capability_soc_out_of_range", entity_id, f"{name} capability is outside 0 through 100 percent")
            return None
        return capability

    def _number(self, entity_id: str, name: str) -> Decimal | None:
        value = _decimal(_state_value(self._state(entity_id)))
        if value is None:
            self._issue("numeric_state_invalid", entity_id, f"{name} must be a finite number")
        return value

    def _state(self, entity_id: str, *, revision: bool = False) -> object:
        try:
            getter = getattr(self.states, "get", None)
            state = getter(entity_id) if callable(getter) else None
        except Exception as exc:
            self._issue("state_access_failed", entity_id, f"unable to read entity: {exc}")
            return None
        if state is None:
            self._issue("state_access_unavailable", entity_id, "entity is missing")
            return None
        if revision:
            raw = _state_value(state)
            updated = _updated(state)
            if raw is None or raw in _UNKNOWN or updated is None:
                self._issue("state_revision_invalid", entity_id, "control state or revision is unavailable")
            else:
                self.revisions[entity_id] = Revision(entity_id, raw, updated, _context_id(state))
        return state

    def _observation(self, state: object, entity_id: str, name: str) -> None:
        if _updated(state) is None:
            self._issue("ha_last_updated_invalid", entity_id, f"{name} observation timestamp is missing or naive")

    def _issue(self, code: str, entity_id: str | None, message: str) -> None:
        self.issues.append(SolisIssue(code, entity_id, message))


def read_state(hass: object, config: SolisConfig, *, now: datetime) -> SolisState:
    return _Reader(hass, config, now).read()


def split_intent(intent: LogicalIntent, *, timezone: tzinfo, midnight_end: str) -> LogicalIntent:
    """Split one logical interval at inverter-local midnight, at most once."""

    if midnight_end not in MIDNIGHT_END_OPTIONS:
        raise ValueError("midnight_end must be 24:00 or 23:59")
    result: list[SlotIntent] = []
    for segment in intent.segments:
        local_start = segment.start.astimezone(timezone)
        local_end = segment.end.astimezone(timezone)
        if local_end.date() == local_start.date() or (
            local_end.time() == time(0, 0) and local_end.date() == local_start.date() + timedelta(days=1)
        ):
            result.append(segment)
            continue
        if local_end.date() != local_start.date() + timedelta(days=1):
            raise ValueError("Solis logical intent may cross at most one local midnight")
        midnight_local = datetime.combine(local_start.date() + timedelta(days=1), time(0), timezone)
        midnight = midnight_local.astimezone(dt_timezone.utc)
        if not segment.start < midnight < segment.end:
            raise ValueError("local-midnight split is not inside the logical interval")
        result.extend((replace(segment, end=midnight), replace(segment, start=midnight)))
    return LogicalIntent(tuple(result))


def _parse_schedule(value: object) -> NativeSchedule | None:
    if not isinstance(value, str) or (match := _TIME_PATTERN.fullmatch(value)) is None:
        return None
    start_text, end_text = match.group("start"), match.group("end")
    start = _minute(start_text)
    if end_text == "24:00":
        return NativeSchedule(start, 1440, True)
    return NativeSchedule(start, _minute(end_text))


def _minute(value: str) -> int:
    hour, minute = (int(part) for part in value.split(":"))
    return hour * 60 + minute


def _aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _field(value: object, name: str, default: object = None) -> object:
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def _state_value(state: object) -> str | None:
    value = _field(state, "state")
    return value if isinstance(value, str) else None


def _attribute(state: object, name: str) -> object:
    attributes = _field(state, "attributes", {})
    return attributes.get(name) if isinstance(attributes, Mapping) else None


def _updated(state: object) -> datetime | None:
    value = _field(state, "last_updated")
    return value if isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None else None


def _context_id(state: object) -> str | None:
    context = _field(state, "context")
    value = _field(context, "id") if context is not None else _field(state, "context_id")
    return value if isinstance(value, str) else None


def _parse_datetime(value: object) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)) or (
        isinstance(value, str) and re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value.strip())
    ):
        try:
            return datetime.fromtimestamp(float(Decimal(str(value))), dt_timezone.utc)
        except (ArithmeticError, OSError, OverflowError, ValueError):
            return None
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return result if result.tzinfo is not None and result.utcoffset() is not None else None


def _unit_aliases(unit: str) -> frozenset[str]:
    return {"%": frozenset(("%", "percent")), "A": frozenset(("a", "amp", "amps", "ampere", "amperes"))}.get(unit, frozenset((unit.lower(),)))


class WriteOutcome(str, Enum):
    NO_CHANGE = "no_change"
    APPLIED = "applied"
    CONFLICT = "conflict"
    REJECTED = "rejected"
    SERVICE_ERROR = "service_error"
    SERVICE_TIMEOUT = "service_timeout"
    READBACK_TIMEOUT = "readback_timeout"


@dataclass(frozen=True, slots=True)
class SolisChange:
    entity_id: str
    target: bool | str | Decimal | datetime
    precondition: Revision
    capability: ObservedCapability | None = None

    def __post_init__(self) -> None:
        if self.entity_id != self.precondition.entity_id:
            raise ValueError("change entity must match its captured precondition")
        domain = self.entity_id.split(".", 1)[0]
        if domain not in {"switch", "select", "number", "text", "datetime"}:
            raise ValueError("change has an unsupported Home Assistant domain")
        if domain == "number" and self.capability is None:
            raise ValueError("number change requires its observed capability")
        if domain != "number" and self.capability is not None:
            raise ValueError("only number changes carry capability metadata")


@dataclass(frozen=True, slots=True)
class WriteResult:
    entity_id: str
    outcome: WriteOutcome
    message: str = ""

    @property
    def success(self) -> bool:
        return self.outcome in {WriteOutcome.NO_CHANGE, WriteOutcome.APPLIED}


@dataclass(frozen=True, slots=True)
class _NativeIntent:
    key: SlotKey
    segment: SlotIntent
    time_text: str
    schedule: NativeSchedule


class SolisAdapter:
    """One serialized CAS/readback writer for the configured Solis entities."""

    def __init__(self, hass: object, config: SolisConfig, *, timezone: tzinfo) -> None:
        self.hass = hass
        self.config = config
        self.timezone = timezone
        self._lock = asyncio.Lock()
        self._managed = frozenset(_writable_entities(config))

    def next_start_change(
        self,
        state: SolisState,
        intent: LogicalIntent | None,
        *,
        reserve_soc_percent: Decimal,
    ) -> SolisChange | None:
        """Return the next ordered idempotent start change, never a stop."""

        if state.health is not ControllerHealth.HEALTHY or state.persistent is None:
            return None
        reserve = _quantize(
            max(Decimal(MINIMUM_SOC_PERCENT), reserve_soc_percent),
            state.persistent.battery_reserve_soc,
        )
        persistent_targets: tuple[tuple[str, object, ObservedCapability | None], ...] = (
            (self.config.persistent.storage_mode_entity_id, StorageMode.FEED_IN_PRIORITY.value, None),
            (self.config.persistent.allow_grid_charging_entity_id, True, None),
            (self.config.protection.battery_reserve_soc_entity_id, reserve, state.persistent.battery_reserve_soc),
            (self.config.protection.battery_reserve_entity_id, True, None),
        )
        for entity_id, target, capability in persistent_targets:
            change = self._change(state, entity_id, target, capability)
            if change is not None:
                return change

        # Valid idle reconciles the operating policy only. Slot cleanup and
        # stop selection remain controller responsibilities.
        if intent is None:
            return None
        native = self._native_intent(intent)
        if _native_overlap(native):
            raise ValueError("logical Solis segments overlap")

        directions = tuple(
            direction for slot in state.slots for direction in (slot.charge, slot.discharge)
        )
        if any(direction.enabled is None for direction in directions):
            return None
        desired = {item.key: item for item in native}
        for direction in directions:
            if not direction.enabled:
                continue
            expected = desired.get(direction.key)
            if expected is None or not _direction_matches(direction, expected):
                return None

        for item in native:
            direction = state.direction(item.key)
            if direction.enabled:
                continue
            config = self.config.direction(item.key)
            for entity_id, target, capability in (
                (config.time_entity_id, item.time_text, None),
                (config.current_entity_id, item.segment.current, direction.current),
                (config.target_soc_entity_id, item.segment.target_soc, direction.target_soc),
                (config.enable_entity_id, True, None),
            ):
                change = self._change(state, entity_id, target, capability)
                if change is not None:
                    return change
        return None

    def intent_matches(
        self,
        state: SolisState,
        intent: LogicalIntent | None,
        *,
        reserve_soc_percent: Decimal,
    ) -> bool:
        """Return whether the full policy and complete desired intent match."""

        if self.next_start_change(
            state, intent, reserve_soc_percent=reserve_soc_percent
        ) is not None:
            return False
        if state.health is not ControllerHealth.HEALTHY or state.persistent is None:
            return False
        reserve = _quantize(
            max(Decimal(MINIMUM_SOC_PERCENT), reserve_soc_percent),
            state.persistent.battery_reserve_soc,
        )
        if (
            state.persistent.storage_mode != StorageMode.FEED_IN_PRIORITY.value
            or not state.persistent.allow_grid_charging
            or not state.persistent.battery_reserve
            or state.persistent.battery_reserve_soc.current_value != reserve
        ):
            return False
        enabled = tuple(
            direction for slot in state.slots for direction in (slot.charge, slot.discharge)
            if direction.enabled
        )
        if intent is None:
            return True
        native = self._native_intent(intent)
        expected = {item.key: item for item in native}
        return len(enabled) == len(native) and all(
            direction.key in expected and _direction_matches(direction, expected[direction.key])
            for direction in enabled
        )

    async def apply(self, change: SolisChange, *, deadline: float) -> WriteResult:
        if change.entity_id not in self._managed:
            return WriteResult(change.entity_id, WriteOutcome.REJECTED, "entity is not in the configured Solis map")
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return WriteResult(change.entity_id, WriteOutcome.SERVICE_TIMEOUT, "operation deadline exhausted")
        try:
            await asyncio.wait_for(self._lock.acquire(), remaining)
        except asyncio.TimeoutError:
            return WriteResult(change.entity_id, WriteOutcome.SERVICE_TIMEOUT, "writer lock deadline exhausted")
        try:
            return await self._apply_locked(change, deadline=deadline)
        finally:
            self._lock.release()

    async def stop(self, slot_key: SlotKey, *, deadline: float) -> WriteResult:
        config = self.config.direction(slot_key)
        revision = _capture_revision(self._get(config.enable_entity_id), config.enable_entity_id)
        if revision is None:
            return WriteResult(config.enable_entity_id, WriteOutcome.REJECTED, "slot enable is unknown or has no revision")
        if revision.state == "off":
            return WriteResult(config.enable_entity_id, WriteOutcome.NO_CHANGE, "slot is already off")
        if revision.state != "on":
            return WriteResult(config.enable_entity_id, WriteOutcome.REJECTED, "slot enable is not on or off")
        return await self.apply(SolisChange(config.enable_entity_id, False, revision), deadline=deadline)

    async def set_mode(self, mode: StorageMode, *, deadline: float) -> WriteResult:
        if mode not in (StorageMode.SELF_USE, StorageMode.FEED_IN_PRIORITY):
            return WriteResult(self.config.persistent.storage_mode_entity_id, WriteOutcome.REJECTED, "unsupported storage mode")
        entity_id = self.config.persistent.storage_mode_entity_id
        revision = _capture_revision(self._get(entity_id), entity_id)
        if revision is None:
            return WriteResult(entity_id, WriteOutcome.REJECTED, "storage mode is unknown or has no revision")
        return await self.apply(SolisChange(entity_id, mode.value, revision), deadline=deadline)

    def _native_intent(self, intent: LogicalIntent) -> tuple[_NativeIntent, ...]:
        split = split_intent(intent, timezone=self.timezone, midnight_end=self.config.midnight_end)
        allocation = self.config.allocation(split.segments[0].owner)
        return tuple(
            _NativeIntent(allocation[index], segment, *_encode_schedule(
                segment, timezone=self.timezone, midnight_end=self.config.midnight_end
            ))
            for index, segment in enumerate(split.segments)
        )

    def _change(
        self,
        state: SolisState,
        entity_id: str,
        target: object,
        capability: ObservedCapability | None,
    ) -> SolisChange | None:
        revision = state.revision(entity_id)
        if revision is None:
            raise ValueError(f"Solis control has no captured revision: {entity_id}")
        if _target_matches(entity_id, target, revision.state):
            return None
        if entity_id.startswith("number."):
            if capability is None:
                raise ValueError(f"Solis number has no capability: {entity_id}")
            target = _quantize(Decimal(str(target)), capability)
        return SolisChange(entity_id, target, revision, capability)

    def _get(self, entity_id: str) -> object:
        states = getattr(self.hass, "states", self.hass)
        getter = getattr(states, "get", None)
        return getter(entity_id) if callable(getter) else None

    def _listen(self, entity_id: str, callback: Callable[..., object]) -> Callable[[], object]:
        direct = getattr(self.hass, "async_listen_state_change", None)
        if callable(direct):
            remove = direct(entity_id, callback)
            return remove if callable(remove) else (lambda: None)
        try:
            from homeassistant.helpers.event import async_track_state_change_event

            return async_track_state_change_event(self.hass, [entity_id], callback)
        except (AttributeError, TypeError):
            return lambda: None

    async def _apply_locked(self, change: SolisChange, *, deadline: float) -> WriteResult:
        current = self._get(change.entity_id)
        if not _revision_matches(current, change.precondition):
            return WriteResult(change.entity_id, WriteOutcome.CONFLICT, "captured HA revision no longer matches")
        validated = _service(change, current)
        if isinstance(validated, WriteResult):
            return validated
        normalized, domain, service, data = validated
        if _value_matches(change.entity_id, normalized, _state_value(current)):
            return WriteResult(change.entity_id, WriteOutcome.NO_CHANGE, "Home Assistant already has target")

        event = asyncio.Event()
        in_flight = False
        optimistic_match = False

        def changed(*args: object, **kwargs: object) -> None:
            nonlocal optimistic_match
            candidate = _event_state(args, kwargs)
            if (
                in_flight and candidate is not None
                and _value_matches(change.entity_id, normalized, _state_value(candidate))
                and _new_revision(candidate, change.precondition)
            ):
                optimistic_match = True
            event.set()

        remove = self._listen(change.entity_id, changed)
        try:
            current = self._get(change.entity_id)
            if not _revision_matches(current, change.precondition):
                return WriteResult(change.entity_id, WriteOutcome.CONFLICT, "captured HA revision changed before service call")
            in_flight = True
            try:
                await self._call_service(domain, service, data, deadline=deadline)
            except asyncio.TimeoutError:
                return WriteResult(change.entity_id, WriteOutcome.SERVICE_TIMEOUT, "blocking Home Assistant service timed out; outcome is ambiguous")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return WriteResult(change.entity_id, WriteOutcome.SERVICE_ERROR, f"Home Assistant service failed; outcome is ambiguous: {exc}")
            finally:
                in_flight = False
            if optimistic_match or _matching_new_revision(self._get(change.entity_id), change, normalized):
                return WriteResult(change.entity_id, WriteOutcome.APPLIED, "successful service and matching HA revision")
            end = min(deadline, asyncio.get_running_loop().time() + READBACK_TIMEOUT.total_seconds())
            while True:
                remaining = end - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return WriteResult(change.entity_id, WriteOutcome.READBACK_TIMEOUT, "successful service had no matching HA readback")
                event.clear()
                if _matching_new_revision(self._get(change.entity_id), change, normalized):
                    return WriteResult(change.entity_id, WriteOutcome.APPLIED, "successful service and matching HA readback")
                try:
                    await asyncio.wait_for(event.wait(), remaining)
                except asyncio.TimeoutError:
                    return WriteResult(change.entity_id, WriteOutcome.READBACK_TIMEOUT, "successful service had no matching HA readback")
        finally:
            result = remove()
            if inspect.iscoroutine(result):
                # HA's production unsubscribe is synchronous. Close an invalid
                # test-adapter coroutine without creating cleanup machinery.
                result.close()
            elif isinstance(result, asyncio.Future):
                result.cancel()

    async def _call_service(
        self, domain: str, service: str, data: Mapping[str, object], *, deadline: float
    ) -> None:
        services = getattr(self.hass, "services", None)
        call = getattr(services, "async_call", None)
        if not callable(call):
            call = getattr(self.hass, "async_call", None)
        if not callable(call):
            raise TypeError("Home Assistant service API is unavailable")
        result = call(domain, service, dict(data), blocking=True)
        if not inspect.isawaitable(result):
            raise TypeError("Home Assistant service call must return an awaitable")
        task = asyncio.ensure_future(result)
        timeout = min(SERVICE_TIMEOUT.total_seconds(), max(0.0, deadline - asyncio.get_running_loop().time()))
        try:
            done, _ = await asyncio.wait((task,), timeout=timeout)
        except asyncio.CancelledError:
            task.cancel()
            task.add_done_callback(_consume)
            raise
        if not done:
            task.cancel()
            task.add_done_callback(_consume)
            raise asyncio.TimeoutError
        task.result()


def _writable_entities(config: SolisConfig) -> tuple[str, ...]:
    values = [
        config.persistent.storage_mode_entity_id,
        config.persistent.allow_grid_charging_entity_id,
        config.persistent.inverter_time_entity_id,
        config.protection.battery_reserve_entity_id,
        config.protection.battery_reserve_soc_entity_id,
    ]
    for slot in config.slots:
        for direction in (slot.charge, slot.discharge):
            values.extend((direction.enable_entity_id, direction.time_entity_id, direction.current_entity_id, direction.target_soc_entity_id))
    return tuple(values)


def _encode_schedule(
    segment: SlotIntent, *, timezone: tzinfo, midnight_end: str
) -> tuple[str, NativeSchedule]:
    local_start = segment.start.astimezone(timezone)
    local_end = segment.end.astimezone(timezone)
    for value in (local_start, local_end):
        if value.second or value.microsecond:
            raise ValueError("Solis schedule boundaries must be exact minutes")
    start_text = local_start.strftime("%H:%M")
    if local_end.date() == local_start.date():
        end_text = local_end.strftime("%H:%M")
    elif local_end.date() == local_start.date() + timedelta(days=1) and local_end.time() == time(0):
        end_text = midnight_end
    else:
        raise ValueError("native Solis segment must end on the same day or local midnight")
    text = f"{start_text}-{end_text}"
    schedule = _parse_schedule(text)
    if schedule is None or not schedule.ranges():
        raise ValueError("native Solis schedule is empty or invalid")
    return text, schedule


def _native_overlap(items: tuple[_NativeIntent, ...]) -> bool:
    return any(
        _schedules_overlap(left.schedule, right.schedule)
        for index, left in enumerate(items)
        for right in items[index + 1 :]
    )


def _schedules_overlap(left: NativeSchedule, right: NativeSchedule) -> bool:
    return any(max(a, c) < min(b, d) for a, b in left.ranges() for c, d in right.ranges())


def _direction_matches(state: SolisDirectionState, desired: _NativeIntent) -> bool:
    return (
        state.enabled is True
        and state.time_text == desired.time_text
        and state.current is not None
        and state.current.current_value == desired.segment.current
        and state.target_soc is not None
        and state.target_soc.current_value == desired.segment.target_soc
    )


def _quantize(target: Decimal, capability: ObservedCapability) -> Decimal:
    if not target.is_finite():
        raise ValueError("Solis number target must be finite")
    result = capability.minimum + (
        (max(target, capability.minimum) - capability.minimum) / capability.step
    ).to_integral_value(rounding=ROUND_CEILING) * capability.step
    if result > capability.maximum:
        raise ValueError("Solis number target exceeds its observed capability")
    return result


def _target_matches(entity_id: str, target: object, raw: str) -> bool:
    if entity_id.startswith("switch."):
        return raw == ("on" if target is True else "off")
    if entity_id.startswith("number."):
        return _decimal(raw) == _decimal(target)
    if entity_id.startswith("datetime.") and isinstance(target, datetime):
        return _parse_datetime(raw) == target
    return raw == str(target)


def _service(change: SolisChange, state: object) -> tuple[str, str, str, Mapping[str, object]] | WriteResult:
    domain = change.entity_id.split(".", 1)[0]
    target = change.target
    if domain == "switch" and isinstance(target, bool):
        normalized = "on" if target else "off"
        return normalized, domain, "turn_on" if target else "turn_off", {"entity_id": change.entity_id}
    if domain == "select" and isinstance(target, str):
        options = _attribute(state, "options")
        if not isinstance(options, (list, tuple)) or target not in options:
            return WriteResult(change.entity_id, WriteOutcome.REJECTED, "select target is not advertised")
        return target, domain, "select_option", {"entity_id": change.entity_id, "option": target}
    if domain == "text" and isinstance(target, str) and _parse_schedule(target) is not None:
        return target, domain, "set_value", {"entity_id": change.entity_id, "value": target}
    if domain == "number" and isinstance(target, Decimal) and change.capability is not None:
        live = _read_capability(state)
        if live != change.capability:
            return WriteResult(change.entity_id, WriteOutcome.CONFLICT, "number capability changed")
        try:
            target = _quantize(target, live)
        except ValueError as exc:
            return WriteResult(change.entity_id, WriteOutcome.REJECTED, str(exc))
        return str(target), domain, "set_value", {"entity_id": change.entity_id, "value": float(target)}
    if domain == "datetime" and isinstance(target, datetime) and target.tzinfo is not None:
        normalized = target.isoformat()
        return normalized, domain, "set_value", {"entity_id": change.entity_id, "datetime": normalized}
    return WriteResult(change.entity_id, WriteOutcome.REJECTED, "target does not match the configured entity domain")


def _read_capability(state: object) -> ObservedCapability | None:
    values = tuple(_decimal(value) for value in (
        _state_value(state), _attribute(state, "min"), _attribute(state, "max"), _attribute(state, "step")
    ))
    unit = _attribute(state, "unit_of_measurement")
    if any(value is None for value in values) or not isinstance(unit, str):
        return None
    try:
        return ObservedCapability(*values, unit)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _capture_revision(state: object, entity_id: str) -> Revision | None:
    raw, updated = _state_value(state), _updated(state)
    if raw is None or raw in _UNKNOWN or updated is None:
        return None
    return Revision(entity_id, raw, updated, _context_id(state))


def _revision_matches(state: object, expected: Revision) -> bool:
    current = _capture_revision(state, expected.entity_id)
    return current == expected


def _new_revision(state: object, expected: Revision) -> bool:
    updated = _updated(state)
    return (
        updated is not None and updated > expected.last_updated
    ) or _context_id(state) != expected.context_id


def _value_matches(entity_id: str, normalized: str, raw: str | None) -> bool:
    if raw is None:
        return False
    return _decimal(raw) == _decimal(normalized) if entity_id.startswith("number.") else raw == normalized


def _matching_new_revision(state: object, change: SolisChange, normalized: str) -> bool:
    return _value_matches(change.entity_id, normalized, _state_value(state)) and _new_revision(state, change.precondition)


def _event_state(args: tuple[object, ...], kwargs: Mapping[str, object]) -> object | None:
    for value in args:
        data = _field(value, "data")
        if isinstance(data, Mapping) and "new_state" in data:
            return data["new_state"]
        if isinstance(value, Mapping) and "state" in value:
            return value
    data = kwargs.get("data")
    return data.get("new_state") if isinstance(data, Mapping) else None


def _consume(task: asyncio.Future[object]) -> None:
    try:
        task.result()
    except BaseException:
        pass


__all__ = [
    "BatteryPowerSign", "MAXIMUM_TELEMETRY_AGE", "NativeSchedule", "Revision",
    "SlotKey", "SolisAdapter", "SolisChange", "SolisConfig", "SolisDirectionState",
    "SolisIssue", "SolisPersistentState", "SolisSlotState", "SolisState",
    "SolisTelemetry", "WriteOutcome", "WriteResult", "config_from_mapping",
    "read_state", "split_intent",
]
