"""Focused Home Assistant lifecycle tests for the MVP integration."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import yaml
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CoreState, HomeAssistant

from custom_components.house_battery_control import async_setup
from custom_components.house_battery_control import config as integration_config
from custom_components.house_battery_control.const import DOMAIN
from custom_components.house_battery_control.coordinator import Coordinator


def config() -> integration_config.Config:
    path = Path(__file__).parents[3] / "house_battery_control.yaml"
    return integration_config.from_mapping(yaml.safe_load(path.read_text()))


def coordinator() -> MagicMock:
    instance = MagicMock(spec=Coordinator)
    instance.async_start = AsyncMock()
    instance.async_stop = AsyncMock()
    return instance


def strict_one_shot_remove_check(hass: HomeAssistant, monkeypatch) -> list[str]:
    """Record teardown attempts to remove an already-fired one-shot listener."""

    original_listen_once = hass.bus.async_listen_once
    removed_after_fire: list[str] = []

    def listen_once(event_bus, event_type, listener):
        fired = False

        async def tracked_listener(event):
            nonlocal fired
            fired = True
            await listener(event)

        remove = original_listen_once(event_type, tracked_listener)

        def strict_remove() -> None:
            if fired:
                removed_after_fire.append(event_type)
                return
            remove()

        return strict_remove

    monkeypatch.setattr(type(hass.bus), "async_listen_once", listen_once)
    return removed_after_fire


async def test_running_home_assistant_starts_coordinator(hass: HomeAssistant) -> None:
    instance = coordinator()
    with (
        patch("custom_components.house_battery_control.Coordinator", return_value=instance),
        patch("custom_components.house_battery_control.async_load_platform", AsyncMock()),
    ):
        assert await async_setup(hass, {DOMAIN: config()})

    assert hass.data[DOMAIN] is instance
    instance.async_start.assert_awaited_once_with()


async def test_start_is_deferred_until_home_assistant_started(hass: HomeAssistant) -> None:
    hass.set_state(CoreState.starting)
    instance = coordinator()
    with (
        patch("custom_components.house_battery_control.Coordinator", return_value=instance),
        patch("custom_components.house_battery_control.async_load_platform", AsyncMock()),
    ):
        assert await async_setup(hass, {DOMAIN: config()})

    instance.async_start.assert_not_awaited()
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    instance.async_start.assert_awaited_once_with()


async def test_started_then_stop_does_not_remove_fired_one_shot_listeners(
    hass: HomeAssistant,
    monkeypatch,
) -> None:
    hass.set_state(CoreState.starting)
    instance = coordinator()
    removed_after_fire = strict_one_shot_remove_check(hass, monkeypatch)
    with (
        patch("custom_components.house_battery_control.Coordinator", return_value=instance),
        patch("custom_components.house_battery_control.async_load_platform", AsyncMock()),
    ):
        assert await async_setup(hass, {DOMAIN: config()})

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    assert removed_after_fire == []
    instance.async_start.assert_awaited_once_with()
    instance.async_stop.assert_awaited_once_with()


async def test_running_stop_does_not_remove_fired_one_shot_listener(
    hass: HomeAssistant,
    monkeypatch,
) -> None:
    instance = coordinator()
    removed_after_fire = strict_one_shot_remove_check(hass, monkeypatch)
    with (
        patch("custom_components.house_battery_control.Coordinator", return_value=instance),
        patch("custom_components.house_battery_control.async_load_platform", AsyncMock()),
    ):
        assert await async_setup(hass, {DOMAIN: config()})

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    assert removed_after_fire == []
    instance.async_stop.assert_awaited_once_with()


async def test_stop_unsubscribes_and_removes_coordinator(hass: HomeAssistant) -> None:
    instance = coordinator()
    with (
        patch("custom_components.house_battery_control.Coordinator", return_value=instance),
        patch("custom_components.house_battery_control.async_load_platform", AsyncMock()),
    ):
        assert await async_setup(hass, {DOMAIN: config()})

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    instance.async_stop.assert_awaited_once_with()
    assert DOMAIN not in hass.data


async def test_reload_stops_previous_instance_before_replacing_it(hass: HomeAssistant) -> None:
    previous = coordinator()
    current = coordinator()
    with (
        patch(
            "custom_components.house_battery_control.Coordinator",
            side_effect=(previous, current),
        ),
        patch("custom_components.house_battery_control.async_load_platform", AsyncMock()),
    ):
        await async_setup(hass, {DOMAIN: config()})
        await async_setup(hass, {DOMAIN: config()})

    previous.async_stop.assert_awaited_once_with()
    assert hass.data[DOMAIN] is current


async def test_concurrent_stop_is_idempotent(hass: HomeAssistant) -> None:
    instance = coordinator()
    release = asyncio.Event()

    async def delayed_stop() -> None:
        await release.wait()

    instance.async_stop.side_effect = delayed_stop
    with (
        patch("custom_components.house_battery_control.Coordinator", return_value=instance),
        patch("custom_components.house_battery_control.async_load_platform", AsyncMock()),
    ):
        await async_setup(hass, {DOMAIN: config()})

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await asyncio.sleep(0)
    release.set()
    await hass.async_block_till_done()
    assert instance.async_stop.await_count == 1
