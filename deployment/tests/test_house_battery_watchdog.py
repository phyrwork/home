"""Offline contract tests for the independent house-battery watchdog."""

import asyncio
from copy import deepcopy
from pathlib import Path
from time import monotonic, time
from typing import Any

import yaml
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.script import Script
from homeassistant.helpers.template import Template

from custom_components.house_battery_control.ha_writer import (
    HA_READBACK_TIMEOUT,
    HA_SERVICE_CALL_TIMEOUT,
)


ROOT = Path(__file__).resolve().parents[1]
FILES = ROOT / "files"
CONFIG_PATH = FILES / "house_battery_control.yaml"
SCRIPT_PATH = FILES / "scripts" / "house_battery.yaml"
AUTOMATION_PATH = FILES / "automations" / "house_battery.yaml"

GUARD = "input_boolean.house_battery_control_disable"
MODE = "select.garage_inverter_control_storage_mode"
PEAK = "switch.garage_inverter_control_grid_peak_shaving"
RESERVE = "switch.garage_inverter_control_battery_reserve"
HEARTBEAT = "sensor.house_battery_control_heartbeat"
HEALTH = "sensor.house_battery_control_health"


def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text())


def _configured_slots(config: dict[str, Any]) -> list[str]:
    return [
        direction["enable_entity_id"]
        for slot in config["solis"]["slots"]
        for direction in (slot["charge"], slot["discharge"])
    ]


def _actions(value: Any) -> list[dict[str, Any]]:
    """Flatten nested HA action lists without interpreting templates."""
    found: list[dict[str, Any]] = []
    if isinstance(value, list):
        for item in value:
            found.extend(_actions(item))
    elif isinstance(value, dict):
        if "service" in value or "action" in value or "wait_template" in value:
            found.append(value)
        for key in ("sequence", "then", "else", "default"):
            if key in value:
                found.extend(_actions(value[key]))
        if "repeat" in value:
            found.extend(_actions(value["repeat"].get("sequence", [])))
        for choice in value.get("choose", []):
            found.extend(_actions(choice.get("sequence", [])))
    return found


def _script() -> dict[str, Any]:
    return _load(SCRIPT_PATH)["house_battery_fail_safe"]


def _scaled_script(
    *,
    owner_seconds: float = 0.6,
    settle_seconds: float = 0.25,
    finalization_seconds: float = 0.2,
    cancellation_seconds: float = 0.05,
) -> dict[str, Any]:
    definition = deepcopy(_script())
    timing = definition["sequence"][0]["variables"]
    timing["OWNER_DEADLINE_SECONDS"] = owner_seconds
    timing["OUTSTANDING_WRITE_SETTLE_SECONDS"] = settle_seconds
    timing["FINALIZATION_BUDGET_SECONDS"] = finalization_seconds
    timing["CANCELLATION_SETTLE_SECONDS"] = cancellation_seconds
    return definition


def _pass_script() -> dict[str, Any]:
    return _load(SCRIPT_PATH)["house_battery_fail_safe_pass"]


def _automation() -> dict[str, Any]:
    return _load(AUTOMATION_PATH)[0]


def _shutdown_automation() -> dict[str, Any]:
    return _load(AUTOMATION_PATH)[1]


def _cancel_script() -> dict[str, Any]:
    return _load(SCRIPT_PATH)["house_battery_fail_safe_cancel_pass"]


def _service_entity_id(call: ServiceCall) -> str | None:
    value = call.data.get("entity_id")
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)) and len(value) == 1:
        return value[0]
    return None


def test_script_mapping_is_exactly_the_deployed_t0003_map() -> None:
    config = _load(CONFIG_PATH)
    script = _script()
    variables = script["sequence"][0]["variables"]

    assert variables["guard_entity_id"] == config["control_disable_guard_entity_id"] == GUARD
    assert variables["storage_mode_entity_id"] == config["solis"]["persistent"]["storage_mode_entity_id"] == MODE
    assert variables["peak_shaving_entity_id"] == config["solis"]["persistent"]["grid_peak_shaving_entity_id"] == PEAK
    assert variables["battery_reserve_entity_id"] == config["solis"]["protection"]["battery_reserve_entity_id"] == RESERVE
    assert variables["slot_entity_ids"] == _configured_slots(config)

    action_text = yaml.safe_dump(_load(SCRIPT_PATH))
    for entity_id in [GUARD, MODE, PEAK, RESERVE, *_configured_slots(config)]:
        assert entity_id in action_text


def test_guard_is_first_mutation_and_never_cleared() -> None:
    script = _script()
    sequence = script["sequence"]
    first_mutation = next(action for action in sequence if "service" in action)
    assert sequence[0].keys() == {"variables"}
    assert sequence[1].keys() == {"variables"}
    assert "initial_guard_state" in sequence[0]["variables"]
    assert "initial_guard_last_changed_timestamp" in sequence[0]["variables"]
    assert "owner_started_at" in sequence[0]["variables"]
    assert "outstanding_write_settle_until" not in sequence[1]["variables"]
    assert first_mutation["service"] == "input_boolean.turn_on"
    assert first_mutation["target"]["entity_id"] == GUARD
    assert first_mutation["continue_on_error"] is True
    assert "input_boolean.turn_off" not in yaml.safe_dump(_load(SCRIPT_PATH))
    assert "input_boolean.turn_off" not in yaml.safe_dump(sequence[1:])


def test_all_solis_mutations_are_independent_and_safe_actions_only() -> None:
    actions = _actions(_pass_script()["sequence"])
    services = [action.get("service") for action in actions]
    assert "select.select_option" in services
    assert "switch.turn_on" in services
    assert "switch.turn_off" in services
    assert not any(service and service.startswith("house_battery_control.") for service in services)
    assert "select.select_option" in services and "switch.turn_on" in services
    for action in actions:
        if action.get("service") in {
            "input_boolean.turn_on",
            "switch.turn_off",
            "switch.turn_on",
            "select.select_option",
        }:
            assert action["continue_on_error"] is True
    slot_actions = [
        action
        for action in actions
        if action.get("service") == "switch.turn_off"
        and isinstance(action.get("target", {}).get("entity_id"), str)
        and "slot" in action["target"]["entity_id"]
    ]
    assert [action["target"]["entity_id"] for action in slot_actions] == _configured_slots(_load(CONFIG_PATH))

    sequence = _pass_script()["sequence"]
    mutations = [index for index, action in enumerate(sequence) if "service" in action]
    assert len(mutations) == 15
    for index in mutations:
        proof = sequence[index - 1]
        assert proof["condition"] == "template"
        assert "states('input_boolean.house_battery_control_disable') == 'on'" in proof["value_template"]
        assert "deadline_timestamp" in proof["value_template"]


def test_script_has_initial_and_final_exact_proof_with_two_pass_bound() -> None:
    script = _script()
    text = yaml.safe_dump(script)
    assert text.count("Self-Use") >= 3
    assert text.count("states(peak_shaving_entity_id)") >= 3
    assert text.count("states(battery_reserve_entity_id)") >= 3
    helper_starts = [
        action for action in _actions(script["sequence"])
        if action.get("service") == "script.turn_on"
        and action.get("target", {}).get("entity_id") == "{{ helper_entity_id }}"
    ]
    assert len(helper_starts) == 2
    cancel_starts = [
        action for action in _actions(script["sequence"])
        if action.get("service") == "script.turn_on"
        and action.get("target", {}).get("entity_id") == "{{ cancel_helper_entity_id }}"
    ]
    assert len(cancel_starts) == 3
    assert "OWNER_DEADLINE_SECONDS" in text
    assert "OUTSTANDING_WRITE_SETTLE_SECONDS" in text
    assert "FINALIZATION_BUDGET_SECONDS" in text
    assert "CANCELLATION_SETTLE_SECONDS" in text
    assert text.count("deadline_timestamp") >= 6
    assert "script.turn_off" not in text
    assert _script()["mode"] == "single"
    assert _pass_script()["mode"] == "single"
    assert _cancel_script()["mode"] == "single"
    assert _cancel_script()["sequence"][0]["service"] == "script.turn_off"
    assert "pass_one_settled_proof" in text
    assert "settlement_elapsed" in text
    assert "second_pass_completed_before_cutoff" in text
    assert "RECONCILIATION_PENDING" in text
    assert "final_proof" in text
    assert ".append(" not in text
    assert "namespace(failures=[])" in text


def test_settlement_constant_and_cutoff_finalization_order() -> None:
    script = _script()
    timing = script["sequence"][0]["variables"]
    assert timing["OUTSTANDING_WRITE_SETTLE_SECONDS"] == int(
        (HA_SERVICE_CALL_TIMEOUT + HA_READBACK_TIMEOUT).total_seconds()
    ) == 25
    assert timing["OWNER_DEADLINE_SECONDS"] == 30
    assert timing["FINALIZATION_BUDGET_SECONDS"] == 3
    assert timing["CANCELLATION_SETTLE_SECONDS"] == 1
    assert timing["OUTSTANDING_WRITE_SETTLE_SECONDS"] < (
        timing["OWNER_DEADLINE_SECONDS"] - timing["FINALIZATION_BUDGET_SECONDS"]
    )

    sequence = script["sequence"]
    cutoff_delay = next(
        index
        for index, action in enumerate(sequence)
        if "delay" in action
        and "mutation_cutoff_timestamp" in yaml.safe_dump(action)
        and index > len(sequence) // 2
    )
    final_variables = next(
        index
        for index, action in enumerate(sequence)
        if "variables" in action and "final_proof" in action["variables"]
    )
    cutoff_cancel = next(
        index
        for index in range(cutoff_delay + 1, final_variables)
        if sequence[index].get("service") == "script.turn_on"
        and sequence[index].get("target", {}).get("entity_id")
        == "{{ cancel_helper_entity_id }}"
    )
    settlement_wait = next(
        index
        for index in range(cutoff_cancel + 1, final_variables)
        if "wait_template" in sequence[index]
    )
    assert cutoff_delay < cutoff_cancel < settlement_wait < final_variables


def test_automation_has_required_lifecycle_periodic_and_evidence_triggers() -> None:
    automation = _automation()
    triggers = {trigger["id"]: trigger for trigger in automation["trigger"]}
    assert set(triggers) == {
        "home_assistant_start",
        "every_minute",
        "heartbeat_changed",
        "controller_health_changed",
        "control_disable_changed",
    }
    assert triggers["every_minute"]["platform"] == "time_pattern"
    assert triggers["every_minute"]["minutes"] == "/1"
    assert triggers["heartbeat_changed"]["entity_id"] == HEARTBEAT
    assert triggers["controller_health_changed"]["entity_id"] == HEALTH
    assert triggers["control_disable_changed"]["entity_id"] == GUARD
    assert automation["mode"] == "single"
    assert automation["action"][0]["service"] == "script.turn_on"
    assert automation["action"][0]["target"]["entity_id"] == "script.house_battery_fail_safe"

    shutdown = _shutdown_automation()
    assert shutdown["trigger"] == [
        {
            "platform": "homeassistant",
            "event": "shutdown",
            "id": "home_assistant_shutdown",
        }
    ]
    assert shutdown["mode"] == "single"
    assert shutdown["action"][0]["service"] == "input_boolean.turn_on"
    assert shutdown["action"][0]["target"]["entity_id"] == GUARD
    assert shutdown["action"][1]["service"] == "script.turn_on"
    assert shutdown["action"][1]["target"]["entity_id"] == "script.house_battery_fail_safe"
    assert {
        action["service"].split(".", 1)[0] for action in shutdown["action"]
    } == {"input_boolean", "script"}


def test_automation_is_fail_closed_and_lifecycle_is_unconditional() -> None:
    text = yaml.safe_dump(_load(AUTOMATION_PATH))
    assert "home_assistant_start" in text
    assert "home_assistant_shutdown" in text
    assert "!= 'off'" in text
    assert "!= 'healthy'" in text
    assert "heartbeat is none" in text
    assert "> 180" in text
    assert "> 60" in text
    assert "unknown" not in text or "as_timestamp" in text


def _set_control_states(hass: HomeAssistant, *, safe: bool, guard: str = "on") -> None:
    hass.states.async_set(GUARD, guard)
    for entity_id in _configured_slots(_load(CONFIG_PATH)):
        hass.states.async_set(entity_id, "off" if safe else "on")
    hass.states.async_set(MODE, "Self-Use" if safe else "Feed-In Priority")
    hass.states.async_set(PEAK, "on" if safe else "off")
    hass.states.async_set(RESERVE, "off" if safe else "on")
    hass.states.async_set("script.house_battery_fail_safe_pass", "off")
    hass.states.async_set("script.house_battery_fail_safe_cancel_pass", "off")


def _script_runner(hass: HomeAssistant, definition: dict[str, Any], name: str) -> Script:
    return Script(
        hass,
        cv.SCRIPT_SCHEMA(definition["sequence"]),
        name,
        "script",
        script_mode=definition["mode"],
        max_exceeded=definition.get("max_exceeded", "WARNING"),
    )


def _register_control_services(
    hass: HomeAssistant,
    calls: list[tuple[str, str, dict[str, Any]]],
) -> None:
    async def handle(call: ServiceCall) -> None:
        data = dict(call.data)
        calls.append((call.domain, call.service, data))
        raw_entity_ids = data.get("entity_id")
        if isinstance(raw_entity_ids, str):
            entity_ids = (raw_entity_ids,)
        elif isinstance(raw_entity_ids, (list, tuple)):
            entity_ids = tuple(raw_entity_ids)
        else:
            entity_ids = ()
        if not entity_ids and call.domain != "persistent_notification":
            return
        if call.domain == "input_boolean" and call.service == "turn_on":
            for entity_id in entity_ids:
                hass.states.async_set(entity_id, "on")
        elif call.domain == "switch":
            for entity_id in entity_ids:
                hass.states.async_set(entity_id, "on" if call.service == "turn_on" else "off")
        elif call.domain == "select" and call.service == "select_option":
            for entity_id in entity_ids:
                hass.states.async_set(entity_id, data["option"])
        elif call.domain == "persistent_notification" and call.service == "create":
            hass.states.async_set(
                f"persistent_notification.{data['notification_id']}",
                "notifying",
                {"message": data["message"]},
            )
        elif call.domain == "persistent_notification" and call.service == "dismiss":
            hass.states.async_remove(f"persistent_notification.{data['notification_id']}")

    for domain, service in (
        ("input_boolean", "turn_on"),
        ("switch", "turn_on"),
        ("switch", "turn_off"),
        ("select", "select_option"),
        ("persistent_notification", "create"),
        ("persistent_notification", "dismiss"),
    ):
        hass.services.async_register(domain, service, handle)


async def test_ha_template_renders_failure_reason_in_immutable_sandbox(
    hass: HomeAssistant,
) -> None:
    _set_control_states(hass, safe=True, guard="unavailable")
    first_slot = _configured_slots(_load(CONFIG_PATH))[0]
    hass.states.async_set(first_slot, "on")
    final_variables = next(
        action["variables"]
        for action in _script()["sequence"]
        if isinstance(action, dict)
        and "variables" in action
        and "final_proof" in action["variables"]
    )
    rendered = Template(final_variables["failure_reason"], hass).async_render(
        {
            "guard_entity_id": GUARD,
            "slot_entity_ids": _configured_slots(_load(CONFIG_PATH)),
            "storage_mode_entity_id": MODE,
            "peak_shaving_entity_id": PEAK,
            "battery_reserve_entity_id": RESERVE,
            "helper_entity_id": "script.house_battery_fail_safe_pass",
            "cancel_helper_entity_id": "script.house_battery_fail_safe_cancel_pass",
            "outstanding_write_settle_until": 0,
            "mutation_cutoff_timestamp": 0,
            "owner_deadline_timestamp": 9999999999,
            "cutoff_cancellation_requested_at": 0,
            "second_pass_completed_before_cutoff": False,
        }
    )
    assert "guard=unavailable" in rendered
    assert f"{first_slot}=on" in rendered
    assert "RECONCILIATION_PENDING:second_pass_not_completed_before_cutoff" in rendered


async def test_script_trace_guard_failure_attempts_no_solis_write_and_notifies(
    hass: HomeAssistant,
) -> None:
    _set_control_states(hass, safe=False, guard="off")
    calls: list[tuple[str, str, dict[str, Any]]] = []

    # Deliberately acknowledge the local helper service without changing the
    # guard, modelling a failed guard assertion/readback.
    async def failed_guard(call: ServiceCall) -> None:
        calls.append((call.domain, call.service, dict(call.data)))

    hass.services.async_register("input_boolean", "turn_on", failed_guard)
    _register_control_services(hass, calls)
    hass.services.async_register("input_boolean", "turn_on", failed_guard)

    async def script_service(call: ServiceCall) -> None:
        calls.append((call.domain, call.service, dict(call.data)))
        if _service_entity_id(call) == "script.house_battery_fail_safe_cancel_pass":
            hass.states.async_set(
                "script.house_battery_fail_safe_cancel_pass", "on"
            )
            hass.states.async_set(
                "script.house_battery_fail_safe_cancel_pass", "off"
            )
            hass.states.async_set("script.house_battery_fail_safe_pass", "off")

    hass.services.async_register("script", "turn_on", script_service)

    supervisor = _script_runner(hass, _script(), "guard-failure")
    await supervisor.async_run()
    await supervisor.async_run()

    assert not any(domain in {"switch", "select"} for domain, _, _ in calls)
    assert not any(
        domain == "script"
        and service == "turn_on"
        and data.get("entity_id") == ["script.house_battery_fail_safe_pass"]
        for domain, service, data in calls
    )
    assert sum(
        domain == "persistent_notification" and service == "create"
        for domain, service, _ in calls
    ) == 1


async def test_script_trace_initial_safe_proof_is_zero_solis_fast_path(
    hass: HomeAssistant,
) -> None:
    _set_control_states(hass, safe=True, guard="on")
    hass.states.async_remove(GUARD)
    hass.states.async_set(GUARD, "on", timestamp=time() - 26)
    hass.states.async_set(
        "persistent_notification.house_battery_watchdog",
        "notifying",
        {"message": "stale"},
    )
    calls: list[tuple[str, str, dict[str, Any]]] = []
    _register_control_services(hass, calls)

    async def script_service(call: ServiceCall) -> None:
        calls.append((call.domain, call.service, dict(call.data)))
        if _service_entity_id(call) == "script.house_battery_fail_safe_cancel_pass":
            hass.states.async_set(
                "script.house_battery_fail_safe_cancel_pass", "on"
            )
            hass.states.async_set(
                "script.house_battery_fail_safe_cancel_pass", "off"
            )
            hass.states.async_set("script.house_battery_fail_safe_pass", "off")

    hass.services.async_register("script", "turn_on", script_service)

    await _script_runner(hass, _script(), "already-safe").async_run()

    assert not any(domain in {"switch", "select"} for domain, _, _ in calls)
    assert not any(
        domain == "script"
        and service == "turn_on"
        and data.get("entity_id") == ["script.house_battery_fail_safe_pass"]
        for domain, service, data in calls
    )
    assert hass.states.get("persistent_notification.house_battery_watchdog") is None


async def test_script_trace_late_write_is_closed_by_deliberate_second_pass(
    hass: HomeAssistant,
) -> None:
    # Every Solis entity looks safe initially. The guard transition still
    # forbids the zero-write fast path because a pre-guard write can settle
    # near the end of the newly anchored settlement horizon.
    _set_control_states(hass, safe=True, guard="off")
    calls: list[tuple[str, str, dict[str, Any]]] = []
    _register_control_services(hass, calls)
    helper = _script_runner(hass, _pass_script(), "helper")
    pass_count = 0
    first_slot = _configured_slots(_load(CONFIG_PATH))[0]

    async def script_service(call: ServiceCall) -> None:
        nonlocal pass_count
        calls.append((call.domain, call.service, dict(call.data)))
        target = _service_entity_id(call)
        if target == "script.house_battery_fail_safe_cancel_pass":
            hass.states.async_set(
                "script.house_battery_fail_safe_cancel_pass", "on"
            )
            hass.states.async_set(
                "script.house_battery_fail_safe_cancel_pass", "off"
            )
            hass.states.async_set("script.house_battery_fail_safe_pass", "off")
            return
        assert target == "script.house_battery_fail_safe_pass"
        pass_count += 1
        variables = dict(call.data.get("variables", {}))

        async def run_pass() -> None:
            hass.states.async_set("script.house_battery_fail_safe_pass", "on")
            await helper.async_run(variables)
            hass.states.async_set("script.house_battery_fail_safe_pass", "off")
            if pass_count == 1:
                # Surface the in-flight pre-guard write during the deliberate
                # settlement horizon, just before pass two may start.
                async def late_write() -> None:
                    await asyncio.sleep(0.22)
                    hass.states.async_set(first_slot, "on")

                hass.async_create_task(late_write())

        hass.async_create_task(run_pass())

    hass.services.async_register("script", "turn_on", script_service)

    await _script_runner(hass, _scaled_script(), "late-write-supervisor").async_run()
    await hass.async_block_till_done()

    assert pass_count == 2
    assert hass.states.get(first_slot).state == "off"
    assert hass.states.get(MODE).state == "Self-Use"
    assert hass.states.get(PEAK).state == "on"
    assert hass.states.get(RESERVE).state == "off"


async def test_recently_on_guard_cannot_take_zero_solis_fast_path(
    hass: HomeAssistant,
) -> None:
    _set_control_states(hass, safe=True, guard="on")
    calls: list[tuple[str, str, dict[str, Any]]] = []
    _register_control_services(hass, calls)
    helper = _script_runner(hass, _pass_script(), "recent-guard-helper")
    pass_count = 0
    helper_tasks: list[asyncio.Task[Any]] = []

    async def script_service(call: ServiceCall) -> None:
        nonlocal pass_count
        calls.append((call.domain, call.service, dict(call.data)))
        target = _service_entity_id(call)
        if target == "script.house_battery_fail_safe_cancel_pass":
            hass.states.async_set(
                "script.house_battery_fail_safe_cancel_pass", "on"
            )
            hass.states.async_set(
                "script.house_battery_fail_safe_cancel_pass", "off"
            )
            hass.states.async_set("script.house_battery_fail_safe_pass", "off")
            return
        assert target == "script.house_battery_fail_safe_pass"
        pass_count += 1
        variables = dict(call.data.get("variables", {}))

        async def run_pass() -> None:
            hass.states.async_set("script.house_battery_fail_safe_pass", "on")
            await helper.async_run(variables)
            hass.states.async_set("script.house_battery_fail_safe_pass", "off")

        helper_tasks.append(hass.async_create_task(run_pass()))

    hass.services.async_register("script", "turn_on", script_service)

    await _script_runner(
        hass, _scaled_script(), "recent-guard-supervisor"
    ).async_run()
    await asyncio.gather(*helper_tasks, return_exceptions=True)

    assert pass_count == 2
    assert any(domain in {"switch", "select"} for domain, _, _ in calls)


async def test_deadline_finalizes_while_child_and_cancellation_resist(
    hass: HomeAssistant,
) -> None:
    _set_control_states(hass, safe=False, guard="off")
    calls: list[tuple[str, str, dict[str, Any]]] = []
    _register_control_services(hass, calls)
    definition = _scaled_script(
        owner_seconds=0.12,
        settle_seconds=0.04,
        finalization_seconds=0.05,
        cancellation_seconds=0.01,
    )
    helper = _script_runner(hass, _pass_script(), "resistant-helper")
    canceller = _script_runner(hass, _cancel_script(), "resistant-canceller")
    cloud_release = asyncio.Event()
    cancellation_release = asyncio.Event()
    background: list[asyncio.Task[Any]] = []
    late_completions: list[tuple[str, str]] = []
    first_slot = _configured_slots(_load(CONFIG_PATH))[0]
    cancel_requests = 0

    async def resistant_switch(call: ServiceCall) -> None:
        calls.append((call.domain, call.service, dict(call.data)))
        entity_id = _service_entity_id(call)
        assert entity_id == first_slot
        # Make every visible Solis state look safe while the child remains
        # detached. Final safety must still report RECONCILIATION_PENDING.
        for slot_entity_id in _configured_slots(_load(CONFIG_PATH)):
            hass.states.async_set(slot_entity_id, "off")
        hass.states.async_set(MODE, "Self-Use")
        hass.states.async_set(PEAK, "on")
        hass.states.async_set(RESERVE, "off")
        await cloud_release.wait()
        # The only operation allowed to escape the bound is safety-increasing.
        hass.states.async_set(entity_id, "off")
        late_completions.append((call.service, entity_id))

    async def resistant_cancel(call: ServiceCall) -> None:
        calls.append((call.domain, call.service, dict(call.data)))
        await cancellation_release.wait()

    hass.services.async_register("switch", "turn_off", resistant_switch)
    hass.services.async_register("script", "turn_off", resistant_cancel)

    async def script_turn_on(call: ServiceCall) -> None:
        nonlocal cancel_requests
        calls.append((call.domain, call.service, dict(call.data)))
        target = _service_entity_id(call)
        if target == "script.house_battery_fail_safe_cancel_pass":
            cancel_requests += 1
            if cancel_requests == 1:
                # Initial stale-child cleanup settles immediately. The cutoff
                # cancellation request below is the resistant one.
                hass.states.async_set(
                    "script.house_battery_fail_safe_cancel_pass", "on"
                )
                hass.states.async_set(
                    "script.house_battery_fail_safe_cancel_pass", "off"
                )
                return

            async def run_canceller() -> None:
                hass.states.async_set(
                    "script.house_battery_fail_safe_cancel_pass", "on"
                )
                await canceller.async_run()
                hass.states.async_set(
                    "script.house_battery_fail_safe_cancel_pass", "off"
                )

            background.append(hass.async_create_task(run_canceller()))
            return
        assert target == "script.house_battery_fail_safe_pass"
        variables = dict(call.data.get("variables", {}))

        async def run_pass() -> None:
            hass.states.async_set("script.house_battery_fail_safe_pass", "on")
            await helper.async_run(variables)
            hass.states.async_set("script.house_battery_fail_safe_pass", "off")

        background.append(hass.async_create_task(run_pass()))

    hass.services.async_register("script", "turn_on", script_turn_on)

    started = monotonic()
    await _script_runner(hass, definition, "deadline-supervisor").async_run()
    elapsed = monotonic() - started

    assert elapsed < 0.2
    assert helper.is_running
    assert canceller.is_running
    assert hass.states.get("script.house_battery_fail_safe_pass").state == "on"
    assert (
        hass.states.get("script.house_battery_fail_safe_cancel_pass").state
        == "on"
    )
    assert late_completions == []
    assert any(domain == "persistent_notification" and service == "create" for domain, service, _ in calls)
    notification = hass.states.get("persistent_notification.house_battery_watchdog")
    assert notification is not None
    assert "RECONCILIATION_PENDING" in notification.attributes["message"]

    cloud_release.set()
    cancellation_release.set()
    await asyncio.gather(*background, return_exceptions=True)
    assert late_completions == [("turn_off", first_slot)]
    assert not any(domain == "select" for domain, _, _ in calls)
    assert not any(domain == "switch" and service == "turn_on" for domain, service, _ in calls)


async def test_automation_churn_and_shutdown_share_one_nonrenewing_owner(
    hass: HomeAssistant,
) -> None:
    _set_control_states(hass, safe=False, guard="off")
    calls: list[tuple[str, str, dict[str, Any]]] = []
    _register_control_services(hass, calls)
    helper = _script_runner(hass, _pass_script(), "churn-helper")
    supervisor = _script_runner(hass, _scaled_script(), "single-supervisor")
    normal_path = _script_runner(
        hass,
        {"sequence": _automation()["action"], "mode": _automation()["mode"]},
        "normal-automation-path",
    )
    shutdown_path = _script_runner(
        hass,
        {
            "sequence": _shutdown_automation()["action"],
            "mode": _shutdown_automation()["mode"],
        },
        "shutdown-automation-path",
    )
    pass_started = asyncio.Event()
    owner_tasks: list[asyncio.Task[Any]] = []
    helper_tasks: list[asyncio.Task[Any]] = []
    owner_starts = 0
    pass_count = 0
    deadlines: list[str] = []
    shutdown_guard_calls = 0

    async def input_boolean_turn_on(call: ServiceCall) -> None:
        nonlocal shutdown_guard_calls
        calls.append((call.domain, call.service, dict(call.data)))
        shutdown_guard_calls += 1
        hass.states.async_set(GUARD, "on")

    hass.services.async_register("input_boolean", "turn_on", input_boolean_turn_on)

    async def script_turn_on(call: ServiceCall) -> None:
        nonlocal owner_starts, pass_count
        calls.append((call.domain, call.service, dict(call.data)))
        target = _service_entity_id(call)
        if target == "script.house_battery_fail_safe":
            def mark_owner() -> None:
                nonlocal owner_starts
                owner_starts += 1

            owner_tasks.append(
                hass.async_create_task(supervisor.async_run(started_action=mark_owner))
            )
            return
        if target == "script.house_battery_fail_safe_cancel_pass":
            if not helper.is_running:
                hass.states.async_set("script.house_battery_fail_safe_pass", "off")
            hass.states.async_set(
                "script.house_battery_fail_safe_cancel_pass", "on"
            )
            hass.states.async_set(
                "script.house_battery_fail_safe_cancel_pass", "off"
            )
            return
        assert target == "script.house_battery_fail_safe_pass"
        pass_count += 1
        variables = dict(call.data.get("variables", {}))
        deadlines.append(str(variables["deadline_timestamp"]))
        pass_started.set()

        async def run_pass() -> None:
            hass.states.async_set("script.house_battery_fail_safe_pass", "on")
            await helper.async_run(variables)
            hass.states.async_set("script.house_battery_fail_safe_pass", "off")

        helper_tasks.append(hass.async_create_task(run_pass()))

    hass.services.async_register("script", "turn_on", script_turn_on)

    # Start through the real normal automation action path.
    await normal_path.async_run()
    await asyncio.wait_for(pass_started.wait(), timeout=1)

    # Rapid heartbeat/Solis churn repeatedly requests the same supervisor. The
    # active single owner and its deadline must survive unchanged.
    for _ in range(20):
        await normal_path.async_run()

    # A shutdown path remains independently invokable while the owner runs. It
    # reasserts a deliberately cleared guard, but cannot cancel/reset the owner.
    hass.states.async_set(GUARD, "off")
    await shutdown_path.async_run()
    assert hass.states.get(GUARD).state == "on"

    await asyncio.gather(*owner_tasks, return_exceptions=True)
    await asyncio.gather(*helper_tasks, return_exceptions=True)

    assert owner_starts == 1
    assert pass_count == 2
    assert len(set(deadlines)) == 1
    assert shutdown_guard_calls >= 2  # owner assertion plus shutdown reassertion
    assert not supervisor.is_running
    assert hass.states.get(MODE).state == "Self-Use"
    assert all(
        hass.states.get(entity_id).state == "off"
        for entity_id in _configured_slots(_load(CONFIG_PATH))
    )
