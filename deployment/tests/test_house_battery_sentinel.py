"""Offline contract for the notification-only stale-heartbeat sentinel."""
from pathlib import Path

import yaml
from jinja2 import Environment

AUTOMATION_PATH = Path(__file__).resolve().parents[1] / "files/automations/house_battery.yaml"


def sentinel():
    return yaml.safe_load(AUTOMATION_PATH.read_text())[0]


def test_startup_grace_and_minute_trigger():
    value = sentinel()
    assert value['id'] == 'house_battery_stale_heartbeat_sentinel'
    assert value['mode'] == 'single'
    assert value['trigger'] == [
        {'platform': 'homeassistant', 'event': 'start', 'id': 'startup_grace'},
        {'platform': 'time_pattern', 'minutes': '/1', 'id': 'every_minute'},
    ]
    assert value['action'][0]['choose'][0]['sequence'] == [{'delay': {'minutes': 10}}]


def test_stale_notification_and_recovery_never_control_inverter():
    decision = sentinel()['action'][1]
    notice = decision['choose'][0]['sequence'][0]
    clear = decision['default'][0]
    assert notice['service'] == 'persistent_notification.create'
    assert clear['service'] == 'persistent_notification.dismiss'
    assert notice['data']['notification_id'] == clear['data']['notification_id']
    text = AUTOMATION_PATH.read_text()
    assert text.count('service:') == 2
    assert 'select.select_option' not in text
    assert 'switch.turn_' not in text


def test_heartbeat_boundaries():
    template = sentinel()['action'][1]['choose'][0]['conditions'][0]['value_template']
    for sample, expected in [(None, True), (819, True), (820, False), (1000, False), (1060, False), (1061, True)]:
        rendered = Environment().from_string(template).render(
            states=lambda _: sample,
            now=lambda: 1000,
            as_timestamp=lambda value, default=None: default if value is None else value,
        )
        assert rendered.strip() == str(expected)
