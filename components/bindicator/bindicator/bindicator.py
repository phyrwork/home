import logging
from datetime import datetime, timedelta
from enum import Enum
from typing import cast

from homeassistant.core import HomeAssistant, ServiceCall

from . import wastecalendar as wc


_LOGGER = logging.getLogger(__name__)


class LightColor(Enum):
    BLUE = (240, 94)
    GREEN = (134, 89)
    PINK = (281, 96)


class Light:
    def __init__(self, hass: HomeAssistant, id: str) -> None:
        self.hass = hass
        self.id = id

    async def turn_on(self, color: LightColor) -> None:
        await self.hass.services.async_call(
            "light",
            "turn_on",
            {
                "entity_id": self.id,
                "brightness_pct": 100,
                "hs_color": color.value,
            },
        )


class Controller:
    TYPE_COLOR = {
        wc.CollectionType.DOMESTIC: LightColor.PINK,
        wc.CollectionType.RECYCLE: LightColor.BLUE,
        wc.CollectionType.ORGANIC: LightColor.GREEN,
    }

    def __init__(self, hass: HomeAssistant, light: Light) -> None:
        self.hass = hass
        self.light = light

    async def update(self, _call: ServiceCall) -> None:
        collection = await wc.next()

        now = datetime.now(collection.date.tzinfo)
        threshold = timedelta(days=1)
        if collection.date > now + threshold:
            _LOGGER.info(f"next collection is beyond threshold: ignoring")
            return

        color = next(
            (color for type, color in self.TYPE_COLOR.items() if type in collection.types),
            None
            )
        if color is None:
            raise ValueError(f"color not found for collection types {collection.types}")

        _LOGGER.info(f"next collection color is {color}")
        await self.light.turn_on(color)
