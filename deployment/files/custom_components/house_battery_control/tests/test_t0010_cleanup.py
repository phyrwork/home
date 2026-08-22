"""Offline acceptance checks for removal of the retired helper boundary."""

from pathlib import Path

import yaml


REMOVED_IDS = (
    "input_number.house_battery_" + "state_of_charge",
    "input_number.house_battery_" + "state_of_charge_target",
    "input_select.house_battery_" + "operating_mode",
)
LEGACY_TOKEN = "stub_" + "inverter"
RETAINED_IDS = (
    "input_number.house_battery_power_limit",
    "input_number.house_battery_reserve_margin",
    "input_number.house_battery_export_hysteresis",
)


def _files_root() -> Path:
    return Path(__file__).parents[3]


def test_exact_helper_cleanup_and_observation_helper_retention() -> None:
    numbers = yaml.safe_load((_files_root() / "input_numbers/house_battery.yaml").read_text())
    assert set(numbers) == {
        "house_battery_power_limit",
        "house_battery_reserve_margin",
        "house_battery_export_hysteresis",
    }
    assert not (_files_root() / "input_selects/house_battery.yaml").exists()
    deployed_config = (_files_root() / "house_battery_control.yaml").read_text()
    assert all(entity_id in deployed_config for entity_id in RETAINED_IDS)


def _scan_paths() -> tuple[Path, ...]:
    deployment = _files_root().parent
    roots = (
        deployment / "files/custom_components/house_battery_control",
        deployment / "tests",
        deployment / "files",
        deployment / "templates",
    )
    suffixes = {".py", ".yaml", ".yml", ".j2"}
    return tuple(
        sorted(
            {
                path
                for root in roots
                if root.exists()
                for path in root.rglob("*")
                if path.is_file() and path.suffix in suffixes
            }
        )
    )


def test_removed_ids_and_stub_boundary_are_absent_from_active_sources() -> None:
    deployment = _files_root().parent
    allowed: set[tuple[Path, str]] = {
        (deployment / "files/custom_components/house_battery_control/solis_config.py", token)
        for token in REMOVED_IDS
    }
    allowed.add(
        (
            deployment / "files/custom_components/house_battery_control/tests/test_solis_config.py",
            REMOVED_IDS[0],
        )
    )
    tokens = (*REMOVED_IDS, LEGACY_TOKEN)
    seen: set[tuple[Path, str]] = set()
    for path in _scan_paths():
        for line in path.read_text().splitlines():
            for token in tokens:
                if token in line:
                    occurrence = (path, token)
                    assert occurrence in allowed, f"unexpected retired reference: {path}: {token}"
                    seen.add(occurrence)

    assert all(
        (deployment / "files/custom_components/house_battery_control/solis_config.py", token)
        in seen
        for token in REMOVED_IDS
    )
    assert not (
        deployment
        / (
            "files/custom_components/house_battery_control/dependencies/"
            + "stub_"
            + "inverter.py"
        )
    ).exists()


def test_legacy_power_limit_is_the_only_retained_helper_reference() -> None:
    config_source = (_files_root() / "house_battery_control.yaml").read_text()
    assert RETAINED_IDS[0] in config_source
    assert all(entity_id not in config_source for entity_id in REMOVED_IDS)
