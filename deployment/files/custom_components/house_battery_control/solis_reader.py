"""Read-only Home Assistant boundary for the configured Solis entities.

The adapter deliberately has no Home Assistant imports.  Home Assistant's
state registry (or a deterministic fake in tests) is supplied by the caller,
which keeps this boundary usable during offline commissioning and testing.
"""

from collections.abc import Mapping
from datetime import datetime, time, timezone
from decimal import Decimal, InvalidOperation
import re
from typing import Protocol

from .contracts import (
    ControllerHealth,
    ObservedCapability,
    RuntimeCapabilities,
    SlotDirection,
)
from .domain_constants import FULL_SOC_PERCENT
from .solis_config import SolisConfig, SolisSlotDirectionConfig, SolisSlotOwner
from .solis_state import (
    MAXIMUM_FUTURE_CLOCK_SKEW,
    MAXIMUM_TELEMETRY_AGE,
    IssueSeverity,
    SolisIssue,
    SolisPersistentState,
    SolisSlotCapability,
    SolisSlotDirectionState,
    SolisSlotState,
    SolisStateReadResult,
    SolisStateSnapshot,
    SolisTelemetry,
)


class StateAccess(Protocol):
    """Minimal injected state access used by :class:`SolisStateReader`."""

    def get(self, entity_id: str, default: object = None) -> object:
        ...


_TIME_PATTERN = re.compile(r"^(?P<start>[0-2][0-9]:[0-5][0-9])-(?P<end>[0-2][0-9]:[0-5][0-9])$")
_UNKNOWN_STATES = frozenset(("unknown", "unavailable"))
_REQUIRED_STORAGE_MODES = frozenset(("Self-Use", "Feed-In Priority"))


class SolisStateReader:
    """Read and validate one complete configured Solis observation."""

    def __init__(
        self,
        config: SolisConfig | None,
        states: StateAccess | Mapping[str, object] | object,
        now: datetime,
    ) -> None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        self.config = config
        self.states = states
        self.now = now
        self._issues: list[SolisIssue] = []

    def read(self) -> SolisStateReadResult:
        """Return a structured result; unsafe external state never raises."""

        self._issues = []
        if self.config is None:
            self._critical("solis_config_missing", None, "Solis configuration is not present")
            return self._result(None, None, None, ())

        telemetry = self._read_telemetry(self.config)
        persistent = self._read_persistent(self.config)
        slots = self._read_slots(self.config)
        snapshot = None
        if (
            telemetry is not None
            and persistent is not None
            and len(slots) == 6
            and not any(issue.critical for issue in self._issues)
        ):
            capabilities = RuntimeCapabilities(
                maximum_charge_current=self._global_capabilities["maximum_charge_current"],
                maximum_discharge_current=self._global_capabilities["maximum_discharge_current"],
            )
            snapshot = SolisStateSnapshot(
                telemetry=telemetry,
                persistent=persistent,
                capabilities=capabilities,
                slots=slots,
                observed_at=self.now,
            )
        return self._result(snapshot, telemetry, persistent, slots)

    def _result(
        self,
        snapshot: SolisStateSnapshot | None,
        telemetry: SolisTelemetry | None,
        persistent: SolisPersistentState | None,
        slots: tuple[SolisSlotState, ...],
    ) -> SolisStateReadResult:
        health = (
            ControllerHealth.HEALTHY
            if snapshot is not None and not any(issue.critical for issue in self._issues)
            else ControllerHealth.DEGRADED
        )
        return SolisStateReadResult(
            health=health,
            snapshot=snapshot,
            telemetry=telemetry,
            persistent=persistent,
            slots=slots,
            issues=tuple(self._issues),
        )

    def _read_telemetry(self, config: SolisConfig) -> SolisTelemetry | None:
        soc_id = config.telemetry.state_of_charge_entity_id
        soc_state = self._state(soc_id)
        soc = self._number_from_state(soc_state, soc_id, "soc_missing")
        if soc is not None and not Decimal(0) <= soc <= Decimal(FULL_SOC_PERCENT):
            self._critical("soc_out_of_range", soc_id, "SOC must be between 0 and 100 percent")
            soc = None

        power_id = config.telemetry.battery_power_entity_id
        sign = config.telemetry.battery_power_sign
        power: Decimal | None = None
        power_state = self._state(power_id)
        raw_power = self._number_from_state(power_state, power_id, "battery_power_missing")
        if raw_power is not None:
            unit = self._attribute(power_state, "unit_of_measurement")
            if not isinstance(unit, str) or not unit.strip():
                self._critical("battery_power_unit_missing", power_id, "battery-power unit is missing")
            elif unit.strip().lower() == "kw":
                power = raw_power
            elif unit.strip().lower() == "w":
                power = raw_power / Decimal("1000")
            else:
                self._critical("battery_power_unit_unknown", power_id, f"unsupported battery-power unit: {unit}")
            if power is not None and sign.value == "positive_means_discharging":
                power = -power

        voltage_id = config.telemetry.battery_voltage_entity_id
        voltage_state = self._state(voltage_id)
        voltage = self._number_from_state(voltage_state, voltage_id, "battery_voltage_missing")
        voltage_unit = self._attribute(voltage_state, "unit_of_measurement")
        if voltage is not None and (
            not isinstance(voltage_unit, str) or voltage_unit.strip().lower() != "v"
        ):
            self._critical("battery_voltage_unit_invalid", voltage_id, "battery voltage unit must be V")
            voltage = None
        if voltage is not None and voltage <= 0:
            self._critical("battery_voltage_invalid", voltage_id, "battery voltage must be positive")
            voltage = None

        soc_last_updated = self._observation_timestamp(soc_state, soc_id, "SOC")
        power_last_updated = (
            self._observation_timestamp(power_state, power_id, "battery power")
            if power_id is not None
            else None
        )
        device_timestamp: datetime | None = None
        timestamp_id = config.telemetry.device_timestamp_entity_id
        timestamp_state = self._state(timestamp_id)
        raw_timestamp = self._state_value(timestamp_state)
        device_timestamp = self._parse_datetime(raw_timestamp)
        if device_timestamp is None:
            self._critical(
                "device_timestamp_invalid",
                timestamp_id,
                "device timestamp is missing, invalid or naive",
            )
        else:
            age = self.now - device_timestamp
            if age < -MAXIMUM_FUTURE_CLOCK_SKEW:
                self._critical("device_timestamp_future", timestamp_id, "device timestamp is too far in the future")
            elif age > MAXIMUM_TELEMETRY_AGE:
                self._critical("device_timestamp_stale", timestamp_id, "device timestamp is stale")

        if soc is None or power is None or voltage is None or device_timestamp is None:
            return None
        return SolisTelemetry(
            state_of_charge_percent=soc,
            battery_power_kw=power,
            battery_voltage_v=voltage,
            device_timestamp=device_timestamp,
            home_assistant_last_updated=soc_last_updated,
            soc_last_updated=soc_last_updated,
            power_last_updated=power_last_updated,
        )

    def _read_persistent(self, config: SolisConfig) -> SolisPersistentState | None:
        persistent = config.persistent
        storage_state = self._state(persistent.storage_mode_entity_id)
        storage_mode_value = self._state_value(storage_state)
        options_value = self._attribute(storage_state, "options")
        options: tuple[str, ...] = ()
        if isinstance(options_value, (list, tuple)) and all(
            isinstance(value, str) for value in options_value
        ):
            options = tuple(options_value)
        else:
            self._critical(
                "storage_mode_options_invalid",
                persistent.storage_mode_entity_id,
                "storage-mode options are missing or invalid",
            )
        if not _REQUIRED_STORAGE_MODES.issubset(options):
            self._critical(
                "storage_mode_options_missing",
                persistent.storage_mode_entity_id,
                "storage-mode options must include Self-Use and Feed-In Priority",
            )
        storage_mode = storage_mode_value if isinstance(storage_mode_value, str) else ""
        if storage_mode in _UNKNOWN_STATES or not storage_mode or storage_mode not in options:
            self._critical(
                "storage_mode_invalid",
                persistent.storage_mode_entity_id,
                "storage mode is unavailable or not advertised",
            )

        switch_values: dict[str, bool | None] = {}
        switch_values["allow_grid_charging"] = self._switch(persistent.allow_grid_charging_entity_id)

        inverter_time_id = persistent.inverter_time_entity_id
        inverter_time = self._parse_datetime(self._state_value(self._state(inverter_time_id)))
        if inverter_time is None:
            self._critical("inverter_datetime_invalid", inverter_time_id, "inverter datetime is invalid or naive")

        protection = config.protection
        battery_reserve = self._switch(protection.battery_reserve_entity_id)
        reserve_soc = self._capability(protection.battery_reserve_soc_entity_id, "%", "battery_reserve_soc")

        if (
            switch_values["allow_grid_charging"] is None
            or inverter_time is None
            or any(
                value is None
                for value in (
                    battery_reserve,
                    reserve_soc,
                )
            )
            or not storage_mode
        ):
            return None
        return SolisPersistentState(
            storage_mode=storage_mode,
            storage_mode_options=options,
            allow_grid_charging=switch_values["allow_grid_charging"],  # type: ignore[arg-type]
            inverter_time=inverter_time,
            battery_reserve=battery_reserve,  # type: ignore[arg-type]
            battery_reserve_soc=reserve_soc,  # type: ignore[arg-type]
        )

    def _read_slots(self, config: SolisConfig) -> tuple[SolisSlotState, ...]:
        self._global_capabilities: dict[str, ObservedCapability] = {}
        self._global_capabilities["maximum_charge_current"] = self._capability(
            config.capability.battery_max_charge_current_entity_id, "A", "maximum_charge_current"
        )
        self._global_capabilities["maximum_discharge_current"] = self._capability(
            config.capability.battery_max_discharge_current_entity_id, "A", "maximum_discharge_current"
        )

        result: list[SolisSlotState] = []
        enabled_directions: list[tuple[int, str, SolisSlotOwner]] = []
        for slot in config.slots:
            charge, charge_enabled = self._slot_direction(
                slot.physical_slot, "charge", slot.charge
            )
            discharge, discharge_enabled = self._slot_direction(
                slot.physical_slot, "discharge", slot.discharge
            )
            if charge_enabled:
                enabled_directions.append(
                    (slot.physical_slot, "charge", slot.charge.owner)
                )
                if slot.charge.owner is SolisSlotOwner.RESERVED:
                    self._critical(
                        "reserved_slot_enabled",
                        slot.charge.enable_entity_id,
                        f"reserved Solis charge slot {slot.physical_slot} is enabled",
                    )
            if discharge_enabled:
                enabled_directions.append(
                    (slot.physical_slot, "discharge", slot.discharge.owner)
                )
                if slot.discharge.owner is SolisSlotOwner.RESERVED:
                    self._critical(
                        "reserved_slot_enabled",
                        slot.discharge.enable_entity_id,
                        f"reserved Solis discharge slot {slot.physical_slot} is enabled",
                    )
            if charge is None or discharge is None:
                continue
            capability = SolisSlotCapability(
                physical_slot=slot.physical_slot,
                charge_current=charge.current,
                charge_target_soc=charge.target_soc,
                discharge_current=discharge.current,
                discharge_target_soc=discharge.target_soc,
            )
            result.append(SolisSlotState(slot.physical_slot, charge, discharge, capability))

        if len(enabled_directions) > 1:
            self._critical(
                "multiple_enabled_slots",
                None,
                "more than one Solis charge/discharge direction is enabled",
            )
        physical_slots = {item[0] for item in enabled_directions}
        for physical_slot in physical_slots:
            directions = {
                item[1] for item in enabled_directions if item[0] == physical_slot
            }
            if len(directions) > 1:
                self._critical(
                    "slot_direction_conflict",
                    None,
                    f"charge and discharge are both enabled for slot {physical_slot}",
                )
                break
        return tuple(sorted(result, key=lambda item: item.physical_slot))

    def _slot_direction(
        self,
        physical_slot: int,
        direction: str,
        config: SolisSlotDirectionConfig,
    ) -> tuple[SolisSlotDirectionState | None, bool]:
        enabled = self._switch(config.enable_entity_id)
        time_value = self._state_value(self._state(config.time_entity_id))
        if not isinstance(time_value, str):
            self._critical(
                "slot_time_invalid",
                config.time_entity_id,
                "slot time must be HH:MM-HH:MM text",
            )
            time_text = ""
            start = end = None
            crosses_midnight = False
        else:
            time_text = time_value
            start, end, crosses_midnight = self._parse_slot_time(
                time_text, bool(enabled), config.time_entity_id
            )
        current = self._capability(
            config.current_entity_id, "A", f"slot_{physical_slot}_{direction}_current"
        )
        target_soc = self._capability(
            config.target_soc_entity_id,
            "%",
            f"slot_{physical_slot}_{direction}_target_soc",
        )
        if enabled is None or current is None or target_soc is None or start is None or end is None:
            return None, bool(enabled)
        return SolisSlotDirectionState(
            physical_slot=physical_slot,
            direction=SlotDirection(direction),
            owner=config.owner,
            enabled=enabled,
            time_text=time_text,
            start=start,
            end=end,
            crosses_midnight=crosses_midnight,
            current=current,
            target_soc=target_soc,
        ), enabled

    def _parse_slot_time(
        self, value: str, enabled: bool, entity_id: str
    ) -> tuple[time | None, time | None, bool]:
        match = _TIME_PATTERN.fullmatch(value)
        if match is None:
            self._critical("slot_time_invalid", entity_id, "slot time must be exact HH:MM-HH:MM")
            return None, None, False
        start = self._clock_time(match.group("start"))
        end = self._clock_time(match.group("end"))
        if start is None or end is None:
            self._critical("slot_time_invalid", entity_id, "slot time contains an invalid clock value")
            return None, None, False
        if start == end:
            if enabled:
                self._critical(
                    "slot_time_zero_enabled",
                    entity_id,
                    "enabled slot cannot have a zero-length interval",
                )
                return None, None, False
            return start, end, False
        return start, end, end < start

    @staticmethod
    def _clock_time(value: str) -> time | None:
        hour, minute = (int(part) for part in value.split(":"))
        if hour > 23:
            return None
        return time(hour, minute)

    def _capability(
        self, entity_id: str, expected_unit: str | None, name: str
    ) -> ObservedCapability | None:
        state = self._state(entity_id)
        current = self._number_from_state(state, entity_id, "capability_state_invalid")
        minimum = self._attribute_decimal(state, entity_id, "min", name)
        maximum = self._attribute_decimal(state, entity_id, "max", name)
        step = self._attribute_decimal(state, entity_id, "step", name)
        unit = self._attribute(state, "unit_of_measurement")
        if not isinstance(unit, str) or not unit.strip():
            self._critical("capability_unit_invalid", entity_id, f"{name} capability unit is missing")
            unit_value = ""
        else:
            unit_value = unit.strip()
        if expected_unit is not None and unit_value.lower() not in _unit_aliases(expected_unit):
            self._critical("capability_unit_invalid", entity_id, f"{name} capability unit must be {expected_unit}")
        if any(value is None for value in (current, minimum, maximum, step)):
            return None
        if expected_unit == "%":
            for label, value in (
                ("current", current),
                ("minimum", minimum),
                ("maximum", maximum),
            ):
                if not Decimal(0) <= value <= Decimal(FULL_SOC_PERCENT):
                    self._critical(
                        "capability_soc_out_of_range",
                        entity_id,
                        f"{name} capability {label} must be from 0 through 100 percent",
                    )
        try:
            return ObservedCapability(
                current, minimum, maximum, step, unit_value
            )  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            self._critical("capability_invalid", entity_id, f"{name} capability is invalid: {exc}")
            return None

    def _attribute_decimal(
        self, state: object, entity_id: str, key: str, name: str
    ) -> Decimal | None:
        value = self._attribute(state, key)
        parsed = self._decimal(value)
        if parsed is None:
            self._critical(
                "capability_attribute_invalid",
                entity_id,
                f"{name} capability {key} is missing or non-finite",
            )
        return parsed

    def _switch(self, entity_id: str) -> bool | None:
        value = self._state_value(self._state(entity_id))
        if value == "on":
            return True
        if value == "off":
            return False
        self._critical("switch_state_invalid", entity_id, "switch state must be exactly on or off")
        return None

    def _number_from_state(self, state: object, entity_id: str, missing_code: str) -> Decimal | None:
        value = self._state_value(state)
        if value is None:
            self._critical(missing_code, entity_id, "entity is missing")
            return None
        if isinstance(value, str) and value.lower() in _UNKNOWN_STATES:
            self._critical("state_unavailable", entity_id, f"entity state is {value}")
            return None
        parsed = self._decimal(value)
        if parsed is None:
            self._critical("numeric_state_invalid", entity_id, "state must be a finite number")
        return parsed

    @staticmethod
    def _decimal(value: object) -> Decimal | None:
        if isinstance(value, bool) or value is None:
            return None
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return None
        return result if result.is_finite() else None

    def _state(self, entity_id: str) -> object:
        try:
            if isinstance(self.states, Mapping):
                return self.states.get(entity_id)
            getter = getattr(self.states, "get", None)
            if callable(getter):
                return getter(entity_id)
            getter = getattr(self.states, "get_state", None)
            if callable(getter):
                return getter(entity_id)
            registry = getattr(self.states, "states", None)
            getter = getattr(registry, "get", None)
            if callable(getter):
                return getter(entity_id)
        except Exception as exc:  # state access failures are external issues
            self._critical("state_access_failed", entity_id, f"unable to read entity state: {exc}")
            return None
        self._critical("state_access_unavailable", entity_id, "injected state access has no get method")
        return None

    @staticmethod
    def _state_value(state: object) -> object:
        if state is None:
            return None
        if isinstance(state, Mapping):
            return state.get("state")
        return getattr(state, "state", state)

    @staticmethod
    def _attribute(state: object, key: str) -> object:
        if state is None:
            return None
        if isinstance(state, Mapping):
            attrs = state.get("attributes", state)
        else:
            attrs = getattr(state, "attributes", {})
        if isinstance(attrs, Mapping):
            return attrs.get(key)
        return None

    def _observation_timestamp(
        self, state: object, entity_id: str, label: str
    ) -> datetime | None:
        if state is None:
            self._critical(
                "ha_last_updated_missing",
                entity_id,
                f"Home Assistant {label} observation timestamp is missing",
            )
            return None
        value = (
            state.get("last_updated")
            if isinstance(state, Mapping)
            else getattr(state, "last_updated", None)
        )
        if value is None:
            self._critical(
                "ha_last_updated_missing",
                entity_id,
                f"Home Assistant {label} observation timestamp is missing",
            )
            return None
        parsed = self._parse_datetime(value)
        if parsed is None:
            self._critical(
                "ha_last_updated_naive",
                entity_id,
                f"Home Assistant {label} observation timestamp is invalid or naive",
            )
        return parsed

    @staticmethod
    def _parse_datetime(value: object) -> datetime | None:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float, Decimal)) or (
            isinstance(value, str)
            and re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value.strip())
        ):
            try:
                seconds = Decimal(str(value))
                if not seconds.is_finite():
                    return None
                return datetime.fromtimestamp(float(seconds), timezone.utc)
            except (InvalidOperation, OverflowError, OSError, ValueError):
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
        if result.tzinfo is None or result.utcoffset() is None:
            return None
        return result

    def _critical(self, code: str, entity_id: str | None, message: str) -> None:
        self._issues.append(SolisIssue(code, IssueSeverity.CRITICAL, entity_id, message))

def _unit_aliases(expected: str) -> frozenset[str]:
    if expected == "%":
        return frozenset(("%", "percent"))
    if expected == "A":
        return frozenset(("a", "amp", "amps", "ampere", "amperes"))
    return frozenset((expected.lower(),))


def read_solis_state(
    config: SolisConfig | None,
    states: StateAccess | Mapping[str, object] | object,
    now: datetime,
) -> SolisStateReadResult:
    """Convenience function for one read-only Solis observation."""

    return SolisStateReader(config, states, now).read()


__all__ = ["StateAccess", "SolisStateReader", "read_solis_state"]
