from asyncio import run_coroutine_threadsafe
from dataclasses import field
from datetime import time, datetime, timedelta
from enum import Enum, auto
from typing import ClassVar, Type, cast, List, Callable, Any
from typing_extensions import TypeAlias

from aiohttp import ClientSession

from appdaemon.plugins.hass.hassapi import Hass
from marshmallow import Schema, EXCLUDE
from marshmallow_dataclass import add_schema, dataclass


LogFunc: TypeAlias = Callable[[str], None]


CALENDAR_URL = "https://servicelayer3c.azure-api.net/wastecalendar/collection/search/10095470485"


class CollectionType(Enum):
    ORGANIC = auto()
    RECYCLE = auto()
    DOMESTIC = auto()


@add_schema
@dataclass
class Collection:
    Schema: ClassVar[Type[Schema]]

    date: datetime
    types: List[CollectionType] = field(metadata={"data_key": "roundTypes"})

    class Meta:
        unknown = EXCLUDE


@add_schema
@dataclass
class Calendar:
    Schema: ClassVar[Type[Schema]]

    collections: List[Collection]

    class Meta:
        unknown = EXCLUDE


class CalendarQuerier:
    def __init__(self, url: str) -> None:
        self.session = ClientSession()
        self.url = url

    async def next(self) -> Collection:
        async with self.session.get(self.url) as resp:
            calendar = cast(Calendar, Calendar.Schema().load(await resp.json()))
        if not calendar.collections:
            raise ValueError("no collections found")
        return calendar.collections[0]

    async def close(self) -> None:
        await self.session.close()


class LightColor(Enum):
    BLUE = (240, 94)
    GREEN = (134, 89)
    PINK = (281, 96)


class Light:
    def __init__(self, ha: Hass, id: str) -> None:
        self.ha = ha
        self.id = id

    async def turn_on(self, color: LightColor) -> None:
        await self.ha.turn_on(self.id, brightness_pct=100, hs_color=color.value)


class Controller:
    TYPE_COLOR = {
        CollectionType.DOMESTIC: LightColor.PINK,
        CollectionType.RECYCLE: LightColor.BLUE,
        CollectionType.ORGANIC: LightColor.GREEN,
    }

    def __init__(self, log: LogFunc, querier: CalendarQuerier, light: Light) -> None:
        self.log = log
        self.querier = querier
        self.light = light

    async def update(self, _kwargs: Any) -> None:
        self.log("updating")

        collection = await self.querier.next()
        self.log(f"next collection is {collection}")

        now = datetime.now(collection.date.tzinfo)
        threshold = timedelta(days=1)
        if collection.date > now + threshold:
            self.log(f"next collection is beyond threshold: ignoring")
            return

        color = next((color for type, color in self.TYPE_COLOR.items() if type in collection.types), None)
        if color is None:
            raise ValueError(f"color not found for collection types {collection.types}")

        self.log(f"next collection color is {color}")
        await self.light.turn_on(color)


class App(Hass):
    querier: CalendarQuerier
    controller: Controller

    async def open(self) -> None:
        light = Light(self, "light.family_room_lamp")
        self.log(f"acquired light {light.id}")

        self.querier = CalendarQuerier(CALENDAR_URL)
        self.controller = Controller(self.log, self.querier, light)

        hour = 17
        await self.run_daily(self.controller.update, time(hour=hour))
        self.log(f"scheduled update for {hour}h daily")

    async def close(self) -> None:
        await self.querier.close()

    def initialize(self) -> None:
        run_coroutine_threadsafe(self.open(), self.AD.loop)

    def terminate(self) -> None:
        run_coroutine_threadsafe(self.close(), self.AD.loop)
