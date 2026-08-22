"""Typed House Battery Control configuration."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import TypeAlias, cast

from . import battery, solis_config
from .reserve_planner import CommissionedPowerEnvelope
from .solis_policy import (
    CapabilityResolutionRecord,
    ManualGridImportVerification,
    PersistentCandidateAuthorization,
)

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
class CandidateCommissioningConfig:
    """Typed, default-disabled T0011 authority container.

    Runtime commissioning sessions are intentionally not represented here;
    this is only the future IaC shape emitted for human review.
    """

    services_enabled: bool = False
    persistent_candidate_authorization: PersistentCandidateAuthorization | None = None
    candidate_mapping_fingerprint: str | None = None
    candidate_policy_fingerprint: str | None = None
    candidate_validated_at: datetime | None = None
    manual_grid_verification: ManualGridImportVerification | None = None
    capability_resolutions: tuple[CapabilityResolutionRecord, ...] = ()
    commissioned_power_envelope: CommissionedPowerEnvelope | None = None


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

    solis: solis_config.SolisConfig | None = None
    """Optional live Solis entity mapping; omitted until the later cutover."""

    control_disable_guard_entity_id: str = "input_boolean.house_battery_control_disable"
    """Fail-closed helper; only an exact ``off`` opens observation/commissioning."""

    candidate_commissioning: CandidateCommissioningConfig = CandidateCommissioningConfig()
    """Human-reviewed authority; disabled and empty until explicitly activated."""


def from_mapping(source: Mapping[str, ConfigValue]) -> Config:
    """Map validated YAML-shaped data to typed internal configuration."""
    _require_keys(
        source,
        {"battery", "tariff", "solar", "policy"},
        "config",
        optional={"solis", "control_disable_guard_entity_id", "candidate_commissioning"},
    )

    battery_source = _mapping(source["battery"], "battery")
    _require_keys(
        battery_source,
        {
            "capacity_kwh",
            "minimum_state_of_charge_percent",
            "charge_efficiency",
            "discharge_efficiency",
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

    live_solis_config = None
    if "solis" in source:
        live_solis_config = solis_config.from_mapping(source["solis"])

    guard = _entity_id(
        source.get(
            "control_disable_guard_entity_id",
            "input_boolean.house_battery_control_disable",
        ),
        "control_disable_guard_entity_id",
    )
    if not guard.startswith(("input_boolean.", "switch.")):
        raise ValueError("control_disable_guard_entity_id must be a switch-like entity")

    candidate = _candidate_commissioning(source.get("candidate_commissioning"))

    return Config(
        battery=battery_config,
        tariff=tariff_config,
        solar=solar_config,
        policy=policy_config,
        solis=live_solis_config,
        control_disable_guard_entity_id=guard,
        candidate_commissioning=candidate,
    )


def _candidate_commissioning(value: ConfigValue) -> CandidateCommissioningConfig:
    """Parse the exact T0021 candidate authority shape when supplied."""

    if value is None:
        return CandidateCommissioningConfig()
    source = _mapping(value, "candidate_commissioning")
    expected = {
        "services_enabled", "persistent_candidate_authorization", "candidate_mapping_fingerprint",
        "candidate_policy_fingerprint", "candidate_validated_at", "manual_grid_verification",
        "capability_resolutions", "commissioned_power_envelope",
    }
    _require_keys(source, expected, "candidate_commissioning")
    enabled = source["services_enabled"]
    if not isinstance(enabled, bool):
        raise ValueError("candidate_commissioning.services_enabled must be bool")

    persistent = source["persistent_candidate_authorization"]
    persistent_record = None
    if persistent is not None:
        p = _mapping(persistent, "persistent_candidate_authorization")
        _require_keys(p, {"issued_at", "mapping_fingerprint", "policy_fingerprint", "ha_readback_validated", "device_reconciliation_validated", "schema_version"}, "persistent_candidate_authorization")
        persistent_record = PersistentCandidateAuthorization(
            _parse_datetime(p["issued_at"], "issued_at"),
            _fingerprint_text(p["mapping_fingerprint"], "mapping_fingerprint"),
            _fingerprint_text(p["policy_fingerprint"], "policy_fingerprint"),
            _bool(p["ha_readback_validated"], "ha_readback_validated"),
            _bool(p["device_reconciliation_validated"], "device_reconciliation_validated"),
            _int(p["schema_version"], "schema_version"),
        )

    mapping = _optional_fingerprint(source["candidate_mapping_fingerprint"], "candidate_mapping_fingerprint")
    policy = _optional_fingerprint(source["candidate_policy_fingerprint"], "candidate_policy_fingerprint")
    validated_at = None if source["candidate_validated_at"] is None else _parse_datetime(source["candidate_validated_at"], "candidate_validated_at")

    manual = source["manual_grid_verification"]
    manual_record = None
    if manual is not None:
        m = _mapping(manual, "manual_grid_verification")
        _require_keys(m, {"maximum_grid_import_power_kw", "verified_at", "mapping_fingerprint", "policy_fingerprint", "setting_contains_expected_value"}, "manual_grid_verification")
        manual_record = ManualGridImportVerification(
            _decimal(m["maximum_grid_import_power_kw"], "maximum_grid_import_power_kw"),
            _parse_datetime(m["verified_at"], "verified_at"),
            _fingerprint_text(m["mapping_fingerprint"], "mapping_fingerprint"),
            _fingerprint_text(m["policy_fingerprint"], "policy_fingerprint"),
            _bool(m["setting_contains_expected_value"], "setting_contains_expected_value"),
        )

    resolutions_value = source["capability_resolutions"]
    if not isinstance(resolutions_value, list):
        raise ValueError("capability_resolutions must be a list")
    resolutions: list[CapabilityResolutionRecord] = []
    for index, item in enumerate(resolutions_value):
        c = _mapping(item, f"capability_resolutions[{index}]")
        _require_keys(c, {"entity_id", "observed_unit", "verified_target", "verified_at", "mapping_fingerprint", "policy_fingerprint", "evidence_classification", "documented_unlimited_target"}, f"capability_resolutions[{index}]")
        from .contracts import DocumentedUnlimitedValue
        target = DocumentedUnlimitedValue() if c["verified_target"] == "documented_unlimited" else _decimal(c["verified_target"], "verified_target")
        resolutions.append(CapabilityResolutionRecord(
            _string(c["entity_id"], "entity_id"), _string(c["observed_unit"], "observed_unit"), target,
            _parse_datetime(c["verified_at"], "verified_at"), _fingerprint_text(c["mapping_fingerprint"], "mapping_fingerprint"),
            _fingerprint_text(c["policy_fingerprint"], "policy_fingerprint"), _string(c["evidence_classification"], "evidence_classification"),
            None if c["documented_unlimited_target"] is None else _decimal(c["documented_unlimited_target"], "documented_unlimited_target"),
        ))

    envelope = source["commissioned_power_envelope"]
    envelope_record = None
    if envelope is not None:
        e = _mapping(envelope, "commissioned_power_envelope")
        _require_keys(e, {"maximum_charge_power_kw", "maximum_discharge_power_kw", "maximum_grid_import_power_kw", "schema_version", "inverter_identity", "mapping_fingerprint", "candidate_policy_fingerprint", "manual_grid_fingerprint", "capability_fingerprint", "evidence_source", "validated_at"}, "commissioned_power_envelope")
        envelope_record = CommissionedPowerEnvelope(
            _decimal(e["maximum_charge_power_kw"], "maximum_charge_power_kw"), _decimal(e["maximum_discharge_power_kw"], "maximum_discharge_power_kw"), _decimal(e["maximum_grid_import_power_kw"], "maximum_grid_import_power_kw"),
            _string(e["schema_version"], "schema_version"), _string(e["inverter_identity"], "inverter_identity"), _fingerprint_text(e["mapping_fingerprint"], "mapping_fingerprint"), _fingerprint_text(e["candidate_policy_fingerprint"], "candidate_policy_fingerprint"), _fingerprint_text(e["manual_grid_fingerprint"], "manual_grid_fingerprint"), _fingerprint_text(e["capability_fingerprint"], "capability_fingerprint"), _string(e["evidence_source"], "evidence_source"), _parse_datetime(e["validated_at"], "validated_at"),
        )
    return CandidateCommissioningConfig(enabled, persistent_record, mapping, policy, validated_at, manual_record, tuple(resolutions), envelope_record)


def _require_keys(
    source: Mapping[str, ConfigValue],
    expected: set[str],
    name: str,
    *,
    optional: set[str] | None = None,
) -> None:
    actual = set(source)
    missing = expected - actual
    unknown = actual - expected - (optional or set())
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


def _parse_datetime(value: ConfigValue, name: str) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError(f"{name} must be an ISO-8601 datetime") from None
    else:
        raise ValueError(f"{name} must be an ISO-8601 datetime")
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return result


def _bool(value: ConfigValue, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be bool")
    return value


def _int(value: ConfigValue, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _fingerprint_text(value: ConfigValue, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{name} must be a SHA-256 fingerprint")
    try:
        int(value, 16)
    except ValueError:
        raise ValueError(f"{name} must be a SHA-256 fingerprint") from None
    return value


def _optional_fingerprint(value: ConfigValue, name: str) -> str | None:
    return None if value is None else _fingerprint_text(value, name)


def _decimal(value: ConfigValue, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a decimal number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be a decimal number") from None
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


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
