"""The reviewed Solis telemetry timeout patch remains narrow and repeatable."""
import re
from pathlib import Path

import yaml

ROLE = Path(__file__).parents[1] / 'roles/custom_component/tasks/main.yaml'


def test_timeout_patch_is_scoped_guarded_idempotent_and_validated():
    tasks = yaml.safe_load(ROLE.read_text())
    patch = next(t for t in tasks if t['name'] == 'Allow slow SolisCloud telemetry responses')
    assert patch['when'] == "component_domain == 'solis'"
    before, guard, mutation, validation = patch['block']
    assert before['ansible.builtin.stat']['checksum_algorithm'] == 'sha256'
    assert 'd710319cf46fb204e42270e154daa100fafeba68d4f7b6e8305c51830cb46518' in guard['ansible.builtin.assert']['that'][0]
    assert 'afa1e8a61f86106ae8a68383c83509b1364d33cca9b0ab44f3fc9cbe63e1c23e' in guard['ansible.builtin.assert']['that'][0]
    edit = mutation['ansible.builtin.replace']
    source = 'DEFAULT_REQUEST_TIMEOUT = 30'
    expected = 'DEFAULT_REQUEST_TIMEOUT = 45'
    result = re.sub(edit['regexp'], edit['replace'], source)
    assert result == expected
    assert re.sub(edit['regexp'], edit['replace'], result) == expected
    assert mutation['notify'] == 'restart ha'
    assert validation['ansible.builtin.command'] == 'ha core check'
    assert validation['when'] == 'solis_timeout_patch.changed'
