"""Control a house battery using tariff and energy forecasts."""

import asyncio
from typing import Any

import voluptuous as vol
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import CoreState, Event, HomeAssistant
try:
    from homeassistant.core import SupportsResponse
except ImportError:  # pragma: no cover - compatibility with older HA test stubs
    SupportsResponse = None  # type: ignore[assignment,misc]
from homeassistant.helpers.discovery import async_load_platform

from .config import Config, from_mapping
from .const import DOMAIN
from .coordinator import Coordinator
from .commissioning import SERVICE_ABORT, SERVICE_BEGIN, SERVICE_VALIDATE, CommissionedEnvelopeEvidenceProvider, CommissioningWorkflow, service_schemas

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.All(dict, from_mapping),
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up House Battery Control from YAML."""
    typed_config = config.get(DOMAIN)
    if not isinstance(typed_config, Config):
        return True

    old_workflow = hass.data.get(f"{DOMAIN}.commissioning")
    old_finalizer = hass.data.get(f"{DOMAIN}.stop_finalizer")
    old_coordinator = hass.data.get(DOMAIN)
    if old_workflow is not None:
        await old_workflow.async_stop()
        if (
            getattr(old_workflow, "cleanup_pending", False)
            or getattr(old_workflow, "_cleanup_task", None) is not None
            or (
                getattr(old_workflow, "_cleanup_io_task", None) is not None
                and not old_workflow._cleanup_io_task.done()
            )
            or (
                getattr(old_workflow, "_expiry_task", None) is not None
                and not old_workflow._expiry_task.done()
            )
        ):
            # A cancellation-resistant child owns the old lifecycle until it
            # is conclusively drained; do not replace it and lose its typed
            # obligation during reload.
            if isinstance(old_finalizer, dict):
                arm = old_finalizer.get("arm")
                if callable(arm):
                    arm()
            return False
        if isinstance(old_finalizer, dict):
            await_result = old_finalizer.get("await_result")
            if callable(await_result) and not await await_result():
                return False
    else:
        for listener_key in (f"{DOMAIN}.stop_listener", f"{DOMAIN}.started_listener"):
            old_listener = hass.data.pop(listener_key, None)
            if callable(old_listener):
                old_listener()
    if old_workflow is None and old_coordinator is not None:
        stop = getattr(old_coordinator, "async_stop", None)
        if callable(stop):
            stale_state: dict[str, object] = {"task": asyncio.create_task(stop()), "result": None}
            hass.data[f"{DOMAIN}.stop_finalizer"] = stale_state
            try:
                await asyncio.shield(stale_state["task"])
                stale_state["result"] = {"ok": True}
                hass.data.pop(f"{DOMAIN}.stop_finalizer", None)
            except asyncio.CancelledError:
                stale_state["result"] = {"ok": False, "reason": "cancelled"}
                raise
            except Exception as exc:
                stale_state["result"] = {"ok": False, "reason": type(exc).__name__}
                return False

    coordinator = Coordinator(hass, typed_config)
    hass.data[DOMAIN] = coordinator
    envelope_provider = CommissionedEnvelopeEvidenceProvider()
    workflow = CommissioningWorkflow(
        hass,
        coordinator,
        enabled=typed_config.candidate_commissioning.services_enabled,
        capability_resolutions=typed_config.candidate_commissioning.capability_resolutions,
        envelope_provider=envelope_provider,
    )
    hass.data[f"{DOMAIN}.commissioning"] = workflow
    finalizer_state: dict[str, object] = {"task": None, "result": None, "watched": set(), "unsubscribed": set()}

    def _pending_owner() -> object | None:
        for name in ("_cleanup_task", "_cleanup_io_task", "_expiry_task"):
            owner = getattr(workflow, name, None)
            if owner is not None and not owner.done():
                return owner
        return None

    async def _finalize_lifecycle() -> bool:
        owner = _pending_owner()
        if owner is not None:
            finished, _ = await asyncio.wait({owner}, timeout=0.1)
            if not finished:
                finalizer_state["result"] = {"ok": False, "reason": "owner_pending"}
                return False
        try:
            for listener_key in (f"{DOMAIN}.stop_listener", f"{DOMAIN}.started_listener"):
                if listener_key not in finalizer_state["unsubscribed"]:
                    finalizer_state["unsubscribed"].add(listener_key)
                    listener = hass.data.get(listener_key)
                    if callable(listener):
                        listener()
            stop = getattr(coordinator, "async_stop", None)
            if callable(stop):
                await stop()
            if hass.data.get(f"{DOMAIN}.commissioning") is workflow:
                hass.data.pop(f"{DOMAIN}.commissioning", None)
                hass.data.pop(DOMAIN, None)
                hass.data.pop(f"{DOMAIN}.stop_finalizer", None)
                hass.data.pop(f"{DOMAIN}.started_listener", None)
                hass.data.pop(f"{DOMAIN}.stop_listener", None)
                for service in (SERVICE_BEGIN, SERVICE_VALIDATE, SERVICE_ABORT):
                    hass.services.async_remove(DOMAIN, service)
            finalizer_state["result"] = {"ok": True}
            return True
        except asyncio.CancelledError:
            finalizer_state["result"] = {"ok": False, "reason": "cancelled"}
            raise
        except Exception as exc:
            finalizer_state["result"] = {"ok": False, "reason": type(exc).__name__}
            return False

    def _start_finalizer() -> asyncio.Task[bool]:
        task = finalizer_state.get("task")
        if finalizer_state.get("result") == {"ok": True} and isinstance(task, asyncio.Task):
            return task
        if isinstance(task, asyncio.Task) and not task.done():
            return task
        owner = _pending_owner()
        if owner is not None and owner not in finalizer_state["watched"]:
            finalizer_state["watched"].add(owner)
            owner.add_done_callback(lambda _done: _start_finalizer())
        task = asyncio.create_task(_finalize_lifecycle())
        finalizer_state["task"] = task
        return task

    async def _await_finalizer() -> bool:
        return bool(await asyncio.shield(_start_finalizer()))

    finalizer_state["arm"] = _start_finalizer
    finalizer_state["await_result"] = _await_finalizer
    hass.data[f"{DOMAIN}.stop_finalizer"] = finalizer_state
    schemas = service_schemas()

    async def _begin(call: object) -> dict[str, object]:
        return await workflow.async_begin(getattr(call, "data", {}))

    async def _validate(call: object) -> dict[str, object]:
        return await workflow.async_validate(getattr(call, "data", {}))

    async def _abort(call: object) -> dict[str, object]:
        return await workflow.async_abort()

    response_kwargs = {} if SupportsResponse is None else {"supports_response": SupportsResponse.ONLY}
    for service in (SERVICE_BEGIN, SERVICE_VALIDATE, SERVICE_ABORT):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
    hass.services.async_register(DOMAIN, SERVICE_BEGIN, _begin, schema=schemas[SERVICE_BEGIN], **response_kwargs)
    hass.services.async_register(DOMAIN, SERVICE_VALIDATE, _validate, schema=schemas[SERVICE_VALIDATE], **response_kwargs)
    hass.services.async_register(DOMAIN, SERVICE_ABORT, _abort, schema=schemas[SERVICE_ABORT], **response_kwargs)
    await async_load_platform(hass, "sensor", DOMAIN, {}, config)

    async def async_start(_event: Event[dict[str, object]] | None = None) -> None:
        await workflow.async_start()
        await coordinator.async_start()

    async def async_stop(_event: Event[dict[str, object]]) -> None:
        try:
            await workflow.async_stop()
        except asyncio.CancelledError:
            _start_finalizer()
            raise
        if _pending_owner() is not None:
            _start_finalizer()
            return
        await _await_finalizer()

    stop_listener = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, async_stop)
    hass.data[f"{DOMAIN}.stop_listener"] = stop_listener
    if hass.state is CoreState.running:
        await async_start()
    else:
        hass.data[f"{DOMAIN}.started_listener"] = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, async_start)
    return True
