"""Offline tests for the read-only house-battery evidence collector."""

import asyncio
from datetime import UTC, datetime
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import house_battery_evidence as evidence


def test_entity_and_service_filters_cover_every_slot_field() -> None:
    for slot in range(1, 7):
        for direction in ("charge", "discharge"):
            assert evidence.is_relevant_entity(
                f"switch.garage_inverter_control_slot{slot}_{direction}"
            )
            assert evidence.is_relevant_entity(
                f"text.garage_inverter_control_slot{slot}_{direction}_time"
            )
            assert evidence.is_relevant_entity(
                f"number.garage_inverter_control_slot{slot}_{direction}_current"
            )
            assert evidence.is_relevant_entity(
                f"number.garage_inverter_control_slot{slot}_{direction}_soc"
            )

    assert evidence.is_relevant_entity("sensor.house_battery_control_health")
    assert evidence.is_relevant_entity("sensor.garage_inverter_telemetry_battery_power")
    assert evidence.is_relevant_entity(
        "sensor.octopus_energy_electricity_21l4421345_2700007165105_current_demand"
    )
    assert evidence.is_relevant_entity("sensor.ev_charger_energy_meter_power")
    assert evidence.is_relevant_entity("input_boolean.house_battery_control_disable")
    assert evidence.is_relevant_entity("script.house_battery_fail_safe")
    assert evidence.is_relevant_entity("automation.house_battery_independent_watchdog")
    assert evidence.is_relevant_entity(
        "binary_sensor.octopus_energy_charger_intelligent_dispatching"
    )
    assert evidence.is_relevant_entity(
        "switch.octopus_energy_charger_intelligent_smart_charge"
    )
    assert not evidence.is_relevant_entity("light.garage")

    relevant = {
        "event_type": "call_service",
        "data": {
            "domain": "switch",
            "service": "turn_off",
            "service_data": {
                "entity_id": [
                    "light.garage",
                    "switch.garage_inverter_control_slot1_discharge",
                ]
            },
        },
    }
    unrelated = {
        "event_type": "call_service",
        "data": {"service_data": {"entity_id": "light.garage"}},
    }
    assert evidence.is_relevant_service_event(relevant)
    assert evidence.is_relevant_service_event(
        {
            "event_type": "call_service",
            "data": {
                "domain": "input_boolean",
                "service": "turn_on",
                "service_data": {
                    "entity_id": "input_boolean.house_battery_control_disable"
                },
            },
        }
    )
    assert not evidence.is_relevant_service_event(unrelated)
    assert not evidence.is_relevant_service_event(
        {"event_type": "state_changed", "data": relevant["data"]}
    )


def test_state_filter_keeps_raw_relevant_mappings_in_order() -> None:
    states = [
        {"entity_id": "light.garage", "state": "on"},
        {"entity_id": "sensor.house_battery_control_health", "state": "healthy"},
        {
            "entity_id": "switch.garage_inverter_control_slot6_charge",
            "state": "off",
            "attributes": {"raw": [1, 2, 3]},
        },
    ]
    filtered = evidence.filter_states(states)
    assert filtered == states[1:]
    assert filtered[1] is states[2]


def test_serialization_is_utc_jsonl_and_redacts_credentials() -> None:
    token = "top-secret-token"
    encoded = evidence.serialize_record(
        {
            "recorded_at": datetime(2026, 8, 25, 13, 30, tzinfo=UTC),
            "access_token": token,
            "nested": {
                "password": "battery-password",
                "message": f"connection failed for {token}",
            },
        },
        secrets=(token,),
    )
    assert encoded.endswith(b"\n")
    assert token.encode() not in encoded
    assert b"battery-password" not in encoded
    decoded = json.loads(encoded)
    assert decoded["recorded_at"] == "2026-08-25T13:30:00Z"
    assert decoded["access_token"] == "[REDACTED]"
    assert decoded["nested"]["password"] == "[REDACTED]"
    assert decoded["nested"]["message"] == "connection failed for [REDACTED]"


@pytest.mark.parametrize("value", ("", "0", "-1", "nan", "10d", "1e3"))
def test_duration_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError):
        evidence.parse_duration(value)


def test_duration_url_and_token_source_config_validation(tmp_path: Path) -> None:
    assert evidence.parse_duration("1.5h") == 5400
    assert evidence.SNAPSHOT_INTERVAL_SECONDS == 60
    assert evidence.websocket_url("https://home.example/") == (
        "wss://home.example/api/websocket"
    )
    assert evidence.websocket_url("http://home.example/prefix") == (
        "ws://home.example/prefix/api/websocket"
    )
    with pytest.raises(ValueError):
        evidence.websocket_url("https://user:secret@home.example")
    with pytest.raises(ValueError):
        evidence.websocket_url("home.example")

    output = tmp_path / "evidence.jsonl"
    config = evidence.build_config(
        base_url="https://home.example",
        output=str(output),
        duration="24h",
        token_file=None,
        token_env=None,
    )
    assert config.token_env == evidence.DEFAULT_TOKEN_ENV
    assert evidence.read_token(config, {evidence.DEFAULT_TOKEN_ENV: " cached "}) == "cached"

    token_file = tmp_path / "token"
    token_file.write_text("file-token\n")
    file_config = evidence.build_config(
        base_url="https://home.example",
        output=str(output),
        duration="60s",
        token_file=str(token_file),
        token_env=None,
    )
    assert evidence.read_token(file_config) == "file-token"
    with pytest.raises(ValueError):
        evidence.build_config(
            base_url="https://home.example",
            output=str(output),
            duration="1m",
            token_file=str(token_file),
            token_env="HA_API_TOKEN",
        )
    with pytest.raises(ValueError):
        evidence.build_config(
            base_url="https://home.example",
            output=str(token_file),
            duration="1m",
            token_file=str(token_file),
            token_env=None,
        )
    with pytest.raises(ValueError):
        evidence.build_config(
            base_url="https://home.example",
            output=str(tmp_path),
            duration="1m",
            token_file=None,
            token_env="HA_API_TOKEN",
        )
    with pytest.raises(ValueError):
        evidence.build_config(
            base_url="https://home.example",
            output=str(output),
            duration="1m",
            token_file=None,
            token_env="invalid-name",
        )


def test_jsonl_writer_reopens_in_append_mode_with_restrictive_permissions(
    tmp_path: Path,
) -> None:
    output = tmp_path / "capture" / "evidence.jsonl"
    with evidence.JsonlWriter(output) as writer:
        writer.append("connection_established", {"attempt": 1})
    first = output.read_bytes()
    with evidence.JsonlWriter(output) as writer:
        writer.append("snapshot", {"states": []})

    lines = output.read_text().splitlines()
    assert output.read_bytes().startswith(first)
    assert [json.loads(line)["kind"] for line in lines] == [
        "connection_established",
        "snapshot",
    ]
    assert os.stat(output).st_mode & 0o077 == 0


@pytest.mark.parametrize("failure", (ConnectionError, asyncio.TimeoutError))
async def test_collector_reconnects_and_appends_without_exposing_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[BaseException],
) -> None:
    output = tmp_path / "reconnect.jsonl"
    config = evidence.build_config(
        base_url="https://home.example",
        output=str(output),
        duration="2s",
        token_file=None,
        token_env=None,
    )
    secret = "reconnect-secret"

    class OfflineCollector(evidence.EvidenceCollector):
        calls = 0

        async def _collect_connection(self, session, deadline, attempt):
            del session, deadline, attempt
            self.calls += 1
            if self.calls == 1:
                raise failure(f"dropped while using {secret}")
            self.request_stop()

    monkeypatch.setattr(evidence, "MAXIMUM_RECONNECT_DELAY_SECONDS", 0.0)
    with evidence.JsonlWriter(output, secrets=(secret,)) as writer:
        collector = OfflineCollector(config, secret, writer)
        await collector.run()

    raw = output.read_text()
    records = [json.loads(line) for line in raw.splitlines()]
    assert collector.calls == 2
    assert secret not in raw
    assert [record["kind"] for record in records] == [
        "collector_started",
        "connection_lost",
        "collector_stopped",
    ]
    assert records[-1]["reason"] == "stop_requested"


async def test_continuing_events_cannot_hide_a_stalled_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "snapshot-timeout.jsonl"
    config = evidence.build_config(
        base_url="https://home.example",
        output=str(output),
        duration="2s",
        token_file=None,
        token_env=None,
    )

    class FakeWebSocket:
        def __init__(self) -> None:
            self.handshake = iter(({"type": "auth_required"}, {"type": "auth_ok"}))
            self.sent: list[dict[str, object]] = []
            self.received = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def receive_json(self):
            return next(self.handshake)

        async def send_json(self, payload):
            self.sent.append(payload)

        async def receive(self, *, timeout):
            del timeout
            self.received += 1
            event = {
                "type": "event",
                "event": {
                    "event_type": "state_changed",
                    "data": {
                        "entity_id": "sensor.house_battery_control_health",
                        "old_state": None,
                        "new_state": {"state": "healthy"},
                    },
                },
            }
            return SimpleNamespace(type=evidence.WSMsgType.TEXT, data=json.dumps(event))

    class FakeSession:
        def __init__(self, websocket) -> None:
            self.websocket = websocket

        def ws_connect(self, *_args, **_kwargs):
            return self.websocket

    websocket = FakeWebSocket()
    monkeypatch.setattr(evidence, "SNAPSHOT_RESPONSE_TIMEOUT_SECONDS", 0.0)
    with evidence.JsonlWriter(output) as writer:
        collector = evidence.EvidenceCollector(config, "fake-token", writer)
        with pytest.raises(asyncio.TimeoutError, match="get_states"):
            await collector._collect_connection(
                FakeSession(websocket),
                asyncio.get_running_loop().time() + 2,
                1,
            )

    assert websocket.received == 1
    assert [payload["type"] for payload in websocket.sent] == [
        "auth",
        "subscribe_events",
        "subscribe_events",
        "get_states",
    ]


async def test_reconnected_protocol_immediately_writes_a_filtered_snapshot(
    tmp_path: Path,
) -> None:
    output = tmp_path / "filtered-snapshot.jsonl"
    config = evidence.build_config(
        base_url="https://home.example",
        output=str(output),
        duration="2s",
        token_file=None,
        token_env=None,
    )

    class FakeWebSocket:
        def __init__(self) -> None:
            self.handshake = iter(({"type": "auth_required"}, {"type": "auth_ok"}))
            self.sent: list[dict[str, object]] = []
            self.collector: evidence.EvidenceCollector | None = None
            self.results = iter(
                (
                    {"type": "result", "id": 1, "success": True, "result": None},
                    {"type": "result", "id": 2, "success": True, "result": None},
                    {
                        "type": "result",
                        "id": 3,
                        "success": True,
                        "result": [
                            {"entity_id": "light.garage", "state": "on"},
                            {
                                "entity_id": "sensor.house_battery_control_health",
                                "state": "healthy",
                            },
                        ],
                    },
                )
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def receive_json(self):
            return next(self.handshake)

        async def send_json(self, payload):
            self.sent.append(payload)

        async def receive(self, *, timeout):
            del timeout
            result = next(self.results)
            if result["id"] == 3:
                assert self.collector is not None
                self.collector.request_stop()
            return SimpleNamespace(type=evidence.WSMsgType.TEXT, data=json.dumps(result))

    class FakeSession:
        def __init__(self, websocket) -> None:
            self.websocket = websocket

        def ws_connect(self, *_args, **_kwargs):
            return self.websocket

    websocket = FakeWebSocket()
    with evidence.JsonlWriter(output) as writer:
        collector = evidence.EvidenceCollector(config, "fake-token", writer)
        websocket.collector = collector
        await collector._collect_connection(
            FakeSession(websocket),
            asyncio.get_running_loop().time() + 2,
            2,
        )

    assert [payload["type"] for payload in websocket.sent] == [
        "auth",
        "subscribe_events",
        "subscribe_events",
        "get_states",
    ]
    assert [payload.get("event_type") for payload in websocket.sent[1:3]] == [
        "state_changed",
        "call_service",
    ]
    records = [json.loads(line) for line in output.read_text().splitlines()]
    assert records[0]["kind"] == "connection_established"
    assert records[0]["attempt"] == 2
    assert records[1]["kind"] == "snapshot"
    assert records[1]["states"] == [
        {"entity_id": "sensor.house_battery_control_health", "state": "healthy"}
    ]
