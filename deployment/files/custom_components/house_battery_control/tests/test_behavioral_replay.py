"""Acceptance tests replaying reduced real-world controller incidents."""

from pathlib import Path

import pytest

from custom_components.house_battery_control.tests.behavioral_replay import replay_scenario

SCENARIOS = tuple(sorted((Path(__file__).parent / "scenarios").glob("*.yaml")))
assert SCENARIOS, "no behavioural replay scenarios were found"


@pytest.mark.parametrize("scenario_path", SCENARIOS, ids=lambda path: path.stem)
@pytest.mark.asyncio
async def test_real_world_battery_scenario(hass, scenario_path: Path) -> None:
    await replay_scenario(hass, scenario_path)
