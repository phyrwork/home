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
    assert '582c5a9841ed069067ab81468e6b9f0fe476050a4bc27677752158a03095d1db' in guard['ansible.builtin.assert']['that'][0]
    assert '8cc95bb74c7acbd99e4e27c0ce619def9691d65f303de661d2f0d1347c8c5834' in guard['ansible.builtin.assert']['that'][0]
    edit = mutation['ansible.builtin.replace']
    source = 'async with async_timeout.timeout(10):\n    await request()\n'
    expected = 'async with async_timeout.timeout(45):\n    await request()\n'
    result = re.sub(edit['regexp'], edit['replace'], source)
    assert result == expected
    assert re.sub(edit['regexp'], edit['replace'], result) == expected
    assert mutation['notify'] == 'restart ha'
    assert validation['ansible.builtin.command'] == 'ha core check'
    assert validation['when'] == 'solis_timeout_patch.changed'
