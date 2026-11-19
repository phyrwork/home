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
    text = yaml.safe_dump(sentinel())
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


def test_degraded_alert_requires_continuous_hour_and_current_degradation():
    automations = yaml.safe_load(AUTOMATION_PATH.read_text())
    alert = next(a for a in automations if a['id'] == 'house_battery_degraded_health_alert')
    decision = alert['action'][0]['choose']
    template = decision[0]['conditions'][0]['value_template']
    for health, since, expected in [
        ('degraded', None, False),
        ('degraded', 6401, False),
        ('degraded', 6400, True),
        ('degraded', 6399, True),
        ('degraded', 10001, False),
        ('healthy', 6399, False),
        ('unavailable', 6399, False),
    ]:
        rendered = Environment().from_string(template).render(
            state_attr=lambda *_: since,
            is_state=lambda _, value: health == value,
            now=lambda: 10000,
            as_timestamp=lambda value, default=None: default if value is None else value,
        )
        assert rendered.strip() == str(expected)
    assert decision[1]['conditions'][0]['state'] == 'healthy'
    notice = decision[0]['sequence'][0]
    clear = decision[1]['sequence'][0]
    assert notice['data']['notification_id'] == clear['data']['notification_id']
    assert alert['trigger'][1]['to'] == 'healthy'
