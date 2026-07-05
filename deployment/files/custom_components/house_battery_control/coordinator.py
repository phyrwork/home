"""Coordinate planning and inverter control."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
)
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from . import battery, controller, inputs, planner
from .config import Config
from .const import DOMAIN
from .dependencies import solis_cloud, stub_inverter

_LOGGER = logging.getLogger(__name__)
_RETRY_DELAY = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class Decision:
    """Records the currently applicable controller decision."""

    reserve: planner.ReserveInterval
    """Required battery energy across the current interval."""

    command: controller.Command
    """Command selected for the inverter."""


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Records a decision and the context that produced it."""

    decision: Decision
    """Currently applicable controller decision."""

    battery_spec: battery.Spec
    """Battery characteristics used for the calculation."""

    battery_state: battery.State
    """Observed battery state used for the calculation."""

    input_interval: planner.InputInterval
    """Current fused forecast interval."""

    control: solis_cloud.Control
    """Normalized inverter control applied for the decision."""

    planning_horizon_end: datetime
    """End of the fused planning horizon."""

    tariff_forecast_end: datetime
    """End of available tariff coverage."""

    load_forecast_end: datetime
    """End of available load coverage."""

    solar_forecast_end: datetime
    """End of available solar coverage."""


class Coordinator(DataUpdateCoordinator[Snapshot]):
    """Recalculate and apply house battery control decisions."""

    def __init__(self, hass: HomeAssistant, config: Config) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=None,
            name=DOMAIN,
        )
        self.config = config
        self._previous_command: controller.Command | None = None
        self._unsub_sources: CALLBACK_TYPE | None = None
        self._unsub_timer: CALLBACK_TYPE | None = None

    async def async_start(self) -> None:
        """Subscribe to source changes and calculate the first decision."""
        if self._unsub_sources is None:
            self._unsub_sources = async_track_state_change_event(
                self.hass,
                self._source_entity_ids(),
                self._async_source_changed,
            )
        await self.async_refresh()

    async def async_stop(self) -> None:
        """Stop updates and leave the inverter in its fail-safe mode."""
        if self._unsub_sources is not None:
            self._unsub_sources()
            self._unsub_sources = None
        self._cancel_timer()
        await self._async_apply_fallback()
        await self.async_shutdown()

    async def _async_update_data(self) -> Snapshot:
        now = dt_util.now()
        try:
            source = await inputs.async_read_input(
                self.hass,
                self.config,
                now=now,
            )
            intervals = planner.fuse_forecasts(
                now=source.now,
                tariff_forecast=source.tariff_forecast,
                load_forecast=source.load_forecast,
                solar_forecast=source.solar_forecast,
            )
            if not intervals:
                raise ValueError("Forecasts do not extend beyond planning time")

            reserves = planner.reserve_intervals(
                spec=source.battery_spec,
                intervals=intervals,
                reserve_margin_kwh=inputs.read_decimal(
                    self.hass,
                    self.config.policy.reserve_margin_entity_id,
                ),
            )
            reserve = reserves[0]
            command = controller.select_command(
                spec=source.battery_spec,
                state=source.battery_state,
                tariff=intervals[0].tariff,
                reserve=reserve,
                export_hysteresis_kwh=inputs.read_decimal(
                    self.hass,
                    self.config.policy.export_hysteresis_entity_id,
                ),
                previous_command=self._previous_command,
            )
            control = solis_cloud.to_control(command, source.battery_spec)
            await stub_inverter.async_apply(
                self.hass,
                self.config.inverter,
                control,
            )
        except Exception as error:
            await self._async_apply_fallback()
            self._schedule(now + _RETRY_DELAY)
            raise UpdateFailed(str(error)) from error

        self._previous_command = command
        self._schedule(reserve.interval.end)
        return Snapshot(
            decision=Decision(reserve=reserve, command=command),
            battery_spec=source.battery_spec,
            battery_state=source.battery_state,
            input_interval=intervals[0],
            control=control,
            planning_horizon_end=intervals[-1].interval.end,
            tariff_forecast_end=max(
                item.interval.end for item in source.tariff_forecast
            ),
            load_forecast_end=max(
                item.interval.end for item in source.load_forecast
            ),
            solar_forecast_end=max(
                item.interval.end for item in source.solar_forecast
            ),
        )

    async def _async_apply_fallback(self) -> None:
        spec = self.config.battery.to_spec(power_limit_kw=Decimal())
        command = controller.SelfConsumption(
            minimum_energy_kwh=spec.minimum_energy_kwh
        )
        try:
            await stub_inverter.async_apply(
                self.hass,
                self.config.inverter,
                solis_cloud.to_control(command, spec),
            )
        except Exception:
            _LOGGER.exception("Failed to apply inverter fail-safe control")
            return
        self._previous_command = command

    async def _async_source_changed(
        self,
        _event: Event[EventStateChangedData],
    ) -> None:
        await self.async_request_refresh()

    async def _async_timer_elapsed(self, _now: datetime) -> None:
        self._unsub_timer = None
        await self.async_request_refresh()

    def _schedule(self, when: datetime) -> None:
        self._cancel_timer()
        self._unsub_timer = async_track_point_in_utc_time(
            self.hass,
            self._async_timer_elapsed,
            dt_util.as_utc(when),
        )

    def _cancel_timer(self) -> None:
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None

    def _source_entity_ids(self) -> tuple[str, ...]:
        return (
            self.config.battery.state_of_charge_entity_id,
            self.config.battery.power_limit_entity_id,
            self.config.tariff.import_price_entity_id,
            self.config.tariff.export_price_entity_id,
            self.config.policy.reserve_margin_entity_id,
            self.config.policy.export_hysteresis_entity_id,
        )
