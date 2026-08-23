"""Deterministic offline tests for the verified Home Assistant writer."""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from types import MappingProxyType, SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.house_battery_control.contracts import ObservedCapability
from custom_components.house_battery_control.ha_writer import (
    HA_READBACK_TIMEOUT,
    HomeAssistantEventAdapter,
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
        self.remove_gate: asyncio.Event | None = None

    def get_state(self, entity_id: str) -> object:
        return self.states.get(entity_id)

    def async_listen_state_change(self, entity_id: str, callback):
        self.listeners.setdefault(entity_id, []).append(callback)

        def remove_sync() -> None:
            callbacks = self.listeners.get(entity_id, [])
            if callback in callbacks:
                callbacks.remove(callback)

        if self.remove_gate is None:
            return remove_sync

        async def remove_async() -> None:
            await self.remove_gate.wait()
            remove_sync()

        return remove_async

    async def async_call(self, domain: str, service: str, service_data: dict[str, object], *, blocking: bool = True) -> None:
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
        ("datetime.a", NOW.isoformat(), NOW + timedelta(hours=1), "datetime", "set_value", {"entity_id": "datetime.a", "datetime": (NOW + timedelta(hours=1)).isoformat()}),
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
        next_value = "on" if service == "turn_on" else "off" if service == "turn_off" else str(data.get("option", data.get("value", data.get("datetime"))))
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
    assert ha.calls[0][2]["value"] == 2.5
    # The payload is sent through HA and must not contain Decimal, which the
    # recorder cannot JSON encode.
    json.dumps(ha.calls[0][2])
    assert result.service_data is not None
    assert result.service_data["value"] == 2.5

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
    # Service changes the raw value but leaves revision and context untouched:
    # a matching value without a new revision is not proof.
    ha.on_call = lambda _d, _s, _data: ha.set_state(
        "switch.a", "on", updated=NOW, context_id="old"
    )
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
    monkeypatch.setattr("custom_components.house_battery_control.ha_writer.HA_SERVICE_TIMEOUT_CONFIRMATION", timedelta(seconds=0.01))
    timeout = await HomeAssistantWriter(ha).async_write(
        SwitchWriteRequest(precondition(ha, "switch.a"), True)
    )
    assert timeout.outcome is WriteOutcome.SERVICE_TIMEOUT
    assert ha.listeners["switch.a"] == []


@pytest.mark.asyncio
async def test_service_timeout_accepts_late_new_ha_revision_without_replay(
    monkeypatch,
) -> None:
    ha = FakeHA()
    seed(ha, "switch.a", "off")
    ha.service_wait = asyncio.Event()

    def publish_late(_domain, _service, _data):
        asyncio.get_running_loop().call_later(
            0.02,
            lambda: ha.set_state(
                "switch.a",
                "on",
                updated=NOW + timedelta(seconds=1),
                context_id="late-ha",
            ),
        )

    ha.on_call = publish_late
    monkeypatch.setattr(
        "custom_components.house_battery_control.ha_writer.HA_SERVICE_CALL_TIMEOUT",
        timedelta(seconds=0.01),
    )
    monkeypatch.setattr(
        "custom_components.house_battery_control.ha_writer.HA_SERVICE_TIMEOUT_CONFIRMATION",
        timedelta(seconds=0.1),
    )

    result = await HomeAssistantWriter(ha).async_write(
        SwitchWriteRequest(precondition(ha, "switch.a"), True)
    )

    assert result.outcome is WriteOutcome.APPLIED_HA_READBACK
    assert "device state remains unproven" in result.message
    assert len(ha.calls) == 1
    assert ha.listeners["switch.a"] == []


@pytest.mark.asyncio
async def test_service_timeout_checks_current_revision_before_waiting(
    monkeypatch,
) -> None:
    ha = FakeHA()
    seed(ha, "switch.a", "off")
    ha.service_wait = asyncio.Event()
    ha.on_call = lambda *_: ha.set_state(
        "switch.a",
        "on",
        updated=NOW + timedelta(seconds=1),
        context_id="new-ha",
    )
    monkeypatch.setattr(
        "custom_components.house_battery_control.ha_writer.HA_SERVICE_CALL_TIMEOUT",
        timedelta(seconds=0.01),
    )

    result = await HomeAssistantWriter(ha).async_write(
        SwitchWriteRequest(precondition(ha, "switch.a"), True)
    )

    assert result.outcome is WriteOutcome.APPLIED_HA_READBACK
    assert len(ha.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "updated", "context_id"),
    (
        ("on", NOW, "old"),
        ("off", NOW + timedelta(seconds=1), "other"),
    ),
)
async def test_service_timeout_rejects_unproven_or_mismatched_revision(
    monkeypatch, value, updated, context_id
) -> None:
    ha = FakeHA()
    seed(ha, "switch.a", "off")
    ha.service_wait = asyncio.Event()
    ha.on_call = lambda *_: ha.set_state(
        "switch.a", value, updated=updated, context_id=context_id
    )
    monkeypatch.setattr(
        "custom_components.house_battery_control.ha_writer.HA_SERVICE_CALL_TIMEOUT",
        timedelta(seconds=0.01),
    )
    monkeypatch.setattr(
        "custom_components.house_battery_control.ha_writer.HA_SERVICE_TIMEOUT_CONFIRMATION",
        timedelta(seconds=0.01),
    )

    result = await HomeAssistantWriter(ha).async_write(
        SwitchWriteRequest(precondition(ha, "switch.a"), True)
    )

    assert result.outcome is WriteOutcome.SERVICE_TIMEOUT
    assert len(ha.calls) == 1
    assert ha.listeners["switch.a"] == []


@pytest.mark.asyncio
async def test_absolute_deadline_bounds_service_and_confirmation_waits() -> None:
    ha = FakeHA()
    seed(ha, "switch.a", "off")
    ha.service_wait = asyncio.Event()
    writer = HomeAssistantWriter(ha)
    loop = asyncio.get_running_loop()

    result = await writer.async_write(
        SwitchWriteRequest(precondition(ha, "switch.a"), True),
        deadline=loop.time() + 0.02,
    )

    assert result.outcome is WriteOutcome.SERVICE_TIMEOUT
    assert len(ha.calls) == 1
    assert ha.listeners["switch.a"] == []


@pytest.mark.asyncio
async def test_absolute_deadline_bounds_writer_lock_acquisition() -> None:
    ha = FakeHA()
    seed(ha, "switch.a", "off")
    writer = HomeAssistantWriter(ha)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_lock() -> None:
        async with writer.transaction():
            entered.set()
            await release.wait()

    holder = asyncio.create_task(hold_lock())
    await entered.wait()
    try:
        with pytest.raises(TimeoutError, match="deadline"):
            await writer.async_write(
                SwitchWriteRequest(precondition(ha, "switch.a"), True),
                deadline=asyncio.get_running_loop().time() + 0.01,
            )
    finally:
        release.set()
        await holder

    assert ha.calls == []
    assert not writer._lock.locked()


@pytest.mark.asyncio
async def test_synchronous_service_implementation_is_rejected() -> None:
    ha = FakeHA()
    seed(ha, "switch.a", "off")
    ha.async_call = lambda *_args, **_kwargs: None
    result = await HomeAssistantWriter(ha).async_write(
        SwitchWriteRequest(precondition(ha, "switch.a"), True)
    )
    assert result.outcome is WriteOutcome.SERVICE_ERROR
    assert len(ha.calls) == 0


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


@pytest.mark.asyncio
async def test_cancellation_shields_bounded_async_listener_cleanup() -> None:
    ha = FakeHA()
    seed(ha, "switch.a", "off")
    ha.remove_gate = asyncio.Event()
    ha.on_call = lambda _d, _s, _data: ha.set_state(
        "switch.a", "on", updated=NOW + timedelta(seconds=1), context_id="service"
    )
    writer = HomeAssistantWriter(ha)
    task = asyncio.create_task(writer.async_write(SwitchWriteRequest(precondition(ha, "switch.a"), True)))
    for _ in range(5):
        await asyncio.sleep(0)
        if ha.calls:
            break
    assert ha.calls
    task.cancel()
    await asyncio.sleep(0)
    ha.remove_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ha.listeners["switch.a"] == []
    assert not writer._lock.locked()


@pytest.mark.asyncio
async def test_transaction_owner_nested_and_cross_task_access_are_rejected() -> None:
    ha = FakeHA()
    seed(ha, "switch.a", "off")
    writer = HomeAssistantWriter(ha)
    async with writer.transaction() as transaction:
        with pytest.raises(RuntimeError, match="re-entered"):
            await writer.async_write(SwitchWriteRequest(precondition(ha, "switch.a"), True))
        with pytest.raises(RuntimeError, match="nested"):
            async with writer.transaction():
                pass
        async def cross_task_call() -> None:
            await transaction.async_write(SwitchWriteRequest(precondition(ha, "switch.a"), True))

        cross_task = asyncio.create_task(cross_task_call())
        with pytest.raises(RuntimeError, match="owning"):
            await cross_task


@pytest.mark.asyncio
async def test_transaction_closes_after_exit_and_rejects_reentry() -> None:
    ha = FakeHA()
    seed(ha, "switch.a", "off")
    writer = HomeAssistantWriter(ha)
    transaction = writer.transaction()
    async with transaction:
        snapshot = transaction.result()
        assert snapshot.complete
    assert transaction.result() == snapshot
    with pytest.raises(RuntimeError, match="owning"):
        await transaction.async_write(SwitchWriteRequest(precondition(ha, "switch.a"), True))
    with pytest.raises(RuntimeError, match="closed"):
        async with transaction:
            pass


@pytest.mark.asyncio
async def test_external_mutation_after_listener_arm_is_conflict_without_call() -> None:
    ha = FakeHA()
    seed(ha, "switch.a", "off")
    old_listener = ha.async_listen_state_change

    def listen(entity_id, callback):
        remove = old_listener(entity_id, callback)
        ha.set_state(entity_id, "on", updated=NOW + timedelta(seconds=1), context_id="external")
        return remove

    ha.async_listen_state_change = listen
    result = await HomeAssistantWriter(ha).async_write(
        SwitchWriteRequest(StatePrecondition("switch.a", "off", NOW, "old"), True)
    )
    assert result.outcome is WriteOutcome.CONFLICT
    assert ha.calls == []


@pytest.mark.asyncio
async def test_success_and_wholly_failed_transaction_statuses() -> None:
    ha = FakeHA()
    seed(ha, "switch.a", "off")
    seed(ha, "switch.b", "off")
    writer = HomeAssistantWriter(ha)
    ha.on_call = lambda _d, _s, data: ha.set_state(data["entity_id"], "on", updated=NOW + timedelta(seconds=1), context_id="service")
    async with writer.transaction() as transaction:
        success = await transaction.execute([SwitchWriteRequest(precondition(ha, "switch.a"), True)])
    assert success.status is TransactionStatus.SUCCESS

    async with writer.transaction() as transaction:
        failed = await transaction.execute([SwitchWriteRequest(StatePrecondition("switch.b", "on", NOW, "old"), True)])
    assert failed.status is TransactionStatus.FAILED
    assert not failed.results[0].success


@pytest.mark.asyncio
async def test_transaction_cancellation_preserves_outcomes_and_marks_incomplete(monkeypatch) -> None:
    ha = FakeHA()
    seed(ha, "switch.a", "on")
    seed(ha, "switch.b", "off")
    ha.service_wait = asyncio.Event()
    writer = HomeAssistantWriter(ha)
    transaction = writer.transaction()

    async def run() -> None:
        async with transaction:
            await transaction.async_write(SwitchWriteRequest(precondition(ha, "switch.a"), True))
            await transaction.async_write(SwitchWriteRequest(precondition(ha, "switch.b"), True))

    task = asyncio.create_task(run())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert transaction.complete is False
    assert len(transaction.results) == 1
    assert transaction.complete is False
    assert transaction.results[0].outcome is WriteOutcome.NO_CHANGE
    assert transaction.result().complete is False
    assert not writer._lock.locked()


@pytest.mark.asyncio
async def test_shared_writer_serializes_concurrent_writes() -> None:
    ha = FakeHA()
    seed(ha, "switch.a", "off")
    seed(ha, "switch.b", "off")
    writer = HomeAssistantWriter(ha)
    active = 0
    maximum = 0
    gate = asyncio.Event()

    async def service(domain, service, data, *, blocking=True):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await gate.wait()
        entity = data["entity_id"]
        ha.set_state(entity, "on", updated=NOW + timedelta(seconds=1), context_id="service")
        active -= 1

    ha.async_call = service
    first = asyncio.create_task(writer.async_write(SwitchWriteRequest(precondition(ha, "switch.a"), True)))
    await asyncio.sleep(0)
    second = asyncio.create_task(writer.async_write(SwitchWriteRequest(precondition(ha, "switch.b"), True)))
    await asyncio.sleep(0)
    assert maximum == 1
    gate.set()
    assert (await first).outcome is WriteOutcome.APPLIED_HA_READBACK
    assert (await second).outcome is WriteOutcome.APPLIED_HA_READBACK
    assert maximum == 1


@pytest.mark.asyncio
async def test_datetime_requires_pre_normalized_configured_timezone_and_payload_key() -> None:
    london = ZoneInfo("Europe/London")
    ha = FakeHA()
    target = datetime(2026, 1, 1, 12, tzinfo=london)
    seed(ha, "datetime.a", datetime(2026, 1, 1, 11, tzinfo=london).isoformat())
    ha.on_call = lambda _d, _s, data: ha.set_state("datetime.a", data["datetime"], updated=NOW + timedelta(seconds=1), context_id="service")
    result = await HomeAssistantWriter(ha, timezone=london).async_write(
        DatetimeWriteRequest(precondition(ha, "datetime.a"), target)
    )
    assert result.outcome is WriteOutcome.APPLIED_HA_READBACK
    assert ha.calls[0][2] == {"entity_id": "datetime.a", "datetime": target.isoformat()}
    wrong_zone = await HomeAssistantWriter(ha, timezone=london).async_write(
        DatetimeWriteRequest(
            StatePrecondition("datetime.a", target.isoformat(), NOW + timedelta(seconds=1), "service"),
            target.astimezone(timezone.utc),
        )
    )
    assert wrong_zone.outcome is WriteOutcome.REJECTED


@pytest.mark.asyncio
async def test_result_service_data_is_immutable() -> None:
    ha = FakeHA()
    seed(ha, "switch.a", "off")
    result = await HomeAssistantWriter(ha).async_write(SwitchWriteRequest(precondition(ha, "switch.a"), False))
    assert isinstance(result.service_data, MappingProxyType)
    with pytest.raises(TypeError):
        result.service_data["entity_id"] = "switch.other"  # type: ignore[index]


def test_real_home_assistant_event_adapter_delegates_state_listener(monkeypatch) -> None:
    ha = FakeHA()
    captured = {}

    def tracker(hass, entities, callback):
        captured.update(hass=hass, entities=entities, callback=callback)
        return lambda: None

    monkeypatch.setattr(
        "homeassistant.helpers.event.async_track_state_change_event", tracker
    )
    adapter = HomeAssistantEventAdapter(ha)
    callback = lambda *_: None
    remove = adapter.async_listen_state_change("switch.a", callback)
    assert captured["hass"] is ha
    assert captured["entities"] == ["switch.a"]
    assert captured["callback"] is callback
    remove()


@pytest.mark.asyncio
async def test_production_factory_wires_real_delayed_readback_adapter(monkeypatch) -> None:
    ha = FakeHA()
    seed(ha, "switch.a", "off")

    class StateStore:
        def get(self, entity_id):
            return ha.get_state(entity_id)

    class ServiceStore:
        async def async_call(self, domain, service, data, *, blocking=True):
            await ha.async_call(domain, service, data, blocking=blocking)

    real_hass = SimpleNamespace(states=StateStore(), services=ServiceStore())
    monkeypatch.setattr(
        "homeassistant.helpers.event.async_track_state_change_event",
        lambda _hass, entities, callback: ha.async_listen_state_change(entities[0], callback),
    )
    ha.on_call = lambda _d, _s, _data: ha.set_state(
        "switch.a", "on", updated=NOW + timedelta(seconds=1), context_id="factory"
    )
    writer = HomeAssistantWriter.for_home_assistant(real_hass, timezone=timezone.utc)
    result = await writer.async_write(SwitchWriteRequest(precondition(ha, "switch.a"), True))
    assert result.outcome is WriteOutcome.APPLIED_HA_READBACK
    with pytest.raises(ValueError, match="for_home_assistant"):
        HomeAssistantWriter(real_hass)
