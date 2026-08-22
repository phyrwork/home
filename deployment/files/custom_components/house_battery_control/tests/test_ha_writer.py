"""Deterministic offline tests for the verified Home Assistant writer."""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from custom_components.house_battery_control.contracts import ObservedCapability
from custom_components.house_battery_control.ha_writer import (
    HA_READBACK_TIMEOUT,
    HomeAssistantWriter,
)
from custom_components.house_battery_control.write_contracts import (
    DatetimeWriteRequest,
    NumberWriteRequest,
    SelectWriteRequest,
    StatePrecondition,
    SwitchWriteRequest,
    TextWriteRequest,
    TransactionStatus,
    WriteOutcome,
    WriteRequest,
)


NOW = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


class FakeHA:
    def __init__(self) -> None:
        self.states: dict[str, dict[str, object]] = {}
        self.listeners: dict[str, list[object]] = {}
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.service_error: Exception | None = None
        self.service_wait: asyncio.Event | None = None
        self.on_call = None

    def get_state(self, entity_id: str) -> object:
        return self.states.get(entity_id)

    def async_listen_state_change(self, entity_id: str, callback):
        self.listeners.setdefault(entity_id, []).append(callback)

        def remove() -> None:
            callbacks = self.listeners.get(entity_id, [])
            if callback in callbacks:
                callbacks.remove(callback)

        return remove

    async def async_call(self, domain: str, service: str, service_data: dict[str, object]) -> None:
        self.calls.append((domain, service, service_data))
        if self.on_call is not None:
            self.on_call(domain, service, service_data)
        if self.service_error is not None:
            raise self.service_error
        if self.service_wait is not None:
            await self.service_wait.wait()

    def set_state(
        self,
        entity_id: str,
        value: str,
        *,
        updated: datetime,
        context_id: str | None = "new",
        attributes: dict[str, object] | None = None,
    ) -> None:
        self.states[entity_id] = {
            "state": value,
            "last_updated": updated,
            "context_id": context_id,
            "attributes": attributes or {},
        }
        for callback in tuple(self.listeners.get(entity_id, ())):
            callback(entity_id, None, self.states[entity_id])


def precondition(ha: FakeHA, entity_id: str) -> StatePrecondition:
    current = ha.states[entity_id]
    return StatePrecondition(
        entity_id,
        current["state"],
        current["last_updated"],
        current["context_id"],
    )


def seed(ha: FakeHA, entity_id: str, value: str, **attributes: object) -> StatePrecondition:
    ha.set_state(entity_id, value, updated=NOW, context_id="old", attributes=attributes)
    return precondition(ha, entity_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entity_id", "value", "target", "domain", "service", "payload"),
    [
        ("switch.a", "off", True, "switch", "turn_on", {"entity_id": "switch.a"}),
        ("select.a", "Self-Use", "Feed-In Priority", "select", "select_option", {"entity_id": "select.a", "option": "Feed-In Priority"}),
        ("text.a", "old", "new", "text", "set_value", {"entity_id": "text.a", "value": "new"}),
        ("datetime.a", NOW.isoformat(), NOW + timedelta(hours=1), "datetime", "set_value", {"entity_id": "datetime.a", "value": (NOW + timedelta(hours=1)).isoformat()}),
    ],
)
async def test_supported_domains_send_exact_payload(
    entity_id, value, target, domain, service, payload
) -> None:
    ha = FakeHA()
    attrs = {"options": ["Self-Use", "Feed-In Priority"]} if domain == "select" else {}
    seed(ha, entity_id, value, **attrs)
    request_cls = {
        "switch": SwitchWriteRequest,
        "select": SelectWriteRequest,
        "text": TextWriteRequest,
        "datetime": DatetimeWriteRequest,
    }[domain]
    request = request_cls(precondition=precondition(ha, entity_id), target=target)

    # Simulate HA applying the service and publishing a newer revision.
    def apply(_domain, _service, data):
        next_value = "on" if service == "turn_on" else "off" if service == "turn_off" else str(data.get("option", data.get("value")))
        ha.set_state(entity_id, next_value, updated=NOW + timedelta(seconds=1), context_id="service", attributes=attrs)

    ha.on_call = apply
    result = await HomeAssistantWriter(ha).async_write(request)
    assert result.outcome is WriteOutcome.APPLIED_HA_READBACK
    assert ha.calls == [(domain, service, payload)]
    assert ha.listeners[entity_id] == []


@pytest.mark.asyncio
async def test_number_capability_metadata_and_exact_step() -> None:
    ha = FakeHA()
    capability = ObservedCapability(Decimal("2"), Decimal("0"), Decimal("10"), Decimal("0.5"), "A")
    seed(ha, "number.a", "2", min="0", max="10", step="0.5", unit_of_measurement="A")
    request = NumberWriteRequest(precondition(ha, "number.a"), Decimal("2.5"), capability=capability)
    ha.on_call = lambda *_: ha.set_state("number.a", "2.5", updated=NOW + timedelta(seconds=1), context_id="service", attributes={"min": "0", "max": "10", "step": "0.5", "unit_of_measurement": "A"})
    result = await HomeAssistantWriter(ha).async_write(request)
    assert result.outcome is WriteOutcome.APPLIED_HA_READBACK
    assert ha.calls[0][2]["value"] == Decimal("2.5")

    fresh_capability = ObservedCapability(Decimal("2.5"), Decimal("0"), Decimal("10"), Decimal("0.5"), "A")
    bad = NumberWriteRequest(precondition(ha, "number.a"), Decimal("2.6"), capability=fresh_capability)
    bad_result = await HomeAssistantWriter(ha).async_write(bad)
    assert bad_result.outcome is WriteOutcome.REJECTED
    assert len(ha.calls) == 1


@pytest.mark.asyncio
async def test_idempotence_and_precondition_conflicts_do_not_call_service() -> None:
    ha = FakeHA()
    seed(ha, "switch.a", "on")
    writer = HomeAssistantWriter(ha)
    same = await writer.async_write(SwitchWriteRequest(precondition(ha, "switch.a"), True))
    assert same.outcome is WriteOutcome.NO_CHANGE
    assert ha.calls == []

    old = precondition(ha, "switch.a")
    ha.set_state("switch.a", "off", updated=NOW + timedelta(seconds=1), context_id="other")
    conflict = await writer.async_write(SwitchWriteRequest(old, True))
    assert conflict.outcome is WriteOutcome.CONFLICT
    assert ha.calls == []


@pytest.mark.asyncio
async def test_matching_value_without_revision_times_out_without_retry(monkeypatch) -> None:
    ha = FakeHA()
    seed(ha, "switch.a", "off")
    request = SwitchWriteRequest(precondition(ha, "switch.a"), True)
    # Service leaves the value untouched: a matching value without a revision is not proof.
    monkeypatch.setattr("custom_components.house_battery_control.ha_writer.HA_READBACK_TIMEOUT", timedelta(seconds=0.01))
    result = await HomeAssistantWriter(ha).async_write(request)
    assert result.outcome is WriteOutcome.READBACK_TIMEOUT
    assert len(ha.calls) == 1
    assert ha.listeners["switch.a"] == []


@pytest.mark.asyncio
async def test_late_state_event_is_accepted_and_missing_states_are_rejected() -> None:
    ha = FakeHA()
    seed(ha, "switch.a", "off")
    request = SwitchWriteRequest(precondition(ha, "switch.a"), True)

    def apply(_domain, _service, _data):
        asyncio.get_running_loop().call_later(
            0.01,
            lambda: ha.set_state(
                "switch.a",
                "on",
                updated=NOW + timedelta(seconds=1),
                context_id="event",
            ),
        )

    ha.on_call = apply
    result = await HomeAssistantWriter(ha).async_write(request)
    assert result.outcome is WriteOutcome.APPLIED_HA_READBACK

    for value in (None, "unknown", "unavailable"):
        if value is None:
            ha.states.pop("switch.a")
        else:
            ha.set_state("switch.a", value, updated=NOW, context_id="old")
        rejected = await HomeAssistantWriter(ha).async_write(
            SwitchWriteRequest(
                StatePrecondition("switch.a", value or "off", NOW, "old"),
                True,
            )
        )
        assert rejected.outcome is WriteOutcome.REJECTED


@pytest.mark.asyncio
async def test_service_error_and_service_timeout_are_distinct(monkeypatch) -> None:
    ha = FakeHA()
    seed(ha, "switch.a", "off")
    ha.service_error = RuntimeError("rejected by fake service")
    error = await HomeAssistantWriter(ha).async_write(
        SwitchWriteRequest(precondition(ha, "switch.a"), True)
    )
    assert error.outcome is WriteOutcome.SERVICE_ERROR

    ha = FakeHA()
    seed(ha, "switch.a", "off")
    ha.service_wait = asyncio.Event()
    monkeypatch.setattr("custom_components.house_battery_control.ha_writer.HA_SERVICE_CALL_TIMEOUT", timedelta(seconds=0.01))
    timeout = await HomeAssistantWriter(ha).async_write(
        SwitchWriteRequest(precondition(ha, "switch.a"), True)
    )
    assert timeout.outcome is WriteOutcome.SERVICE_TIMEOUT
    assert ha.listeners["switch.a"] == []


@pytest.mark.asyncio
async def test_number_metadata_drift_is_conflict_without_write() -> None:
    ha = FakeHA()
    observed = ObservedCapability(Decimal("2"), Decimal("0"), Decimal("10"), Decimal("1"), "A")
    seed(ha, "number.a", "2", min="0", max="10", step="0.5", unit_of_measurement="A")
    result = await HomeAssistantWriter(ha).async_write(
        NumberWriteRequest(precondition(ha, "number.a"), Decimal("3"), capability=observed)
    )
    assert result.outcome is WriteOutcome.CONFLICT
    assert ha.calls == []


@pytest.mark.asyncio
async def test_text_validator_and_domain_rejection() -> None:
    ha = FakeHA()
    seed(ha, "text.a", "old")
    rejected = await HomeAssistantWriter(ha).async_write(
        TextWriteRequest(precondition(ha, "text.a"), "bad", text_validator=lambda value: value == "good")
    )
    assert rejected.outcome is WriteOutcome.REJECTED
    assert ha.calls == []
    wrong = await HomeAssistantWriter(ha).async_write(
        WriteRequest(precondition(ha, "text.a"), "new", domain="switch")
    )
    assert wrong.outcome is WriteOutcome.REJECTED


@pytest.mark.asyncio
async def test_transaction_is_ordered_non_atomic_and_reports_partial_failure() -> None:
    ha = FakeHA()
    seed(ha, "switch.a", "off")
    seed(ha, "switch.b", "off")
    writer = HomeAssistantWriter(ha)
    ha.on_call = lambda domain, service, data: ha.set_state(data["entity_id"], "on", updated=NOW + timedelta(seconds=1), context_id="service")
    async with writer.transaction() as transaction:
        result = await transaction.execute(
            [
                SwitchWriteRequest(precondition(ha, "switch.a"), True),
                SwitchWriteRequest(StatePrecondition("switch.b", "on", NOW, "old"), True),
            ],
            continue_on_failure=True,
        )
    assert result.status is TransactionStatus.PARTIAL_FAILURE
    assert [item.entity_id for item in result.results] == ["switch.a", "switch.b"]
    assert result.complete


@pytest.mark.asyncio
async def test_cancellation_removes_listener_releases_lock_and_propagates(monkeypatch) -> None:
    ha = FakeHA()
    seed(ha, "switch.a", "off")
    gate = asyncio.Event()
    ha.service_wait = gate
    writer = HomeAssistantWriter(ha)
    monkeypatch.setattr("custom_components.house_battery_control.ha_writer.HA_SERVICE_CALL_TIMEOUT", timedelta(seconds=5))
    task = asyncio.create_task(writer.async_write(SwitchWriteRequest(precondition(ha, "switch.a"), True)))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ha.listeners["switch.a"] == []
    assert not writer._lock.locked()
