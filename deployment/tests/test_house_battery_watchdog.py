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
PEAK = "switch.garage_inverter_control_grid_peak_shaving"
RESERVE = "switch.garage_inverter_control_battery_reserve"


def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text())


def _configured_slots(config: dict[str, Any]) -> list[str]:
    return [
        direction["enable_entity_id"]
        for slot in config["solis"]["slots"]
        for direction in (slot["charge"], slot["discharge"])
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
    assert len(script_text.splitlines()) < 150
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
    assert variables["peak_shaving_entity_id"] == PEAK
    assert variables["battery_reserve_entity_id"] == RESERVE
    assert variables["slot_entity_ids"] == _configured_slots(config)

    text = SCRIPT_PATH.read_text()
    for entity_id in [GUARD, MODE, PEAK, RESERVE, *_configured_slots(config)]:
        assert entity_id in text

    services = [action.get("service") for action in _actions(script["sequence"])]
    assert services.count("switch.turn_off") >= 1
    assert "select.select_option" in services
    assert "switch.turn_on" in services
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


def test_automation_has_startup_stale_health_guard_and_shutdown_triggers() -> None:
    automations = _load(AUTOMATION_PATH)
    assert len(automations) == 2
    normal, shutdown = automations
    trigger_ids = {trigger["id"] for trigger in normal["trigger"]}
    assert trigger_ids == {
        "home_assistant_start",
        "every_minute",
        "heartbeat_changed",
        "controller_health_changed",
        "control_disable_changed",
    }
    condition = normal["condition"][0]["value_template"]
    assert "heartbeat" in condition
    assert "healthy" in condition
    assert "180" in condition
    assert shutdown["trigger"][0]["event"] == "shutdown"
    assert shutdown["action"][0]["service"] == "input_boolean.turn_on"
    assert shutdown["action"][1]["service"] == "script.turn_on"
