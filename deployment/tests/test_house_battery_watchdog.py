"""Focused offline contract tests for the independent battery watchdog."""

from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
FILES = ROOT / "files"
CONFIG_PATH = FILES / "house_battery_control.yaml"
SCRIPT_PATH = FILES / "scripts" / "house_battery.yaml"
AUTOMATION_PATH = FILES / "automations" / "house_battery.yaml"

GUARD = "input_boolean.house_battery_control_disable"
MODE = "select.garage_inverter_control_storage_mode"
RESERVE = "switch.garage_inverter_control_battery_reserve"


def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text())


def _configured_slots(config: dict[str, Any]) -> list[str]:
    return [
        f"switch.{config['solis']['slot_entity_prefix']}_slot{slot}_{direction}"
        for slot in range(1, 7)
        for direction in ("charge", "discharge")
    ]


def _configured_slot_fields(config: dict[str, Any], field: str) -> list[str]:
    suffix = {
        "time_entity_id": "_time",
        "current_entity_id": "_current",
        "target_soc_entity_id": "_soc",
    }[field]
    domain = {"time_entity_id": "text", "current_entity_id": "number", "target_soc_entity_id": "number"}[field]
    return [
        f"{domain}.{config['solis']['slot_entity_prefix']}_slot{slot}_{direction}{suffix}"
        for slot in range(1, 7)
        for direction in ("charge", "discharge")
    ]


def _script() -> dict[str, Any]:
    return _load(SCRIPT_PATH)["house_battery_fail_safe"]


def _actions(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, list):
        for item in value:
            found.extend(_actions(item))
    elif isinstance(value, dict):
        if "service" in value or "wait_template" in value or "stop" in value:
            found.append(value)
        for key in ("sequence", "then", "else", "default"):
            if key in value:
                found.extend(_actions(value[key]))
        if "repeat" in value:
            found.extend(_actions(value["repeat"].get("sequence", [])))
        for choice in value.get("choose", []):
            found.extend(_actions(choice.get("sequence", [])))
    return found


def test_watchdog_is_small_and_has_no_legacy_or_commissioning_vocabulary() -> None:
    script_text = SCRIPT_PATH.read_text()
    automation_text = AUTOMATION_PATH.read_text()
    assert len(script_text.splitlines()) < 230
    assert len(automation_text.splitlines()) < 80
    for text in (script_text, automation_text):
        assert "commission" not in text.lower()
        assert "candidate" not in text.lower()
        assert "fingerprint" not in text.lower()
        assert "journal" not in text.lower()
        assert "legacy" not in text.lower()


def test_guard_is_first_mutation_and_is_never_cleared() -> None:
    script = _script()
    actions = _actions(script["sequence"])
    first_service = next(action for action in actions if "service" in action)
    assert first_service["service"] == "input_boolean.turn_on"
    assert first_service["target"]["entity_id"] == "{{ guard_entity_id }}"
    assert "input_boolean.turn_off" not in SCRIPT_PATH.read_text()
    assert script["mode"] == "single"


def test_fail_safe_covers_all_native_slots_and_safe_policy() -> None:
    config = _load(CONFIG_PATH)
    script = _script()
    variables = script["sequence"][0]["variables"]
    assert variables["guard_entity_id"] == GUARD
    assert variables["storage_mode_entity_id"] == MODE
    assert variables["battery_reserve_entity_id"] == RESERVE
    assert variables["slot_entity_ids"] == _configured_slots(config)
    assert variables["slot_time_entity_ids"] == _configured_slot_fields(
        config, "time_entity_id"
    )
    assert variables["slot_current_entity_ids"] == _configured_slot_fields(
        config, "current_entity_id"
    )

    text = SCRIPT_PATH.read_text()
    for entity_id in [
        GUARD,
        MODE,
        RESERVE,
        *_configured_slots(config),
        *_configured_slot_fields(config, "time_entity_id"),
        *_configured_slot_fields(config, "current_entity_id"),
    ]:
        assert entity_id in text

    services = [action.get("service") for action in _actions(script["sequence"])]
    assert services.count("switch.turn_off") >= 1
    assert "select.select_option" in services
    assert "persistent_notification.create" in services
    assert "Self-Use" in text


def test_writes_are_guarded_and_reconciliation_is_bounded() -> None:
    script = _script()
    text = SCRIPT_PATH.read_text()
    assert script["sequence"][3]["value_template"] == (
        "{{ states(guard_entity_id) == 'on' }}"
    )
    repeat = next(action["repeat"] for action in script["sequence"] if "repeat" in action)
    assert repeat["count"] == 2
    assert sum("wait_template" in action for action in _actions(repeat["sequence"])) >= 1
    assert "expand(slot_entity_ids)" in text
    assert "map('states')" not in text
    assert "seconds: 10" in text
    assert "continue_on_error: true" in text


def test_fail_safe_skips_safe_controls_and_isolates_slot_failures() -> None:
    script = _script()
    text = SCRIPT_PATH.read_text()
    repeat = next(action["repeat"] for action in script["sequence"] if "repeat" in action)
    sequence = repeat["sequence"]

    # The script computes only the unsafe slots, then turns them off one at a
    # time so a cloud timeout for one slot cannot suppress the other attempts.
    unsafe = sequence[0]["variables"]["unsafe_slot_entity_ids"]
    assert "rejectattr('state', 'eq', 'off')" in unsafe
    slot_repeat = sequence[1]["repeat"]
    assert slot_repeat["for_each"] == "{{ unsafe_slot_entity_ids }}"
    slot_action = slot_repeat["sequence"][0]
    assert slot_action["service"] == "switch.turn_off"
    assert slot_action["target"]["entity_id"] == "{{ repeat.item }}"
    assert slot_action["continue_on_error"] is True
    time_repeat = sequence[2]["repeat"]
    assert time_repeat["for_each"] == "{{ slot_time_entity_ids }}"
    assert time_repeat["sequence"][0]["choose"][0]["sequence"][0]["service"] == (
        "text.set_value"
    )
    current_repeat = sequence[3]["repeat"]
    assert current_repeat["for_each"] == "{{ slot_current_entity_ids }}"
    assert current_repeat["sequence"][0]["choose"][0]["sequence"][0]["service"] == (
        "number.set_value"
    )
    # Each fixed Solis control is guarded by its own state comparison. A
    # fully-safe state therefore performs no Solis service writes at all.
    assert "states(storage_mode_entity_id) != 'Self-Use'" in text
    assert "states(battery_reserve_entity_id) != 'off'" in text
    assert "states(repeat.item) != '00:00-00:00'" in text
    assert "float(default=-1) != 0" in text
    assert text.count("continue_on_error: true") >= 5


def test_automation_latches_only_hard_or_stale_failures() -> None:
    automations = _load(AUTOMATION_PATH)
    assert len(automations) == 1
    normal = automations[0]
    trigger_ids = {trigger["id"] for trigger in normal["trigger"]}
    assert trigger_ids == {
        "every_minute",
        "controller_health_changed",
        "controller_unavailable",
        "controller_unknown",
        "control_disable_changed",
    }
    condition = normal["condition"][0]["value_template"]
    assert "heartbeat" in condition
    assert "health == 'fail_safe'" in condition
    assert "controller_unavailable" in condition
    assert "controller_unknown" in condition
    assert "180" in condition
    assert "health_state.last_changed" in condition
    assert "heartbeat is none" in condition
    assert "> 600" in condition
    assert "not (health in ['unavailable', 'unknown']" in condition
    assert "<= 600" in condition
    assert "home_assistant_start" not in condition
    assert "health != 'healthy'" not in condition
    assert "health == 'degraded'" not in condition
    assert "trigger.id == 'control_disable_changed'" in condition
    unavailable = next(
        trigger for trigger in normal["trigger"] if trigger["id"] == "controller_unavailable"
    )
    unknown = next(
        trigger for trigger in normal["trigger"] if trigger["id"] == "controller_unknown"
    )
    assert unavailable["for"] == "00:10:00"
    assert unknown["for"] == "00:10:00"
