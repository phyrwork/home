from datetime import datetime, timedelta, timezone


def parse_iso(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def next_finish_time(now_local, latest_finish_time):
    try:
        hour_s, minute_s, second_s = latest_finish_time.split(":")
        hour = int(hour_s)
        minute = int(minute_s)
        second = int(second_s)
    except Exception:
        hour, minute, second = 6, 0, 0

    finish_local = now_local.replace(
        hour=hour,
        minute=minute,
        second=second,
        microsecond=0,
    )
    if finish_local <= now_local:
        finish_local = finish_local + timedelta(days=1)
    return finish_local


def parse_duration(value):
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text.isdigit():
        return None

    total_seconds = 0.0
    remaining = text
    for suffix, multiplier in (("h", 3600.0), ("m", 60.0)):
        if suffix in remaining:
            parts = remaining.split(suffix, 1)
            number = parts[0].strip()
            remaining = parts[1].strip()
            if not number:
                return None
            try:
                amount = float(number)
            except Exception:
                return None
            if amount < 0:
                return None
            total_seconds += amount * multiplier

    if remaining:
        return None
    if total_seconds <= 0:
        return None
    return timedelta(seconds=total_seconds)


def parse_profile_segments(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    segments = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "@" not in part:
            return None
        duration_text, power_text = part.split("@", 1)
        duration = parse_duration(duration_text.strip())
        if duration is None:
            return None
        try:
            power_kw = float(power_text.strip())
        except Exception:
            return None
        if power_kw < 0:
            return None
        segments.append((duration, power_kw))
    return segments or None


def read_profile_segments(entity_id, default_segments):
    raw = state.get(entity_id)
    if raw in (None, "", "unknown", "unavailable"):
        return default_segments, raw, "missing_profile"
    segments = parse_profile_segments(raw)
    if segments is None:
        return default_segments, raw, "invalid_profile"
    return segments, raw, None


def normalize_finish_time(value):
    if value is None:
        return None
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


def read_finish_time(entity_id, default_finish_time):
    raw = state.get(entity_id)
    if raw in (None, "", "unknown", "unavailable"):
        return default_finish_time, raw, "missing_finish_time"
    normalized = normalize_finish_time(raw)
    if normalized is None:
        return default_finish_time, raw, "invalid_finish_time"
    return normalized, raw, None


def segment_cost(seg_start, seg_end, power_kw, rates, override_rate=None, override_start=None):
    cost = 0.0
    covered = 0.0
    total = (seg_end - seg_start).total_seconds()

    for rate in rates:
        if rate["end"] <= seg_start:
            continue
        if rate["start"] >= seg_end:
            break
        overlap_start = max(seg_start, rate["start"])
        overlap_end = min(seg_end, rate["end"])
        if overlap_end <= overlap_start:
            continue

        rate_value = rate["value"]
        if override_rate is not None and override_start is not None:
            if rate["start"] <= override_start < rate["end"]:
                rate_value = override_rate

        hours = (overlap_end - overlap_start).total_seconds() / 3600.0
        cost += power_kw * hours * rate_value
        covered += (overlap_end - overlap_start).total_seconds()

    if covered + 1e-6 < total:
        return None
    return cost


def cost_profile(start_dt, rates, profile_segments, override_rate=None, override_start=None):
    total = 0.0
    seg_start = start_dt
    for duration, power_kw in profile_segments:
        seg_end = seg_start + duration
        cost = segment_cost(
            seg_start,
            seg_end,
            power_kw,
            rates,
            override_rate=override_rate,
            override_start=override_start,
        )
        if cost is None:
            return None
        total += cost
        seg_start = seg_end
    return total


def profile_kwh(profile_segments):
    kwh = []
    for duration, power_kw in profile_segments:
        hours = duration.total_seconds() / 3600.0
        kwh.append(power_kw * hours)
    return kwh


def build_rates(import_rate_events):
    rates = []
    for entity_id in import_rate_events:
        attrs = state.getattr(entity_id)
        if not attrs:
            continue
        for rate in attrs.get("rates", []):
            start = parse_iso(rate.get("start"))
            end = parse_iso(rate.get("end"))
            value = rate.get("value_inc_vat")
            if not start or not end or value is None:
                continue
            rates.append(
                {
                    "start": start.astimezone(timezone.utc),
                    "end": end.astimezone(timezone.utc),
                    "value": float(value),
                }
            )

    rates.sort(key=lambda item: item["start"])
    return rates


def select_override_rate(export_power_sensor, import_rate_sensor, export_rate_sensor):
    export_power_state = state.get(export_power_sensor)
    try:
        export_power_w = float(export_power_state)
    except Exception:
        export_power_w = 0.0

    override_rate = None
    rate_source = "import"
    try:
        if export_power_w > 0:
            override_rate = float(state.get(export_rate_sensor))
            rate_source = "export"
        else:
            override_rate = float(state.get(import_rate_sensor))
    except Exception:
        override_rate = None

    return override_rate, rate_source


def find_best_start(now_local, now_utc, rates, profile_segments, latest_finish_time, attempts=3):
    total_duration = timedelta()
    for segment in profile_segments:
        total_duration += segment[0]

    best_start = None
    best_cost = None
    latest_finish_utc = None
    latest_start_utc = None
    evaluated_starts = 0

    for _ in range(attempts):
        latest_finish_local = next_finish_time(now_local, latest_finish_time)
        latest_finish_utc = latest_finish_local.astimezone(timezone.utc)
        latest_start_utc = latest_finish_utc - total_duration

        candidate_starts = [
            rate["start"]
            for rate in rates
            if now_utc <= rate["start"] <= latest_start_utc
        ]

        if not candidate_starts:
            now_local = latest_finish_local + timedelta(seconds=1)
            continue

        for start_dt in candidate_starts:
            cost = cost_profile(start_dt, rates, profile_segments)
            if cost is None:
                continue
            if best_cost is None or cost < best_cost:
                best_cost = cost
                best_start = start_dt

        evaluated_starts = len(candidate_starts)
        if best_start is not None:
            break

        now_local = latest_finish_local + timedelta(seconds=1)

    return {
        "best_start": best_start,
        "best_cost": best_cost,
        "latest_finish_utc": latest_finish_utc,
        "latest_start_utc": latest_start_utc,
        "evaluated_starts": evaluated_starts,
    }
