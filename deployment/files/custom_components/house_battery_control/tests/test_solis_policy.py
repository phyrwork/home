"""Offline tests for persistent Solis policy contracts."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from custom_components.house_battery_control.solis_policy import (
    CapabilityResolutionRecord,
    EphemeralAuthorizationStore,
    ManualGridImportVerification,
    bounded_reserve_soc,
    build_candidate_policy,
    build_fail_safe_policy,
    canonical_policy,
    policy_fingerprint,
)
from custom_components.house_battery_control.domain_constants import MAXIMUM_GRID_IMPORT_POWER_KW
from custom_components.house_battery_control.contracts import StorageMode
from custom_components.house_battery_control.contracts import PreserveCurrentPolicyValue


NOW = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
MAPPING = "1" * 64
POLICY = policy_fingerprint()


@pytest.mark.parametrize(
    ("value", "expected"),
    (("10", Decimal("10")), ("10.01", Decimal("11")), ("99.1", Decimal("100")), ("2", Decimal("10"))),
)
def test_reserve_rounds_up_and_uses_only_named_physical_bounds(value, expected):
    assert bounded_reserve_soc(value) == expected


def test_candidate_and_fail_safe_builders_use_named_settings():
    candidate = build_candidate_policy("10.01")
    safe = build_fail_safe_policy()
    assert candidate.storage_mode is StorageMode.FEED_IN_PRIORITY
    assert candidate.battery_reserve_enabled is True
    assert candidate.battery_reserve_soc == Decimal("11")
    assert safe.storage_mode is StorageMode.SELF_USE
    assert safe.battery_reserve_enabled is False
    assert safe.peak_shaving_enabled is True
    assert isinstance(safe.over_discharge_soc, PreserveCurrentPolicyValue)
    assert isinstance(safe.grid_charge_allowed, PreserveCurrentPolicyValue)


def test_policy_serialization_is_stable_and_explicitly_decimal():
    first = canonical_policy()
    assert first == canonical_policy()
    assert b"MINIMUM_SOC_PERCENT" in first
    assert b'"expected_value_kw":"0.1"' in first
    assert policy_fingerprint() == policy_fingerprint()


def test_ephemeral_authorization_is_single_use_and_fingerprint_bound():
    store = EphemeralAuthorizationStore()
    token = store.issue(now=NOW, mapping=MAPPING, policy=POLICY)
    assert store.consume(token, now=NOW + timedelta(seconds=1), mapping=MAPPING, policy=POLICY)
    assert not store.consume(token, now=NOW + timedelta(seconds=2), mapping=MAPPING, policy=POLICY)

    other = store.issue(now=NOW, mapping=MAPPING, policy=POLICY)
    assert not store.consume(other, now=NOW, mapping="2" * 64, policy=POLICY)
    assert not store.consume(other, now=NOW + timedelta(minutes=11), mapping=MAPPING, policy=POLICY)


def test_ephemeral_expiry_is_bounded_and_future_attempt_is_rejected_and_consumed():
    store = EphemeralAuthorizationStore()
    token = store.issue(now=NOW, mapping=MAPPING, policy=POLICY, ttl=timedelta(minutes=1))
    assert not store.consume(token, now=NOW - timedelta(seconds=1), mapping=MAPPING, policy=POLICY)
    assert not store.consume(token, now=NOW, mapping=MAPPING, policy=POLICY)
    with pytest.raises(ValueError):
        store.issue(now=NOW, mapping=MAPPING, policy=POLICY, ttl=timedelta(minutes=11))


def test_manual_grid_verification_requires_exact_decimal_and_matching_fingerprints():
    record = ManualGridImportVerification(MAXIMUM_GRID_IMPORT_POWER_KW, NOW, MAPPING, POLICY, True)
    assert record.valid(now=NOW, mapping=MAPPING, policy=POLICY)
    assert not record.valid(now=NOW, mapping="2" * 64, policy=POLICY)
    wrong = ManualGridImportVerification(Decimal("0.10"), NOW, MAPPING, POLICY, True)
    assert wrong.valid(now=NOW, mapping=MAPPING, policy=POLICY)


def test_capability_resolution_is_fingerprint_and_unit_bound():
    record = CapabilityResolutionRecord("number.example", "W", Decimal("5000"), NOW, MAPPING, POLICY, "manual_commissioning")
    assert record.valid(now=NOW, mapping=MAPPING, policy=POLICY, unit="W")
    assert not record.valid(now=NOW, mapping=MAPPING, policy=POLICY, unit="A")
    assert not record.valid(now=NOW, mapping=MAPPING, policy=POLICY, unit="W", entity_id="number.other")


def test_documented_unlimited_requires_exact_writable_target():
    from custom_components.house_battery_control.contracts import DocumentedUnlimitedValue

    with pytest.raises(ValueError):
        CapabilityResolutionRecord("number.example", "W", DocumentedUnlimitedValue(), NOW, MAPPING, POLICY, "manual_commissioning")
    record = CapabilityResolutionRecord("number.example", "W", DocumentedUnlimitedValue(), NOW, MAPPING, POLICY, "manual_commissioning", Decimal("5000"))
    assert record.valid(now=NOW, mapping=MAPPING, policy=POLICY, unit="W", entity_id="number.example")
