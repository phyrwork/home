"""Serialized and revision-verified Home Assistant write adapter.

The core writer uses no Home Assistant package.  ``HomeAssistantEventAdapter``
is an optional concrete bridge for real HA state events; the duck-typed boundary
also makes all failure and race paths deterministically testable offline.
"""

import asyncio
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta, tzinfo
from decimal import Decimal, InvalidOperation
import inspect
from typing import Awaitable, Protocol

from .contracts import ObservedCapability
from .write_contracts import (
    StatePrecondition,
    TransactionResult,
    TransactionStatus,
    WriteOutcome,
    WriteRequest,
    WriteResult,
)


HA_SERVICE_CALL_TIMEOUT = timedelta(seconds=10)
HA_READBACK_TIMEOUT = timedelta(seconds=15)
HA_LISTENER_CLEANUP_TIMEOUT = timedelta(seconds=5)
_UNKNOWN = frozenset(("unknown", "unavailable"))


class HomeAssistantAccess(Protocol):
    """The minimal state/service/event surface consumed by the writer."""

    def get_state(self, entity_id: str) -> object: ...

    def async_call(
        self, domain: str, service: str, service_data: Mapping[str, object], *, blocking: bool = True
    ) -> Awaitable[object]: ...

    def async_listen_state_change(self, entity_id: str, callback: Callable[..., object]) -> Callable[[], object]: ...


class HomeAssistantEventAdapter:
    """Concrete adapter for a real HA instance and its state-event helper."""

    def __init__(self, hass: object) -> None:
        self.hass = hass
        from homeassistant.helpers.event import async_track_state_change_event

        self._track_state_change_event = async_track_state_change_event

    def get_state(self, entity_id: str) -> object:
        return self.hass.states.get(entity_id)  # type: ignore[attr-defined]

    def async_call(self, domain: str, service: str, service_data: Mapping[str, object], *, blocking: bool = True) -> Awaitable[object]:
        return self.hass.services.async_call(  # type: ignore[attr-defined]
            domain, service, service_data, blocking=blocking
        )

    def async_listen_state_change(self, entity_id: str, callback: Callable[..., object]) -> Callable[[], object]:
        return self._track_state_change_event(self.hass, [entity_id], callback)


def _field(state: object, name: str, default: object = None) -> object:
    if isinstance(state, Mapping):
        return state.get(name, default)
    return getattr(state, name, default)


def _attributes(state: object) -> Mapping[str, object]:
    value = _field(state, "attributes", {})
    return value if isinstance(value, Mapping) else {}


def _state_value(state: object) -> str | None:
    value = _field(state, "state")
    return value if isinstance(value, str) else None


def _updated(state: object) -> datetime | None:
    value = _field(state, "last_updated")
    if isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None:
        return value
    return None


def _context_id(state: object) -> str | None:
    context = _field(state, "context")
    value = _field(context, "id") if context is not None else None
    if value is None and isinstance(state, Mapping):
        value = state.get("context_id")
    return value if isinstance(value, str) else None


def _parse_decimal(value: object) -> Decimal | None:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _domain(entity_id: str) -> str | None:
    return entity_id.split(".", 1)[0] if "." in entity_id else None


class HomeAssistantWriter:
    """One serialized writer boundary for all supported HA entity domains."""

    def __init__(
        self,
        hass: HomeAssistantAccess | object,
        *,
        adapter: HomeAssistantAccess | None = None,
        timezone: tzinfo | None = None,
    ) -> None:
        if adapter is None and hasattr(hass, "states") and hasattr(hass, "services"):
            raise ValueError("real Home Assistant instances require HomeAssistantWriter.for_home_assistant()")
        self.hass = adapter if adapter is not None else hass
        # Datetime targets are already schedule-normalized by the caller.  If
        # configured, this timezone is validation only; the writer never
        # converts or adjusts a target's timezone.
        self.timezone = timezone
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[object] | None = None

    @classmethod
    def for_home_assistant(cls, hass: object, *, timezone: tzinfo | None = None) -> "HomeAssistantWriter":
        """Construct a production writer with the real HA event adapter."""
        return cls(hass, adapter=HomeAssistantEventAdapter(hass), timezone=timezone)

    def transaction(self) -> "WriteTransaction":
        return WriteTransaction(self)

    async def async_write(self, request: WriteRequest) -> WriteResult:
        """Write one request, acquiring the writer lock unless already owned."""
        if self._owner is asyncio.current_task():
            raise RuntimeError("writer.async_write cannot be re-entered inside a transaction; use transaction.async_write")
        async with self.transaction() as transaction:
            return await transaction.async_write(request)

    write = async_write

    def capture_precondition(self, entity_id: str) -> StatePrecondition:
        """Capture an immutable current HA revision for a later write."""
        state = self._get(entity_id)
        if state is None:
            raise ValueError(f"entity does not exist: {entity_id}")
        raw = _state_value(state)
        updated = _updated(state)
        if raw is None or raw in _UNKNOWN or updated is None:
            raise ValueError(f"entity state is unavailable or invalid: {entity_id}")
        return StatePrecondition(entity_id, raw, updated, _context_id(state))

    current_precondition = capture_precondition
    read_precondition = capture_precondition

    def _get(self, entity_id: str) -> object:
        getter = getattr(self.hass, "get_state", None)
        if callable(getter):
            return getter(entity_id)
        getter = getattr(self.hass, "get", None)
        if callable(getter):
            return getter(entity_id)
        states = getattr(self.hass, "states", None)
        if isinstance(states, Mapping):
            return states.get(entity_id)
        getter = getattr(states, "get", None)
        if callable(getter):
            return getter(entity_id)
        raise TypeError("Home Assistant access must provide get_state or get")

    async def _call_service(self, request: WriteRequest, service: str, data: dict[str, object]) -> None:
        call = getattr(self.hass, "async_call", None)
        if not callable(call):
            call = getattr(self.hass, "call_service", None)
        if not callable(call):
            services = getattr(self.hass, "services", None)
            call = getattr(services, "async_call", None)
        if not callable(call):
            raise TypeError("Home Assistant access must provide async_call")
        result = call(request.domain, service, data, blocking=True)
        if not inspect.isawaitable(result):
            raise TypeError("Home Assistant service call must return an awaitable")
        await asyncio.wait_for(result, HA_SERVICE_CALL_TIMEOUT.total_seconds())

    def _listen(self, entity_id: str, callback: Callable[..., object]) -> Callable[[], object]:
        listen = getattr(self.hass, "async_listen_state_change", None)
        if not callable(listen):
            listen = getattr(self.hass, "listen_state_change", None)
        if not callable(listen):
            listen = getattr(self.hass, "async_track_state_change_event", None)
        if not callable(listen):
            # Immediate readback can still be proven without an event surface;
            # delayed readback will deterministically become READBACK_TIMEOUT.
            return lambda: None
        remove = listen(entity_id, callback)
        if not callable(remove):
            raise TypeError("state listener must return a removal callable")
        return remove

    def _precondition(self, request: WriteRequest, state: object) -> WriteResult | None:
        expected = request.precondition
        if state is None:
            return WriteResult(request.entity_id, WriteOutcome.REJECTED, "entity does not exist")
        raw = _state_value(state)
        updated = _updated(state)
        if raw is None or raw in _UNKNOWN or updated is None:
            return WriteResult(request.entity_id, WriteOutcome.REJECTED, "entity state is unavailable or invalid")
        if _domain(request.entity_id) != request.domain:
            return WriteResult(request.entity_id, WriteOutcome.REJECTED, "entity domain does not match request")
        if (
            raw != expected.state
            or updated != expected.last_updated
            or _context_id(state) != expected.context_id
        ):
            return WriteResult(request.entity_id, WriteOutcome.CONFLICT, "state precondition no longer matches")
        return None

    def _metadata(self, state: object) -> ObservedCapability | None:
        attrs = _attributes(state)
        current = _parse_decimal(_state_value(state))
        minimum = _parse_decimal(attrs.get("min", attrs.get("minimum")))
        maximum = _parse_decimal(attrs.get("max", attrs.get("maximum")))
        step = _parse_decimal(attrs.get("step"))
        unit = attrs.get("unit_of_measurement")
        if current is None or minimum is None or maximum is None or step is None or not isinstance(unit, str):
            return None
        try:
            return ObservedCapability(current, minimum, maximum, step, unit)
        except (TypeError, ValueError):
            return None

    def _validate_target(self, request: WriteRequest, state: object) -> tuple[str, str, dict[str, object]] | WriteResult:
        target = request.target
        if request.domain == "switch":
            if not isinstance(target, bool):
                return WriteResult(request.entity_id, WriteOutcome.REJECTED, "switch target must be bool")
            normalized = "on" if target else "off"
            return normalized, "turn_on" if target else "turn_off", {"entity_id": request.entity_id}
        if request.domain == "select":
            if not isinstance(target, str):
                return WriteResult(request.entity_id, WriteOutcome.REJECTED, "select target must be string")
            options = _attributes(state).get("options")
            if not isinstance(options, (list, tuple)) or target not in options:
                return WriteResult(request.entity_id, WriteOutcome.REJECTED, "select target is not advertised")
            return target, "select_option", {"entity_id": request.entity_id, "option": target}
        if request.domain == "text":
            if not isinstance(target, str):
                return WriteResult(request.entity_id, WriteOutcome.REJECTED, "text target must be string")
            if request.text_validator is not None:
                try:
                    accepted = request.text_validator(target)
                except Exception as exc:
                    return WriteResult(request.entity_id, WriteOutcome.REJECTED, f"text validation failed: {exc}")
                if accepted is not True:
                    return WriteResult(request.entity_id, WriteOutcome.REJECTED, "text validator rejected target")
            return target, "set_value", {"entity_id": request.entity_id, "value": target}
        if request.domain == "datetime":
            if not isinstance(target, datetime) or target.tzinfo is None or target.utcoffset() is None:
                return WriteResult(request.entity_id, WriteOutcome.REJECTED, "datetime target must be aware")
            if self.timezone is not None and target.tzinfo != self.timezone:
                return WriteResult(
                    request.entity_id,
                    WriteOutcome.REJECTED,
                    "datetime target must already be normalized to the configured timezone",
                )
            normalized = target.isoformat()
            return normalized, "set_value", {"entity_id": request.entity_id, "datetime": normalized}
        if request.domain == "number":
            if not isinstance(target, Decimal) or not target.is_finite():
                return WriteResult(request.entity_id, WriteOutcome.REJECTED, "number target must be finite Decimal")
            live = self._metadata(state)
            observed = request.capability
            if live is None or observed is None:
                return WriteResult(request.entity_id, WriteOutcome.REJECTED, "number capability metadata is invalid")
            if live != observed:
                return WriteResult(request.entity_id, WriteOutcome.CONFLICT, "number capability metadata drifted")
            if not live.minimum <= target <= live.maximum:
                return WriteResult(request.entity_id, WriteOutcome.REJECTED, "number target is outside capability bounds")
            if (target - live.minimum) % live.step != 0:
                return WriteResult(request.entity_id, WriteOutcome.REJECTED, "number target is not aligned to capability step")
            return str(target), "set_value", {"entity_id": request.entity_id, "value": target}
        return WriteResult(request.entity_id, WriteOutcome.REJECTED, "unsupported domain")

    @staticmethod
    def _matches(request: WriteRequest, state: object, normalized: str) -> bool:
        raw = _state_value(state)
        if request.domain == "number":
            left = _parse_decimal(raw)
            right = _parse_decimal(normalized)
            return left is not None and right is not None and left == right
        if request.domain == "datetime":
            try:
                left = datetime.fromisoformat(raw or "")
            except ValueError:
                return False
            target = request.target
            return isinstance(target, datetime) and left.tzinfo is not None and left == target
        return raw == normalized

    @staticmethod
    def _new_revision(state: object, precondition: StatePrecondition) -> bool:
        updated = _updated(state)
        return (
            updated is not None and updated > precondition.last_updated
        ) or _context_id(state) != precondition.context_id

    async def _write_locked(self, request: WriteRequest) -> WriteResult:
        state = self._get(request.entity_id)
        rejected = self._precondition(request, state)
        if rejected is not None:
            return rejected
        validated = self._validate_target(request, state)
        if isinstance(validated, WriteResult):
            return validated
        normalized, service, service_data = validated
        if self._matches(request, state, normalized):
            return WriteResult(request.entity_id, WriteOutcome.NO_CHANGE, "Home Assistant already has target", service, service_data)

        event = asyncio.Event()
        loop = asyncio.get_running_loop()

        def on_change(*args: object, **kwargs: object) -> None:
            loop.call_soon_threadsafe(event.set)

        remove: Callable[[], object] | None = None
        try:
            # Arm before the final CAS check so a fast state transition cannot be missed.
            remove = self._listen(request.entity_id, on_change)
            latest = self._get(request.entity_id)
            rejected = self._precondition(request, latest)
            if rejected is not None:
                return rejected
            validated = self._validate_target(request, latest)
            if isinstance(validated, WriteResult):
                return validated
            normalized, service, service_data = validated
            if self._matches(request, latest, normalized):
                return WriteResult(request.entity_id, WriteOutcome.NO_CHANGE, "Home Assistant already has target", service, service_data)
            try:
                await self._call_service(request, service, service_data)
            except asyncio.TimeoutError:
                return WriteResult(request.entity_id, WriteOutcome.SERVICE_TIMEOUT, "Home Assistant service call timed out", f"{request.domain}.{service}", service_data)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return WriteResult(request.entity_id, WriteOutcome.SERVICE_ERROR, f"Home Assistant service call failed: {exc}", f"{request.domain}.{service}", service_data)

            after = self._get(request.entity_id)
            if self._matches(request, after, normalized) and self._new_revision(after, request.precondition):
                return WriteResult(request.entity_id, WriteOutcome.APPLIED_HA_READBACK, "Home Assistant state revision confirms the requested value", f"{request.domain}.{service}", service_data)
            deadline = asyncio.get_running_loop().time() + HA_READBACK_TIMEOUT.total_seconds()
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return WriteResult(request.entity_id, WriteOutcome.READBACK_TIMEOUT, "Home Assistant did not confirm a new matching state revision", f"{request.domain}.{service}", service_data)
                try:
                    await asyncio.wait_for(event.wait(), remaining)
                except asyncio.TimeoutError:
                    return WriteResult(request.entity_id, WriteOutcome.READBACK_TIMEOUT, "Home Assistant did not confirm a new matching state revision", f"{request.domain}.{service}", service_data)
                event.clear()
                after = self._get(request.entity_id)
                if self._matches(request, after, normalized) and self._new_revision(after, request.precondition):
                    return WriteResult(request.entity_id, WriteOutcome.APPLIED_HA_READBACK, "Home Assistant state event and revision confirm the requested value", f"{request.domain}.{service}", service_data)
        finally:
            if remove is not None:
                await _cleanup_listener(remove)


async def _cleanup_listener(remove: Callable[[], object]) -> None:
    """Remove a listener through a bounded shield, preserving cancellation."""

    async def invoke() -> None:
        result = remove()
        if inspect.isawaitable(result):
            await result

    cleanup_task = asyncio.create_task(invoke())

    def consume(task: asyncio.Task[object]) -> None:
        try:
            task.result()
        except BaseException:
            pass

    cleanup_task.add_done_callback(consume)
    cancellation: asyncio.CancelledError | None = None
    try:
        await asyncio.wait_for(asyncio.shield(cleanup_task), HA_LISTENER_CLEANUP_TIMEOUT.total_seconds())
    except asyncio.CancelledError as exc:
        cancellation = exc
        current = asyncio.current_task()
        if current is not None:
            current.uncancel()
        try:
            await asyncio.wait_for(asyncio.shield(cleanup_task), HA_LISTENER_CLEANUP_TIMEOUT.total_seconds())
        except asyncio.TimeoutError:
            cleanup_task.cancel()
    except asyncio.TimeoutError:
        cleanup_task.cancel()
    except Exception:
        # Listener removal is cleanup; preserve the primary write outcome.
        pass
    finally:
        if cancellation is not None:
            raise cancellation


class WriteTransaction:
    """Async context holding one writer lock across ordered requests."""

    def __init__(self, writer: HomeAssistantWriter) -> None:
        self.writer = writer
        self.results: list[WriteResult] = []
        self.complete = True
        self._entered = False
        self._closed = False
        self._task: asyncio.Task[object] | None = None

    async def __aenter__(self) -> "WriteTransaction":
        if self._entered:
            raise RuntimeError("transaction is already active and cannot be re-entered")
        if self._closed:
            raise RuntimeError("transaction is closed and cannot be re-entered")
        if self.writer._owner is asyncio.current_task():
            raise RuntimeError("nested transactions are not supported")
        await self.writer._lock.acquire()
        self.writer._owner = asyncio.current_task()
        self._entered = True
        self._task = asyncio.current_task()
        return self

    def _assert_owner(self) -> None:
        if not self._entered or self._task is not asyncio.current_task():
            raise RuntimeError("transaction may only be used by its owning asyncio task")

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        self._assert_owner()
        if isinstance(exc, asyncio.CancelledError):
            self.complete = False
        self.writer._owner = None
        self.writer._lock.release()
        self._entered = False
        self._closed = True
        self._task = None
        return False

    async def async_write(self, request: WriteRequest) -> WriteResult:
        self._assert_owner()
        try:
            result = await self.writer._write_locked(request)
        except asyncio.CancelledError:
            self.complete = False
            raise
        self.results.append(result)
        return result

    write = async_write

    async def execute(
        self,
        requests: Iterable[WriteRequest],
        *,
        continue_on_failure: bool = False,
    ) -> TransactionResult:
        self._assert_owner()
        for request in requests:
            result = await self.async_write(request)
            if not result.success and not continue_on_failure:
                break
        return self.result()

    def result(self) -> TransactionResult:
        failures = [result for result in self.results if not result.success]
        successes = [result for result in self.results if result.success]
        if not failures:
            status = TransactionStatus.SUCCESS if self.complete else (TransactionStatus.PARTIAL_FAILURE if successes else TransactionStatus.FAILED)
        elif successes:
            status = TransactionStatus.PARTIAL_FAILURE
        else:
            status = TransactionStatus.FAILED
        return TransactionResult(tuple(self.results), status, self.complete)

__all__ = [
    "HA_LISTENER_CLEANUP_TIMEOUT",
    "HA_READBACK_TIMEOUT",
    "HA_SERVICE_CALL_TIMEOUT",
    "HomeAssistantAccess",
    "HomeAssistantEventAdapter",
    "HomeAssistantWriter",
    "WriteTransaction",
]
