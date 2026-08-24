"""Deployment contract for the pinned Solis Inverter component."""

from pathlib import Path


DEPLOYMENT_ROOT = Path(__file__).parents[1]


def test_solis_inverter_uses_pristine_pinned_release_without_overlay() -> None:
    config = (DEPLOYMENT_ROOT / "config.yaml").read_text()

    solis_block_start = config.index("component_name: Solis Inverter")
    solis_block_end = config.index(
        "component_name: Solis Cloud Control", solis_block_start
    )
    solis_block = config[solis_block_start:solis_block_end]

    assert 'component_version: "v4.0.1"' in solis_block
    assert "component_source_revision: pristine-v4.0.1" in solis_block
    assert "component_overlay_dir" not in solis_block
    assert "solis-v4.0.1-poll-recovery" not in config


def test_custom_component_source_identity_includes_optional_revision() -> None:
    role = (
        DEPLOYMENT_ROOT / "roles" / "custom_component" / "tasks" / "main.yaml"
    ).read_text()

    assert "component_source_revision is defined" in role
    assert "'#' ~ component_source_revision" in role
