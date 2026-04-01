from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ACTIVE_POWER_THRESHOLD,
    CONF_BASELINE_RATE_SENSOR,
    CONF_CURRENT_RATE_SENSOR,
    CONF_ENERGY_SENSOR,
    CONF_POWER_SENSOR,
    DEFAULT_ACTIVE_POWER_THRESHOLD,
    DOMAIN,
    PERSISTED_STATE_KEYS,
    STATE_BASELINE_RATE,
    STATE_CURRENT_RATE,
    STATE_CURRENT_SAVINGS_RATE,
    STATE_LAST_TOTAL_ENERGY,
    STATE_LAST_UPDATED_TIME,
    STATE_TOTAL_ENERGY,
    STATE_TOTAL_SAVINGS,
)
from .helpers import integrate_left, parse_float, savings_rate


class EnergyCostSavingTrackerCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, logger=logging.getLogger(__name__), name=DOMAIN)
        self.entry = entry
        self._store = Store[dict[str, Any]](hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self._unsub = None
        self._last_power_watts: float | None = None
        self._last_power_timestamp: datetime | None = None
        self.data = {
            STATE_CURRENT_RATE: None,
            STATE_BASELINE_RATE: None,
            STATE_CURRENT_SAVINGS_RATE: None,
            STATE_TOTAL_ENERGY: 0.0,
            STATE_TOTAL_SAVINGS: 0.0,
            STATE_LAST_TOTAL_ENERGY: None,
            STATE_LAST_UPDATED_TIME: None,
        }

    async def async_initialize(self) -> None:
        stored = await self._store.async_load() or {}
        for key in PERSISTED_STATE_KEYS:
            if key in stored:
                self.data[key] = stored[key]

        await self._async_seed_from_sources()

        watch_entities = [
            self.entry.data.get(CONF_CURRENT_RATE_SENSOR),
            self.entry.data.get(CONF_BASELINE_RATE_SENSOR),
            self.entry.data.get(CONF_ENERGY_SENSOR),
            self.entry.data.get(CONF_POWER_SENSOR),
        ]
        watch_entities = [entity_id for entity_id in watch_entities if entity_id]
        if watch_entities:
            self._unsub = async_track_state_change_event(
                self.hass,
                watch_entities,
                self._async_handle_source_update,
            )

    async def async_shutdown(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    async def _async_seed_from_sources(self) -> None:
        self._mirror_rates()
        now = dt_util.utcnow()

        energy_sensor = self.entry.data.get(CONF_ENERGY_SENSOR)
        if energy_sensor:
            current_energy = self._read_float_state(energy_sensor)
            if current_energy is not None:
                self.data[STATE_LAST_TOTAL_ENERGY] = round(current_energy, 6)

        power_sensor = self.entry.data.get(CONF_POWER_SENSOR)
        if power_sensor:
            self._last_power_watts = self._read_float_state(power_sensor)
            self._last_power_timestamp = now
            if self.data[STATE_LAST_TOTAL_ENERGY] is None:
                self.data[STATE_LAST_TOTAL_ENERGY] = round(
                    float(self.data.get(STATE_TOTAL_ENERGY) or 0.0),
                    6,
                )

        await self._async_persist()
        self.async_set_updated_data(dict(self.data))

    async def _async_handle_source_update(self, event) -> None:
        event_data = event.data if hasattr(event, "data") else event["data"]
        entity_id = event_data["entity_id"]
        now = dt_util.utcnow()

        if entity_id in {
            self.entry.data.get(CONF_CURRENT_RATE_SENSOR),
            self.entry.data.get(CONF_BASELINE_RATE_SENSOR),
        }:
            await self._async_integrate_power_until(now)
            self._mirror_rates()
            await self._async_persist()
            self.async_set_updated_data(dict(self.data))
            return

        if entity_id == self.entry.data.get(CONF_POWER_SENSOR):
            await self._async_integrate_power_until(now)
            self._last_power_watts = self._read_float_state(entity_id)
            self._last_power_timestamp = now
            await self._async_persist()
            self.async_set_updated_data(dict(self.data))
            return

        if entity_id == self.entry.data.get(CONF_ENERGY_SENSOR):
            self._mirror_rates()
            await self._async_apply_energy_delta(entity_id, now)
            await self._async_persist()
            self.async_set_updated_data(dict(self.data))
            return

    def _mirror_rates(self) -> None:
        current_rate = self._read_float_state(self.entry.data[CONF_CURRENT_RATE_SENSOR])
        baseline_rate = self._read_float_state(self.entry.data[CONF_BASELINE_RATE_SENSOR])
        self.data[STATE_CURRENT_RATE] = current_rate
        self.data[STATE_BASELINE_RATE] = baseline_rate
        self.data[STATE_CURRENT_SAVINGS_RATE] = savings_rate(current_rate, baseline_rate)

    def _read_float_state(self, entity_id: str) -> float | None:
        state = self.hass.states.get(entity_id)
        return parse_float(state.state if state else None)

    async def _async_apply_energy_delta(self, entity_id: str, now: datetime) -> None:
        current_energy = self._read_float_state(entity_id)
        if current_energy is None:
            return

        previous_energy = parse_float(self.data.get(STATE_LAST_TOTAL_ENERGY))
        if previous_energy is None:
            self.data[STATE_LAST_TOTAL_ENERGY] = round(current_energy, 6)
            return

        delta = current_energy - previous_energy
        if delta <= 0:
            self.data[STATE_LAST_TOTAL_ENERGY] = round(current_energy, 6)
            return

        current_rate = parse_float(self.data.get(STATE_CURRENT_RATE))
        baseline_rate = parse_float(self.data.get(STATE_BASELINE_RATE))
        if current_rate is None or baseline_rate is None:
            self.data[STATE_LAST_TOTAL_ENERGY] = round(current_energy, 6)
            return

        self.data[STATE_TOTAL_ENERGY] = round(
            float(self.data.get(STATE_TOTAL_ENERGY) or 0.0) + delta,
            6,
        )
        self.data[STATE_TOTAL_SAVINGS] = round(
            float(self.data.get(STATE_TOTAL_SAVINGS) or 0.0)
            + (delta * (baseline_rate - current_rate)),
            6,
        )
        self.data[STATE_LAST_TOTAL_ENERGY] = round(current_energy, 6)
        self.data[STATE_LAST_UPDATED_TIME] = now.isoformat()

    async def _async_integrate_power_until(self, now: datetime) -> None:
        if self.entry.data.get(CONF_POWER_SENSOR) is None:
            return

        current_rate = parse_float(self.data.get(STATE_CURRENT_RATE))
        baseline_rate = parse_float(self.data.get(STATE_BASELINE_RATE))
        if current_rate is None or baseline_rate is None:
            self._last_power_timestamp = now
            return

        delta_kwh = integrate_left(
            power_watts=self._last_power_watts,
            started_at=self._last_power_timestamp,
            ended_at=now,
            active_power_threshold=float(
                self.entry.data.get(
                    CONF_ACTIVE_POWER_THRESHOLD,
                    DEFAULT_ACTIVE_POWER_THRESHOLD,
                )
            ),
        )
        self._last_power_timestamp = now
        if delta_kwh <= 0:
            return

        self.data[STATE_TOTAL_ENERGY] = round(
            float(self.data.get(STATE_TOTAL_ENERGY) or 0.0) + delta_kwh,
            6,
        )
        self.data[STATE_TOTAL_SAVINGS] = round(
            float(self.data.get(STATE_TOTAL_SAVINGS) or 0.0)
            + (delta_kwh * (baseline_rate - current_rate)),
            6,
        )
        self.data[STATE_LAST_TOTAL_ENERGY] = round(
            float(self.data.get(STATE_TOTAL_ENERGY) or 0.0),
            6,
        )
        self.data[STATE_LAST_UPDATED_TIME] = now.isoformat()

    async def _async_persist(self) -> None:
        persisted = {key: self.data.get(key) for key in PERSISTED_STATE_KEYS}
        await self._store.async_save(persisted)
