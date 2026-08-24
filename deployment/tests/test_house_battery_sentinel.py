"""Offline contract for the mode-only stale-heartbeat sentinel."""

from pathlib import Path
from typing import Any

import yaml


AUTOMATION_PATH = (
    Path(__file__).resolve().parents[1] / "files" / "automations" / "house_battery.yaml"
)
HEARTBEAT = "sensor.house_battery_control_heartbeat"
MODE = "select.garage_inverter_control_storage_mode"


def _load() -> list[dict[str, Any]]:
    return yaml.safe_load(AUTOMATION_PATH.read_text())


def test_sentinel_has_one_minute_trigger_and_startup_grace() -> None:
    automations = _load()
    assert len(automations) == 1
    sentinel = automations[0]
    assert sentinel["id"] == "house_battery_stale_heartbeat_sentinel"
    assert sentinel["trigger"] == [
        {"platform": "homeassistant", "event": "start", "id": "startup_grace"},
        {"platform": "time_pattern", "minutes": "/1", "id": "every_minute"},
    ]
    assert sentinel["action"][0]["choose"][0]["sequence"] == [
        {"delay": {"minutes": 10}}
    ]
    condition = sentinel["condition"][0]["conditions"][1]["value_template"]
    assert HEARTBEAT in condition
    assert "> 180" in condition
    assert "> 60" in condition


def test_sentinel_rechecks_heartbeat_and_mode_then_writes_only_self_use() -> None:
    sentinel = _load()[0]
    actions = sentinel["action"]
    assert actions[1]["condition"] == "template"
    assert HEARTBEAT in actions[1]["value_template"]
    assert actions[2]["condition"] == "template"
    assert MODE in actions[2]["value_template"]
    assert actions[3] == {
        "service": "select.select_option",
        "target": {"entity_id": MODE},
        "data": {"option": "Self-Use"},
    }
    text = AUTOMATION_PATH.read_text()
    assert "script." not in text
    assert "input_boolean" not in text
    assert "switch." not in text
    assert "number." not in text
    assert "text." not in text
    assert text.count("service:") == 1
