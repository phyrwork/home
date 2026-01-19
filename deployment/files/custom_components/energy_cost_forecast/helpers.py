from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Iterable

import yaml
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util


_DURATION_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)(?P<unit>[hm])")


@dataclass
class RateWindow:
    start: datetime
    end: datetime
    value: float


@dataclass
class ProfileSegment:
    duration: timedelta
    power_kw: float


def parse_duration(value: str) -> timedelta | None:
    if not value:
        return None
    text = str(value).strip().lower()
    if not text:
        return None

    total_seconds = 0.0
    matches = list(_DURATION_RE.finditer(text))
    if not matches:
        return None

    consumed = "".join(match.group(0) for match in matches)
    if consumed != text:
        return None

    for match in matches:
        amount = float(match.group("value"))
        if amount < 0:
            return None
        unit = match.group("unit")
        if unit == "h":
            total_seconds += amount * 3600.0
        elif unit == "m":
            total_seconds += amount * 60.0
        else:
            return None

    if total_seconds <= 0:
        return None
    return timedelta(seconds=total_seconds)


def _parse_profile_list(raw: object) -> list[ProfileSegment] | None:
    if not isinstance(raw, list):
        return None
    segments: list[ProfileSegment] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            return None
        power_w, duration_text = item
        try:
            power_w = float(power_w)
        except Exception:
            return None
        if power_w < 0:
            return None
        duration = parse_duration(str(duration_text))
        if duration is None:
            return None
        segments.append(ProfileSegment(duration=duration, power_kw=power_w / 1000.0))
    return segments or None


async def load_profile(
    hass: HomeAssistant,
    profile_input: object | None,
    profile_file: str | None,
) -> tuple[list[ProfileSegment] | None, str | None, str | None]:
    if profile_file:
        path = Path(profile_file).expanduser()
        if not path.exists():
            return None, profile_file, "missing_profile_file"

        def _read_file() -> str:
            return path.read_text(encoding="utf-8")

        try:
            content = await hass.async_add_executor_job(_read_file)
        except Exception:
            return None, profile_file, "profile_file_error"
        raw = yaml.safe_load(content)
        segments = _parse_profile_list(raw)
        return segments, profile_file, None if segments else "invalid_profile"

    if profile_input is None or profile_input == "":
        return None, None, "missing_profile"

    if isinstance(profile_input, list):
        raw = profile_input
    else:
        try:
            raw = yaml.safe_load(str(profile_input))
        except Exception:
            try:
                raw = json.loads(str(profile_input))
            except Exception:
                return None, str(profile_input), "invalid_profile"
    segments = _parse_profile_list(raw)
    return segments, str(profile_input), None if segments else "invalid_profile"


def parse_rates(raw_rates: object) -> list[RateWindow]:
    if isinstance(raw_rates, str):
        try:
            raw_rates = json.loads(raw_rates)
        except Exception:
            raw_rates = None
    if not isinstance(raw_rates, list):
        return []

    rates: list[RateWindow] = []
    for rate in raw_rates:
        if not isinstance(rate, dict):
            continue
        start = dt_util.parse_datetime(str(rate.get("start")))
        end = dt_util.parse_datetime(str(rate.get("end")))
        value = rate.get("value_inc_vat", rate.get("value"))
        if start is None or end is None or value is None:
            continue
        try:
            value_f = float(value)
        except Exception:
            continue
        rates.append(
            RateWindow(
                start=dt_util.as_utc(start),
                end=dt_util.as_utc(end),
                value=value_f,
            )
        )

    rates.sort(key=lambda item: item.start)
    return rates


def segment_cost(
    seg_start: datetime,
    seg_end: datetime,
    power_kw: float,
    rates: Iterable[RateWindow],
) -> float | None:
    cost = 0.0
    covered = 0.0
    total = (seg_end - seg_start).total_seconds()

    for rate in rates:
        if rate.end <= seg_start:
            continue
        if rate.start >= seg_end:
            break
        overlap_start = max(seg_start, rate.start)
        overlap_end = min(seg_end, rate.end)
        if overlap_end <= overlap_start:
            continue

        hours = (overlap_end - overlap_start).total_seconds() / 3600.0
        cost += power_kw * hours * rate.value
        covered += (overlap_end - overlap_start).total_seconds()

    if covered + 1e-6 < total:
        return None
    return cost


def cost_profile(
    start_dt: datetime,
    rates: Iterable[RateWindow],
    profile_segments: Iterable[ProfileSegment],
) -> float | None:
    total = 0.0
    seg_start = start_dt
    for segment in profile_segments:
        seg_end = seg_start + segment.duration
        cost = segment_cost(seg_start, seg_end, segment.power_kw, rates)
        if cost is None:
            return None
        total += cost
        seg_start = seg_end
    return total


def cost_profile_with_export_offset(
    start_dt: datetime,
    rates: Iterable[RateWindow],
    profile_segments: Iterable[ProfileSegment],
    export_power_kw: float,
    export_rate: float,
) -> float | None:
    total = 0.0
    seg_start = start_dt
    for segment in profile_segments:
        seg_end = seg_start + segment.duration
        base_cost = segment_cost(seg_start, seg_end, segment.power_kw, rates)
        if base_cost is None:
            return None

        if export_power_kw > 0:
            offset_kw = min(export_power_kw, segment.power_kw)
            offset_import = segment_cost(seg_start, seg_end, offset_kw, rates)
            if offset_import is None:
                return None
            hours = (seg_end - seg_start).total_seconds() / 3600.0
            offset_cost = offset_kw * hours * export_rate
            base_cost = base_cost - offset_import + offset_cost

        total += base_cost
        seg_start = seg_end

    return total


def profile_duration(profile_segments: Iterable[ProfileSegment]) -> timedelta:
    total = timedelta()
    for segment in profile_segments:
        total += segment.duration
    return total


def normalize_time(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, time):
        return value.strftime("%H:%M:%S")
    text = str(value).strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) == 2:
        parts.append("00")
    if len(parts) != 3:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2])
    except Exception:
        return None
    if hour < 0 or hour > 23 or minute < 0 or minute > 59 or second < 0 or second > 59:
        return None
    return f"{hour:02d}:{minute:02d}:{second:02d}"


def next_time(now_local: datetime, time_text: str) -> datetime:
    hour_s, minute_s, second_s = time_text.split(":")
    target = now_local.replace(
        hour=int(hour_s),
        minute=int(minute_s),
        second=int(second_s),
        microsecond=0,
    )
    if target <= now_local:
        target += timedelta(days=1)
    return target


def candidate_starts(
    rates: Iterable[RateWindow],
    now_utc: datetime,
    latest_start_utc: datetime | None,
) -> list[datetime]:
    starts = []
    for rate in rates:
        if rate.start < now_utc:
            continue
        if latest_start_utc is not None and rate.start > latest_start_utc:
            continue
        starts.append(rate.start)
    return starts
