"""Render tests for the provenance-rich Octopus fused-rate producers."""

from datetime import datetime, timezone
import json
from pathlib import Path

from jinja2 import Environment, Template
import pytest
import yaml

from custom_components.house_battery_control.octopus_windows import (
    parse_fused_export_rates,
    parse_fused_import_rates,
)


TEMPLATE = Path(__file__).resolve().parents[1] / "templates/templates/octopus_fused_day_rates.yaml.j2"
DEPLOYMENT_ROOT = Path(__file__).resolve().parents[1]
SERIAL = "TESTSERIAL"
IMPORT_MPAN = "111"
EXPORT_MPAN = "222"
DEVICE = "exact-device"
IMPORT_EVENT = "event.octopus_energy_electricity_testserial_111_current_day_rates"
IMPORT_NEXT_EVENT = "event.octopus_energy_electricity_testserial_111_next_day_rates"
EXPORT_EVENT = "event.octopus_energy_electricity_testserial_222_export_current_day_rates"
EXPORT_NEXT_EVENT = "event.octopus_energy_electricity_testserial_222_export_next_day_rates"
IMPORT_RETRIEVAL = "sensor.octopus_energy_electricity_testserial_111_rates_data_last_retrieved"
EXPORT_RETRIEVAL = "sensor.octopus_energy_electricity_testserial_222_rates_data_last_retrieved"
DISPATCH_RETRIEVAL = (
    "sensor.octopus_energy_exact-device_intelligent_dispatches_data_last_retrieved"
)
NOW = datetime(2026, 7, 4, 12, tzinfo=timezone.utc)


class _EntityState:
    def __init__(self, value="2026-07-04T12:00:00+00:00"):
        self.state = value
        self.last_changed = NOW


class _States:
    def __init__(self, values):
        self._values = values

    def __call__(self, entity_id):
        return self._values.get(entity_id, _EntityState("unknown")).state

    def __getitem__(self, entity_id):
        return self._values.get(entity_id, _EntityState("unknown"))


def _outer_render():
    rendered = Template(TEMPLATE.read_text()).render(
        electricity_meter_serial_number=SERIAL,
        electricity_meter_mpan_import=IMPORT_MPAN,
        electricity_meter_mpan_export=EXPORT_MPAN,
        ev_charger_device_id=DEVICE,
    )
    return rendered, yaml.safe_load(rendered)


def _rate(start, end, value, **extra):
    return {
        "start": start,
        "end": end,
        "value_inc_vat": value,
        "is_capped": False,
        **extra,
    }


def _render_rate_attribute(sensor, attributes, values):
    states = _States(values)

    def state_attr(entity_id, attribute):
        return attributes.get(entity_id, {}).get(attribute)

    environment = Environment()
    environment.filters["from_json"] = json.loads
    return json.loads(
        environment.from_string(sensor["attributes"]["rates"]).render(
            states=states,
            state_attr=state_attr,
            as_datetime=lambda value: value if isinstance(value, datetime) else datetime.fromisoformat(value),
        )
    )


def test_outer_render_binds_only_exact_configured_sources() -> None:
    rendered, config = _outer_render()
    import_sensor, export_sensor = config["sensor"]
    assert import_sensor["attributes"]["rate_source_entity_id"] == IMPORT_RETRIEVAL
    assert import_sensor["attributes"]["dispatch_source_entity_id"] == DISPATCH_RETRIEVAL
    assert export_sensor["attributes"]["rate_source_entity_id"] == EXPORT_RETRIEVAL
    assert "selectattr" not in rendered
    assert "intelligent_dispatches_data_last_retrieved$" not in rendered


def test_outer_render_serializes_explicit_pep495_fold_fields() -> None:
    _rendered, config = _outer_render()
    attributes = {
        IMPORT_EVENT: {
            "rates": [_rate(
                "2026-07-04T12:00:00+00:00",
                "2026-07-04T12:30:00+00:00",
                "0.07",
            )],
            "tariff_code": "E-2R-TEST-A",
            "min_rate": "0.07",
        },
        IMPORT_NEXT_EVENT: {"rates": []},
    }
    values = {IMPORT_EVENT: _EntityState(), IMPORT_NEXT_EVENT: _EntityState()}
    records = _render_rate_attribute(config["sensor"][0], attributes, values)
    assert records[0]["start_fold"] == 0
    assert records[0]["end_fold"] == 0


def test_template_is_rendered_by_ansible_and_protected_from_static_sync() -> None:
    assert "octopus_fused_day_rates" in (DEPLOYMENT_ROOT / "config.yaml").read_text()
    assert "P octopus_fused_day_rates.yaml" in (
        DEPLOYMENT_ROOT / "files/templates/.rsync-filter"
    ).read_text()
    assert not (DEPLOYMENT_ROOT / "files/templates/octopus_fused_day_rates.yaml").exists()


def test_deployment_preflight_requires_all_rate_sources_and_retrieval_diagnostics() -> None:
    config = (DEPLOYMENT_ROOT / "config.yaml").read_text()
    required = (
        "event.octopus_energy_electricity_{{ electricity_meter_serial_number | lower }}_{{ electricity_meter_mpan_import }}_current_day_rates",
        "event.octopus_energy_electricity_{{ electricity_meter_serial_number | lower }}_{{ electricity_meter_mpan_import }}_next_day_rates",
        "event.octopus_energy_electricity_{{ electricity_meter_serial_number | lower }}_{{ electricity_meter_mpan_export }}_export_current_day_rates",
        "event.octopus_energy_electricity_{{ electricity_meter_serial_number | lower }}_{{ electricity_meter_mpan_export }}_export_next_day_rates",
        "sensor.octopus_energy_electricity_{{ electricity_meter_serial_number | lower }}_{{ electricity_meter_mpan_import }}_rates_data_last_retrieved",
        "sensor.octopus_energy_electricity_{{ electricity_meter_serial_number | lower }}_{{ electricity_meter_mpan_export }}_rates_data_last_retrieved",
        "sensor.octopus_energy_{{ ev_charger_device_id }}_intelligent_dispatches_data_last_retrieved",
    )
    assert all(entity_id in config for entity_id in required)
    assert 'if entity.get("disabled_by") is None' in config


def test_import_render_preserves_missing_minimum_and_parser_rejects_it() -> None:
    _rendered, config = _outer_render()
    attributes = {
        IMPORT_EVENT: {
            "rates": [
                _rate("2026-07-04T12:00:00+00:00", "2026-07-04T12:30:00+00:00", "0.07"),
                _rate("2026-07-04T12:30:00+00:00", "2026-07-04T13:00:00+00:00", "0.30"),
            ],
            "tariff_code": "E-2R-TEST-A",
            # min_rate deliberately absent: producer must preserve null.
        },
        IMPORT_NEXT_EVENT: {"rates": []},
    }
    values = {
        IMPORT_EVENT: _EntityState(),
        IMPORT_NEXT_EVENT: _EntityState(),
        IMPORT_RETRIEVAL: _EntityState(),
        DISPATCH_RETRIEVAL: _EntityState(),
    }
    records = _render_rate_attribute(config["sensor"][0], attributes, values)
    assert records[0]["event_min_rate"] is None
    assert records[0]["is_intelligent_adjusted"] is False
    assert records[0]["retrieval_source_entity_id"] == IMPORT_RETRIEVAL
    assert records[0]["dispatch_source_entity_id"] == DISPATCH_RETRIEVAL
    with pytest.raises(ValueError):
        parse_fused_import_rates(records)


def test_import_render_preserves_null_adjustment_for_parser_rejection() -> None:
    _rendered, config = _outer_render()
    attributes = {
        IMPORT_EVENT: {
            "rates": [
                _rate(
                    "2026-07-04T12:00:00+00:00",
                    "2026-07-04T12:30:00+00:00",
                    "0.07",
                    is_intelligent_adjusted=None,
                ),
                _rate("2026-07-04T12:30:00+00:00", "2026-07-04T13:00:00+00:00", "0.30"),
            ],
            "tariff_code": "E-2R-TEST-A",
            "min_rate": "0.07",
        },
        IMPORT_NEXT_EVENT: {"rates": []},
    }
    values = {IMPORT_EVENT: _EntityState(), IMPORT_NEXT_EVENT: _EntityState()}
    records = _render_rate_attribute(config["sensor"][0], attributes, values)
    assert records[0]["is_intelligent_adjusted"] is None
    with pytest.raises(ValueError, match="Boolean"):
        parse_fused_import_rates(records)


def test_export_render_includes_revision_and_exact_retrieval_provenance() -> None:
    _rendered, config = _outer_render()
    attributes = {
        EXPORT_EVENT: {
            "rates": [
                _rate("2026-07-04T12:00:00+00:00", "2026-07-04T12:30:00+00:00", "0.15"),
                _rate("2026-07-04T12:30:00+00:00", "2026-07-04T13:00:00+00:00", "0.15"),
            ],
            "tariff_code": "E-EXPORT-TEST-A",
        },
        EXPORT_NEXT_EVENT: {"rates": []},
    }
    values = {
        EXPORT_EVENT: _EntityState(),
        EXPORT_NEXT_EVENT: _EntityState(),
        EXPORT_RETRIEVAL: _EntityState(NOW.isoformat()),
    }
    records = _render_rate_attribute(config["sensor"][1], attributes, values)
    assert records[0]["source_revision_at"] == NOW.isoformat()
    assert records[0]["retrieval_source_entity_id"] == EXPORT_RETRIEVAL
    assert parse_fused_export_rates(records)[0].retrieved_at == NOW
