#!/usr/bin/env python3
"""Collect read-only Home Assistant evidence for house-battery commissioning."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from aiohttp import ClientSession, WSMsgType

SNAPSHOT_INTERVAL_SECONDS = 60.0
SNAPSHOT_RESPONSE_TIMEOUT_SECONDS = 15.0
MAXIMUM_RECONNECT_DELAY_SECONDS = 30.0
MAXIMUM_MESSAGE_BYTES = 32 * 1024 * 1024
DEFAULT_TOKEN_ENV = "HA_API_TOKEN"
_REDACTED = "[REDACTED]"
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SENSITIVE_KEY_PARTS = (
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "password",
    "refresh_token",
    "secret",
    "token",
)

EXACT_ENTITIES = frozenset(
    {
        "automation.house_battery_stale_heartbeat_sentinel",
        "automation.house_battery_independent_watchdog",
        "input_boolean.house_battery_control_disable",
        "input_number.house_battery_cycle_discharge_duration_minutes",
        "script.house_battery_fail_safe",
        "sensor.ev_charger_energy_meter_power",
        "sensor.current_export_electricity_21l4421345_2700009249389",
    }
)
RELEVANT_PREFIXES = (
    "sensor.house_battery_",
    "sensor.garage_inverter_telemetry_",
    "select.garage_inverter_control_",
    "switch.garage_inverter_control_",
    "number.garage_inverter_control_",
    "text.garage_inverter_control_",
    "datetime.garage_inverter_control_",
    "sensor.octopus_energy_electricity_21l4421345_",
    "binary_sensor.octopus_energy_",
    "event.octopus_energy_",
    "number.octopus_energy_",
    "switch.octopus_energy_",
    "sensor.ev_charger_",
)


class AuthenticationError(RuntimeError):
    """Home Assistant rejected the supplied cached credential."""


@dataclass(frozen=True, slots=True)
class CollectorConfig:
    websocket_url: str
    output: Path
    duration_seconds: float
    token_file: Path | None
    token_env: str | None


def parse_duration(value: str) -> float:
    """Parse a positive finite duration with optional s/m/h suffix."""

    text = value.strip().lower()
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([smh]?)", text)
    if match is None:
        raise ValueError("duration must be a positive number with optional s, m or h suffix")
    try:
        amount = Decimal(match.group(1))
    except InvalidOperation as exc:
        raise ValueError("duration must be numeric") from exc
    multiplier = {"": Decimal(1), "s": Decimal(1), "m": Decimal(60), "h": Decimal(3600)}[
        match.group(2)
    ]
    seconds = float(amount * multiplier)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("duration must be positive and finite")
    return seconds


def websocket_url(base_url: str) -> str:
    """Validate an HA base URL and derive its WebSocket endpoint."""

    parsed = urlsplit(base_url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("base URL must be an absolute http or https URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("base URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("base URL must not contain a query or fragment")
    path = parsed.path.rstrip("/")
    if not path.endswith("/api/websocket"):
        path += "/api/websocket"
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


def build_config(
    *,
    base_url: str,
    output: str,
    duration: str,
    token_file: str | None,
    token_env: str | None,
) -> CollectorConfig:
    """Validate CLI values without reading a credential."""

    if token_file is not None and token_env is not None:
        raise ValueError("choose either a token file or token environment variable")
    selected_env = DEFAULT_TOKEN_ENV if token_file is None and token_env is None else token_env
    if selected_env is not None and _ENV_NAME.fullmatch(selected_env) is None:
        raise ValueError("token environment variable name is invalid")
    output_path = Path(output).expanduser()
    if not output or output_path.name in ("", ".", ".."):
        raise ValueError("output must be a JSONL file path")
    if output_path.exists() and output_path.is_dir():
        raise ValueError("output must not be a directory")
    file_path = None if token_file is None else Path(token_file).expanduser()
    if file_path is not None and output_path.resolve() == file_path.resolve():
        raise ValueError("output and token file must be different paths")
    return CollectorConfig(
        websocket_url(base_url),
        output_path,
        parse_duration(duration),
        file_path,
        selected_env,
    )


def read_token(config: CollectorConfig, environ: Mapping[str, str] | None = None) -> str:
    """Read one cached credential without printing or returning its source value."""

    if config.token_file is not None:
        try:
            token = config.token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError("unable to read token file") from exc
    else:
        source = os.environ if environ is None else environ
        token = source.get(config.token_env or DEFAULT_TOKEN_ENV, "").strip()
    if not token:
        raise ValueError("cached Home Assistant token is empty or unavailable")
    return token


def is_relevant_entity(entity_id: object) -> bool:
    """Return whether an HA entity belongs in battery evidence."""

    return isinstance(entity_id, str) and (
        entity_id in EXACT_ENTITIES
        or any(entity_id.startswith(prefix) for prefix in RELEVANT_PREFIXES)
    )


def filter_states(states: object) -> list[Mapping[str, Any]]:
    """Keep raw relevant HA state dictionaries in server order."""

    if not isinstance(states, list):
        return []
    return [
        state
        for state in states
        if isinstance(state, Mapping) and is_relevant_entity(state.get("entity_id"))
    ]


def _strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield from (item.strip() for item in value.split(",") if item.strip())
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _strings(item)


def is_relevant_service_event(event: object) -> bool:
    """Keep only call_service events whose payload targets a relevant entity."""

    if not isinstance(event, Mapping) or event.get("event_type") != "call_service":
        return False
    data = event.get("data")
    return isinstance(data, Mapping) and any(
        is_relevant_entity(candidate) for candidate in _strings(data)
    )


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def redact(value: object, *, secrets: Iterable[str] = ()) -> object:
    """Convert to JSON-safe values while recursively removing credentials."""

    secret_values = tuple(secret for secret in secrets if secret)
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized = key.lower().replace("-", "_")
            if any(part in normalized for part in _SENSITIVE_KEY_PARTS):
                result[key] = _REDACTED
            else:
                result[key] = redact(item, secrets=secret_values)
        return result
    if isinstance(value, (list, tuple, set)):
        return [redact(item, secrets=secret_values) for item in value]
    if isinstance(value, datetime):
        return _utc_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    for secret in secret_values:
        text = text.replace(secret, _REDACTED)
    return text


def serialize_record(record: Mapping[str, Any], *, secrets: Iterable[str] = ()) -> bytes:
    """Serialize one redacted JSONL record deterministically."""

    safe = redact(record, secrets=secrets)
    return (
        json.dumps(safe, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


class JsonlWriter:
    """One restrictive append-only JSONL artifact."""

    def __init__(self, path: Path, *, secrets: Iterable[str] = ()) -> None:
        self.path = path
        self.secrets = tuple(secrets)
        self._fd: int | None = None

    def __enter__(self) -> "JsonlWriter":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        self._fd = os.open(self.path, flags, 0o600)
        os.fchmod(self._fd, 0o600)
        return self

    def __exit__(self, *_args: object) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def append(self, kind: str, payload: Mapping[str, Any] | None = None) -> None:
        if self._fd is None:
            raise RuntimeError("JSONL writer is not open")
        record: dict[str, object] = {
            "kind": kind,
            "recorded_at": _utc_text(datetime.now(UTC)),
        }
        if payload:
            record.update(payload)
        remaining = memoryview(serialize_record(record, secrets=self.secrets))
        while remaining:
            written = os.write(self._fd, remaining)
            if written <= 0:
                raise OSError("unable to append JSONL record")
            remaining = remaining[written:]


class EvidenceCollector:
    """Read-only HA WebSocket collector with reconnect and fixed snapshots."""

    def __init__(self, config: CollectorConfig, token: str, writer: JsonlWriter) -> None:
        self.config = config
        self.token = token
        self.writer = writer
        self._message_id = 0
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    def _next_id(self) -> int:
        self._message_id += 1
        return self._message_id

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.config.duration_seconds
        attempt = 0
        terminal_reason = "duration_complete"
        self.writer.append(
            "collector_started",
            {
                "duration_seconds": self.config.duration_seconds,
                "snapshot_interval_seconds": SNAPSHOT_INTERVAL_SECONDS,
                "websocket_url": self.config.websocket_url,
            },
        )
        try:
            async with ClientSession() as session:
                while not self._stop.is_set() and loop.time() < deadline:
                    remaining = deadline - loop.time()
                    try:
                        async with asyncio.timeout(remaining):
                            await self._collect_connection(session, deadline, attempt + 1)
                        if self._stop.is_set() or loop.time() >= deadline:
                            break
                        raise ConnectionError("Home Assistant WebSocket closed")
                    except AuthenticationError:
                        terminal_reason = "authentication_failed"
                        self.writer.append("authentication_failed")
                        raise
                    except asyncio.TimeoutError as exc:
                        if loop.time() >= deadline:
                            break
                        attempt += 1
                        await self._record_connection_loss(exc, attempt, deadline)
                    except asyncio.CancelledError:
                        terminal_reason = "cancelled"
                        raise
                    except Exception as exc:  # network/protocol failures are evidence
                        attempt += 1
                        await self._record_connection_loss(exc, attempt, deadline)
        finally:
            if self._stop.is_set() and terminal_reason == "duration_complete":
                terminal_reason = "stop_requested"
            self.writer.append("collector_stopped", {"reason": terminal_reason})

    async def _record_connection_loss(
        self,
        exc: BaseException,
        attempt: int,
        deadline: float,
    ) -> None:
        self.writer.append(
            "connection_lost",
            {"attempt": attempt, "error": f"{type(exc).__name__}: {exc}"},
        )
        delay = min(2 ** min(attempt - 1, 5), MAXIMUM_RECONNECT_DELAY_SECONDS)
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=min(delay, remaining))
        except asyncio.TimeoutError:
            pass

    async def _collect_connection(
        self,
        session: ClientSession,
        deadline: float,
        attempt: int,
    ) -> None:
        async with session.ws_connect(
            self.config.websocket_url,
            heartbeat=30,
            max_msg_size=MAXIMUM_MESSAGE_BYTES,
        ) as websocket:
            await self._authenticate(websocket)
            subscriptions = {
                self._next_id(): "state_changed",
                self._next_id(): "call_service",
            }
            for message_id, event_type in subscriptions.items():
                await websocket.send_json(
                    {"id": message_id, "type": "subscribe_events", "event_type": event_type}
                )
            self.writer.append("connection_established", {"attempt": attempt})
            next_snapshot = asyncio.get_running_loop().time()
            snapshot_id: int | None = None
            snapshot_deadline: float | None = None

            while not self._stop.is_set():
                now = asyncio.get_running_loop().time()
                if now >= deadline:
                    return
                if snapshot_deadline is not None and now >= snapshot_deadline:
                    raise asyncio.TimeoutError(
                        "Home Assistant get_states response exceeded deadline"
                    )
                if snapshot_id is None and now >= next_snapshot:
                    snapshot_id = self._next_id()
                    await websocket.send_json({"id": snapshot_id, "type": "get_states"})
                    next_snapshot = now + SNAPSHOT_INTERVAL_SECONDS
                    snapshot_deadline = now + SNAPSHOT_RESPONSE_TIMEOUT_SECONDS

                next_wakeup = (
                    next_snapshot
                    if snapshot_deadline is None
                    else snapshot_deadline
                )
                timeout = min(deadline - now, max(0.05, next_wakeup - now), 1.0)
                try:
                    message = await websocket.receive(timeout=timeout)
                except asyncio.TimeoutError:
                    continue
                if message.type == WSMsgType.TEXT:
                    payload = json.loads(message.data)
                elif message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                    raise ConnectionError("Home Assistant WebSocket disconnected")
                else:
                    continue

                message_type = payload.get("type") if isinstance(payload, Mapping) else None
                if message_type == "result":
                    message_id = payload.get("id")
                    if payload.get("success") is not True:
                        raise RuntimeError(f"Home Assistant command {message_id} failed")
                    if message_id == snapshot_id:
                        self.writer.append(
                            "snapshot",
                            {"states": filter_states(payload.get("result"))},
                        )
                        snapshot_id = None
                        snapshot_deadline = None
                    continue
                if message_type != "event":
                    continue
                event = payload.get("event")
                if not isinstance(event, Mapping):
                    continue
                event_type = event.get("event_type")
                if event_type == "state_changed":
                    data = event.get("data")
                    if isinstance(data, Mapping) and is_relevant_entity(data.get("entity_id")):
                        self.writer.append("state_changed", {"event": event})
                elif is_relevant_service_event(event):
                    self.writer.append("call_service", {"event": event})

    async def _authenticate(self, websocket: Any) -> None:
        required = await websocket.receive_json()
        if not isinstance(required, Mapping) or required.get("type") != "auth_required":
            raise RuntimeError("unexpected Home Assistant authentication handshake")
        await websocket.send_json({"type": "auth", "access_token": self.token})
        result = await websocket.receive_json()
        if not isinstance(result, Mapping) or result.get("type") != "auth_ok":
            raise AuthenticationError("Home Assistant authentication was rejected")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Home Assistant http(s) base URL")
    parser.add_argument("--output", required=True, help="append-only JSONL output path")
    parser.add_argument("--duration", required=True, help="positive duration, e.g. 30m or 24h")
    token = parser.add_mutually_exclusive_group()
    token.add_argument("--token-file", help="path containing a cached HA token")
    token.add_argument(
        "--token-env",
        help=f"environment variable containing a cached HA token (default: {DEFAULT_TOKEN_ENV})",
    )
    return parser


async def _run(config: CollectorConfig, token: str) -> None:
    with JsonlWriter(config.output, secrets=(token,)) as writer:
        collector = EvidenceCollector(config, token, writer)
        loop = asyncio.get_running_loop()
        for name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, name, None)
            if sig is not None:
                try:
                    loop.add_signal_handler(sig, collector.request_stop)
                except (NotImplementedError, RuntimeError):
                    pass
        await collector.run()


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        config = build_config(
            base_url=args.base_url,
            output=args.output,
            duration=args.duration,
            token_file=args.token_file,
            token_env=args.token_env,
        )
        token = read_token(config)
        asyncio.run(_run(config, token))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        safe = redact(f"{type(exc).__name__}: {exc}", secrets=(locals().get("token", ""),))
        print(f"collector failed: {safe}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
