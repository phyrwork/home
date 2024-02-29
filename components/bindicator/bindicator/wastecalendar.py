from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import ClassVar, Type, List, cast

import aiohttp
from marshmallow import Schema, EXCLUDE

from marshmallow_dataclass import add_schema

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


async def next() -> Collection | None:
    async with aiohttp.ClientSession() as session:
        async with session.get(CALENDAR_URL) as resp:
            data = await resp.json()
    calendar = cast(Calendar, Calendar.Schema().load(data))

    # Only consider collections now or in the future
    collections = [
        collection for collection in calendar.collections
        if collection.date >= datetime.now(collection.date.tzinfo)
    ]

    if not collections:
        return None
    return collections[0]
