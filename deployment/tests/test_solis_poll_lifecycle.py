"""Focused lifecycle smoke tests for the Solis polling service.

The upstream repository does not provide a Home Assistant test environment, so
these tests install only the import shims needed by service.py and run with the
standard library.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest


def _module(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


root = Path(__file__).parents[1]
overlay = root / "files" / "custom_component_overlays" / "solis-v4.0.1-poll-recovery"
solis_overlay = _module("solis_overlay")
solis_overlay.__path__ = [str(overlay)]

control_const = _module("solis_overlay.control_const")
control_const.ALL_CONTROLS = {}
control_const.CONTROL_TYPES = {}

ginlong_base = _module("solis_overlay.ginlong_base")
ginlong_base.BaseAPI = type("BaseAPI", (), {})
ginlong_base.GinlongData = type("GinlongData", (), {})
ginlong_base.PortalConfig = type("PortalConfig", (), {})

soliscloud_api = _module("solis_overlay.soliscloud_api")
soliscloud_api.SoliscloudAPI = type("SoliscloudAPI", (), {})
soliscloud_api.SoliscloudConfig = type("SoliscloudConfig", (), {})

soliscloud_const = _module("solis_overlay.soliscloud_const")
soliscloud_const.INVERTER_ACPOWER = "ac_power"
soliscloud_const.INVERTER_ENERGY_TODAY = "energy_today"
soliscloud_const.INVERTER_SERIAL = "serial"
soliscloud_const.INVERTER_STATE = "state"
soliscloud_const.INVERTER_TIMESTAMP_UPDATE = "timestamp"

spec = importlib.util.spec_from_file_location("solis_overlay.service", overlay / "service.py")
assert spec is not None and spec.loader is not None
service = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = service
spec.loader.exec_module(service)


class FakeAPI:
    def __init__(self) -> None:
        self.is_online = True
        self.inverters = {"SN": "id"}
        self.logout_count = 0
        self.fetch = None

    async def fetch_inverter_data(self, serial):
        return await self.fetch(serial)

    async def logout(self) -> None:
        self.logout_count += 1
        self.is_online = False
        self.inverters = None


def make_service() -> service.InverterService:
    inverter_service = object.__new__(service.InverterService)
    inverter_service._api = FakeAPI()
    inverter_service._hass = object()
    inverter_service._schedule_ok = 300
    inverter_service._schedule_nok = 1
    inverter_service._last_updated = None
    inverter_service._logintime = None
    inverter_service._subscriptions = {}
    inverter_service._unsub_update = None
    inverter_service._shutdown = False
    return inverter_service


class TestPollLifecycle:
    def setup_method(self) -> None:
        self.scheduled = []

        def track(hass, callback, when):
            handle = {"callback": callback, "when": when, "cancelled": 0}
            self.scheduled.append(handle)

            def cancel():
                handle["cancelled"] += 1

            return cancel

        self.original_track = service.async_track_point_in_utc_time
        self.original_timeout = service.UPDATE_TIMEOUT_SECONDS
        service.async_track_point_in_utc_time = track

    def teardown_method(self) -> None:
        service.async_track_point_in_utc_time = self.original_track
        service.UPDATE_TIMEOUT_SECONDS = self.original_timeout

    @pytest.mark.asyncio
    async def test_reschedule_replaces_timer_and_shutdown_cancels_it(self) -> None:
        inverter_service = make_service()

        inverter_service.schedule_update(service.timedelta(seconds=1))
        inverter_service.schedule_update(service.timedelta(seconds=2))

        assert len(self.scheduled) == 2
        assert self.scheduled[0]["cancelled"] == 1
        assert self.scheduled[1]["cancelled"] == 0

        await inverter_service.shutdown()
        assert self.scheduled[1]["cancelled"] == 1

        inverter_service.schedule_update(service.timedelta(seconds=3))
        assert len(self.scheduled) == 2

    @pytest.mark.asyncio
    async def test_timeout_logs_out_and_reschedules(self) -> None:
        inverter_service = make_service()
        service.UPDATE_TIMEOUT_SECONDS = 0.01

        async def hang(serial):
            await asyncio.sleep(1)

        inverter_service._api.fetch = hang
        await inverter_service.async_update()

        assert inverter_service._api.logout_count == 1
        assert len(self.scheduled) == 1

    @pytest.mark.asyncio
    async def test_fired_timer_is_replaced_by_one_new_timer(self) -> None:
        inverter_service = make_service()

        async def no_data(serial):
            return None

        inverter_service._api.fetch = no_data
        inverter_service.schedule_update(service.timedelta(seconds=1))
        await self.scheduled[0]["callback"](datetime.now(timezone.utc))

        assert len(self.scheduled) == 2
        assert self.scheduled[0]["cancelled"] == 0
        assert self.scheduled[1]["cancelled"] == 0

    @pytest.mark.asyncio
    async def test_shutdown_during_update_does_not_reschedule(self) -> None:
        inverter_service = make_service()
        started = asyncio.Event()
        release = asyncio.Event()

        async def fetch(serial):
            started.set()
            await release.wait()
            return None

        inverter_service._api.fetch = fetch
        update = asyncio.create_task(inverter_service.async_update())
        await started.wait()
        await inverter_service.shutdown()
        release.set()
        await update

        assert self.scheduled == []

    @pytest.mark.asyncio
    async def test_missing_inverter_list_still_reschedules(self) -> None:
        inverter_service = make_service()
        inverter_service._api.inverters = None

        await inverter_service.async_update()

        assert len(self.scheduled) == 1
