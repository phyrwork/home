"""Pure trusted Octopus rate and export-window model.

This module deliberately has no Home Assistant dependency.  The template
producer supplies a fused, provenance-rich representation of Octopus's public
rate events; the functions below validate that representation and calculate
only the value of a profitable export cycle.  Physical battery scheduling is
owned by later strategy code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Mapping, Sequence

from .domain_constants import (
    BATTERY_CYCLE_COST_PER_KWH,
    MAXIMUM_SOURCE_FUTURE_SKEW,
    OCTOPUS_DISPATCH_SOURCE_MAX_AGE,
    OCTOPUS_EXPORT_SOURCE_MAX_AGE,
    OCTOPUS_RATE_SOURCE_MAX_AGE,
    OCTOPUS_RATE_UNIT,
)
from .interval import TimeInterval


class CheapClassification(str, Enum):
    """The only source-backed classifications accepted by the model."""

    STANDARD_CHEAP = "STANDARD_CHEAP"
    BONUS_DISPATCH = "BONUS_DISPATCH"
    NOT_CHEAP = "NOT_CHEAP"


class CoverageStatus(str, Enum):
    """Why a requested horizon can or cannot be acted upon."""

    COMPLETE = "COMPLETE"
    TRUSTED_EMPTY = "TRUSTED_EMPTY"
    UNAVAILABLE = "UNAVAILABLE"
    GAPPED = "GAPPED"
    INVALID = "INVALID"


def _decimal(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value)) if not isinstance(value, Decimal) else value
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a Decimal-compatible value") from exc
    if not result.is_finite():
        raise ValueError(f"{label} must be finite")
    return result


def _aware(value: Any, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value


def _instant(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _bool(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be Boolean")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class AdjustedRateInterval:
    """One validated import rate with its event provenance."""

    start: datetime
    end: datetime
    import_price: Decimal
    classification: CheapClassification
    source: str
    tariff: str
    source_day: str
    source_event: str
    source_revision_at: datetime
    retrieval_source_entity_id: str
    dispatch_source_entity_id: str
    event_minimum: Decimal
    event_unique_price_count: int
    is_intelligent_adjusted: bool
    unit: str = OCTOPUS_RATE_UNIT
    is_capped: bool | None = None

    def __post_init__(self) -> None:
        _aware(self.start, "start")
        _aware(self.end, "end")
        if _instant(self.end) <= _instant(self.start):
            raise ValueError("rate interval end must be after start")
        price = _decimal(self.import_price, "import_price")
        object.__setattr__(self, "import_price", price)
        if self.unit != OCTOPUS_RATE_UNIT:
            raise ValueError(f"unsupported rate unit: {self.unit!r}")
        _text(self.source, "source")
        _text(self.tariff, "tariff")
        _text(self.source_day, "source_day")
        _text(self.source_event, "source_event")
        _text(self.retrieval_source_entity_id, "retrieval_source_entity_id")
        _text(self.dispatch_source_entity_id, "dispatch_source_entity_id")
        _aware(self.source_revision_at, "source_revision_at")
        _bool(self.is_intelligent_adjusted, "is_intelligent_adjusted")
        if self.is_capped is not None:
            _bool(self.is_capped, "is_capped")
        object.__setattr__(self, "event_minimum", _decimal(self.event_minimum, "event_minimum"))
        if (
            type(self.event_unique_price_count) is not int or self.event_unique_price_count < 1
        ):
            raise ValueError("event_unique_price_count must be a positive integer")


@dataclass(frozen=True, slots=True)
class ExportRateInterval:
    """One validated export forecast rate with retrieval provenance."""

    start: datetime
    end: datetime
    export_price: Decimal
    source: str
    tariff: str
    retrieved_at: datetime
    source_day: str
    source_event: str
    source_revision_at: datetime
    retrieval_source_entity_id: str
    unit: str = OCTOPUS_RATE_UNIT
    is_capped: bool | None = None

    def __post_init__(self) -> None:
        _aware(self.start, "start")
        _aware(self.end, "end")
        if _instant(self.end) <= _instant(self.start):
            raise ValueError("export interval end must be after start")
        object.__setattr__(self, "export_price", _decimal(self.export_price, "export_price"))
        if self.unit != OCTOPUS_RATE_UNIT:
            raise ValueError(f"unsupported rate unit: {self.unit!r}")
        _text(self.source, "source")
        _text(self.tariff, "tariff")
        _text(self.source_day, "source_day")
        _text(self.source_event, "source_event")
        _text(self.retrieval_source_entity_id, "retrieval_source_entity_id")
        _aware(self.retrieved_at, "retrieved_at")
        _aware(self.source_revision_at, "source_revision_at")
        if self.is_capped is not None:
            _bool(self.is_capped, "is_capped")


@dataclass(frozen=True, slots=True)
class RateSourceObservation:
    retrieved_at: datetime
    source: str


@dataclass(frozen=True, slots=True)
class DispatchSourceObservation:
    retrieved_at: datetime
    source: str


@dataclass(frozen=True, slots=True)
class CheapWindowComponent:
    """An import/export intersection and its per-stored-kWh margin."""

    interval: TimeInterval
    rate_interval: AdjustedRateInterval
    export_interval: ExportRateInterval
    margin_per_stored_kwh: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "margin_per_stored_kwh", _decimal(self.margin_per_stored_kwh, "margin"))


@dataclass(frozen=True, slots=True)
class CheapWindow:
    start: datetime
    end: datetime
    components: tuple[CheapWindowComponent, ...]


@dataclass(frozen=True, slots=True)
class CheapWindowResult:
    coverage_status: CoverageStatus
    windows: tuple[CheapWindow, ...] = ()
    diagnostic_components: tuple[CheapWindowComponent, ...] = ()
    issues: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TrustedImportResult:
    """A complete import-classification view independent of export value."""

    coverage_status: CoverageStatus
    intervals: tuple[AdjustedRateInterval, ...] = ()
    diagnostic_intervals: tuple[AdjustedRateInterval, ...] = ()
    issues: tuple[str, ...] = ()


def _classification(value: Any) -> CheapClassification:
    try:
        return CheapClassification(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unknown cheap classification: {value!r}") from exc


def parse_fused_import_rates(rates: str | Sequence[Mapping[str, Any]]) -> tuple[AdjustedRateInterval, ...]:
    """Parse the mandatory provenance-rich records emitted by the template."""

    import json

    records: Any = json.loads(rates) if isinstance(rates, str) else rates
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        raise ValueError("fused rates must be a sequence")
    result: list[AdjustedRateInterval] = []
    groups: dict[tuple[str, str], list[Decimal]] = {}
    metadata: dict[tuple[str, str], tuple[Decimal, int]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError("each fused rate must be an object")
        required = (
            "start", "end", "value_inc_vat", "unit", "is_intelligent_adjusted",
            "classification", "source", "source_event", "source_day", "tariff",
            "source_revision_at", "retrieval_source_entity_id",
            "dispatch_source_entity_id", "event_min_rate", "event_unique_price_count",
        )
        missing = [key for key in required if key not in record]
        if missing:
            raise ValueError(f"fused rate missing mandatory fields: {', '.join(missing)}")
        start = _aware(_parse_datetime(record["start"], "start"), "start")
        end = _aware(_parse_datetime(record["end"], "end"), "end")
        price = _decimal(record["value_inc_vat"], "value_inc_vat")
        adjusted = _bool(record["is_intelligent_adjusted"], "is_intelligent_adjusted")
        event_min = _decimal(record["event_min_rate"], "event_min_rate")
        unique_count = record["event_unique_price_count"]
        if type(unique_count) is not int or unique_count < 1:
            raise ValueError("event_unique_price_count must be a positive integer")
        source = _text(record["source"], "source")
        event = _text(record["source_event"], "source_event")
        key = (source, event)
        groups.setdefault(key, []).append(price)
        if key in metadata and metadata[key] != (event_min, unique_count):
            raise ValueError("event provenance metadata is inconsistent")
        metadata[key] = (event_min, unique_count)
        is_capped = record.get("is_capped")
        if is_capped is not None:
            _bool(is_capped, "is_capped")
        interval = AdjustedRateInterval(
            start=start,
            end=end,
            import_price=price,
            classification=_classification(record["classification"]),
            source=source,
            tariff=_text(record["tariff"], "tariff"),
            source_day=_text(record["source_day"], "source_day"),
            source_event=event,
            source_revision_at=_parse_datetime(record["source_revision_at"], "source_revision_at"),
            retrieval_source_entity_id=_text(
                record["retrieval_source_entity_id"], "retrieval_source_entity_id"
            ),
            dispatch_source_entity_id=_text(
                record["dispatch_source_entity_id"], "dispatch_source_entity_id"
            ),
            event_minimum=event_min,
            event_unique_price_count=unique_count,
            is_intelligent_adjusted=adjusted,
            unit=record["unit"],
            is_capped=is_capped,
        )
        result.append(interval)
        if adjusted and price != event_min:
            raise ValueError("adjusted rate must equal the event minimum")
    for key, prices in groups.items():
        expected_min = min(prices)
        supplied_min, supplied_unique = metadata[key]
        if supplied_min != expected_min or supplied_unique != len(set(prices)):
            raise ValueError("event minimum or unique-price count does not match rates")
        for interval in (item for item in result if (item.source, item.source_event) == key):
            if interval.classification is CheapClassification.BONUS_DISPATCH:
                if interval.import_price != expected_min or not _adjusted_for(interval):
                    raise ValueError("bonus dispatch interval has inconsistent provenance")
            elif interval.classification is CheapClassification.STANDARD_CHEAP:
                if interval.import_price != expected_min or supplied_unique not in (2, 3) or _adjusted_for(interval):
                    raise ValueError("standard cheap interval has inconsistent provenance")
            elif (
                interval.import_price == expected_min
                and supplied_unique in (2, 3)
            ) or _adjusted_for(interval):
                # Every minimum in a two/three-rate standard event, and every
                # adjusted interval, must be classified explicitly.
                raise ValueError("cheap interval is not classified consistently")
    issue = _validate_import_interval_sequence(result)
    if issue:
        raise ValueError(issue)
    return tuple(result)


def _adjusted_for(interval: AdjustedRateInterval) -> bool:
    """Return the producer-normalized upstream adjustment flag."""

    return interval.is_intelligent_adjusted


def _validate_import_interval_sequence(
    intervals: Sequence[AdjustedRateInterval],
) -> str | None:
    """Validate direct objects as strictly as fused parser output."""

    allowed = {
        CheapClassification.STANDARD_CHEAP,
        CheapClassification.BONUS_DISPATCH,
        CheapClassification.NOT_CHEAP,
    }
    groups: dict[str, list[AdjustedRateInterval]] = {}
    previous: AdjustedRateInterval | None = None
    for interval in intervals:
        if type(interval) is not AdjustedRateInterval:
            return "import rate has an unexpected concrete type"
        if type(interval.classification) is not CheapClassification or interval.classification not in allowed:
            return "import rate classification is not allowlisted"
        try:
            _aware(interval.start, "start")
            _aware(interval.end, "end")
            _aware(interval.source_revision_at, "source_revision_at")
            _text(interval.source, "source")
            _text(interval.source_event, "source_event")
            _text(interval.source_day, "source_day")
            _text(interval.tariff, "tariff")
            _text(interval.retrieval_source_entity_id, "retrieval_source_entity_id")
            _text(interval.dispatch_source_entity_id, "dispatch_source_entity_id")
            _bool(interval.is_intelligent_adjusted, "is_intelligent_adjusted")
            _decimal(interval.import_price, "import_price")
            _decimal(interval.event_minimum, "event_minimum")
        except (AttributeError, ValueError) as exc:
            return str(exc)
        if interval.unit != OCTOPUS_RATE_UNIT:
            return "import rate unit is not canonical"
        if type(interval.event_unique_price_count) is not int or interval.event_unique_price_count < 1:
            return "event unique-price count is invalid"
        if _instant(interval.end) <= _instant(interval.start):
            return "import interval end must be after start"
        if previous is not None:
            if _instant(interval.start) < _instant(previous.start):
                return "import intervals are not ordered by UTC instant"
            if _instant(interval.start) < _instant(previous.end):
                return "import intervals overlap or are duplicated"
        previous = interval
        groups.setdefault(interval.source_event, []).append(interval)

    for event, group in groups.items():
        first = group[0]
        expected_metadata = (
            first.source,
            first.tariff,
            first.source_day,
            first.source_revision_at,
            first.unit,
            first.retrieval_source_entity_id,
            first.dispatch_source_entity_id,
            first.event_minimum,
            first.event_unique_price_count,
        )
        if any(
            (
                item.source,
                item.tariff,
                item.source_day,
                item.source_revision_at,
                item.unit,
                item.retrieval_source_entity_id,
                item.dispatch_source_entity_id,
                item.event_minimum,
                item.event_unique_price_count,
            )
            != expected_metadata
            for item in group
        ):
            return f"event provenance metadata is inconsistent: {event}"
        prices = [item.import_price for item in group]
        if min(prices) != first.event_minimum:
            return f"event minimum does not match rates: {event}"
        if len(set(prices)) != first.event_unique_price_count:
            return f"event unique-price count does not match rates: {event}"
        for item in group:
            expected_classification = (
                CheapClassification.BONUS_DISPATCH
                if item.is_intelligent_adjusted and item.import_price == first.event_minimum
                else CheapClassification.STANDARD_CHEAP
                if (
                    not item.is_intelligent_adjusted
                    and item.import_price == first.event_minimum
                    and first.event_unique_price_count in (2, 3)
                )
                else CheapClassification.NOT_CHEAP
            )
            if item.is_intelligent_adjusted and item.import_price != first.event_minimum:
                return f"adjusted rate is not at event minimum: {event}"
            if item.classification is not expected_classification:
                return f"rate classification contradicts event provenance: {event}"
    return None


def _validate_export_interval_sequence(
    intervals: Sequence[ExportRateInterval],
) -> str | None:
    previous: ExportRateInterval | None = None
    groups: dict[str, list[ExportRateInterval]] = {}
    for interval in intervals:
        if type(interval) is not ExportRateInterval:
            return "export rate has an unexpected concrete type"
        try:
            _aware(interval.start, "start")
            _aware(interval.end, "end")
            _aware(interval.retrieved_at, "retrieved_at")
            _aware(interval.source_revision_at, "source_revision_at")
            _text(interval.source, "source")
            _text(interval.source_event, "source_event")
            _text(interval.source_day, "source_day")
            _text(interval.tariff, "tariff")
            _text(interval.retrieval_source_entity_id, "retrieval_source_entity_id")
            _decimal(interval.export_price, "export_price")
        except (AttributeError, ValueError) as exc:
            return str(exc)
        if interval.unit != OCTOPUS_RATE_UNIT:
            return "export rate unit is not canonical"
        if _instant(interval.end) <= _instant(interval.start):
            return "export interval end must be after start"
        if previous is not None:
            if _instant(interval.start) < _instant(previous.start):
                return "export intervals are not ordered by UTC instant"
            if _instant(interval.start) < _instant(previous.end):
                return "export intervals overlap or are duplicated"
        previous = interval
        groups.setdefault(interval.source_event, []).append(interval)
    for event, group in groups.items():
        first = group[0]
        expected_metadata = (
            first.source,
            first.tariff,
            first.source_day,
            first.source_revision_at,
            first.retrieved_at,
            first.unit,
            first.retrieval_source_entity_id,
        )
        if any(
            (
                item.source,
                item.tariff,
                item.source_day,
                item.source_revision_at,
                item.retrieved_at,
                item.unit,
                item.retrieval_source_entity_id,
            )
            != expected_metadata
            for item in group
        ):
            return f"export event provenance metadata is inconsistent: {event}"
    return None


def parse_public_import_event(
    event: Mapping[str, Any],
    *,
    source: str,
    source_day: str,
    source_event: str,
    source_revision_at: datetime,
    retrieval_source_entity_id: str,
    dispatch_source_entity_id: str,
    unit: str = OCTOPUS_RATE_UNIT,
) -> tuple[AdjustedRateInterval, ...]:
    """Validate one upstream public event before it is fused.

    The upstream event deliberately permits an omitted adjustment flag for
    ordinary rates.  That omission is normalised here, at the producer
    boundary only; ``parse_fused_import_rates`` remains strict.
    """

    raw_rates = event.get("rates")
    import json

    if isinstance(raw_rates, str):
        try:
            raw_rates = json.loads(raw_rates)
        except json.JSONDecodeError as exc:
            raise ValueError("public event rates must be JSON") from exc
    if not isinstance(raw_rates, Sequence) or isinstance(raw_rates, (str, bytes)) or not raw_rates:
        raise ValueError("public event rates must be a non-empty sequence")
    if "min_rate" not in event:
        raise ValueError("public event is missing min_rate")
    supplied_min = _decimal(event["min_rate"], "min_rate")
    values: list[Decimal] = []
    for raw in raw_rates:
        if not isinstance(raw, Mapping) or "value_inc_vat" not in raw:
            raise ValueError("public event rate is missing value_inc_vat")
        values.append(_decimal(raw["value_inc_vat"], "value_inc_vat"))
    computed_min = min(values)
    if supplied_min != computed_min:
        raise ValueError("public event min_rate does not match its rates")
    unique_count = len(set(values))
    tariff = event.get("tariff_code", event.get("tariff"))
    result: list[AdjustedRateInterval] = []
    for raw, price in zip(raw_rates, values, strict=True):
        adjusted = _bool(raw.get("is_intelligent_adjusted", False), "is_intelligent_adjusted")
        classification = (
            CheapClassification.BONUS_DISPATCH
            if adjusted and price == computed_min
            else CheapClassification.STANDARD_CHEAP
            if not adjusted and price == computed_min and unique_count in (2, 3)
            else CheapClassification.NOT_CHEAP
        )
        if adjusted and price != computed_min:
            raise ValueError("adjusted rate must equal the event minimum")
        result.append(
            AdjustedRateInterval(
                start=_parse_datetime(raw.get("start"), "start"),
                end=_parse_datetime(raw.get("end"), "end"),
                import_price=price,
                classification=classification,
                source=source,
                tariff=_text(tariff, "tariff_code"),
                source_day=_text(source_day, "source_day"),
                source_event=_text(source_event, "source_event"),
                source_revision_at=source_revision_at,
                retrieval_source_entity_id=retrieval_source_entity_id,
                dispatch_source_entity_id=dispatch_source_entity_id,
                event_minimum=supplied_min,
                event_unique_price_count=unique_count,
                is_intelligent_adjusted=adjusted,
                unit=unit,
                is_capped=raw.get("is_capped"),
            )
        )
    issue = _validate_import_interval_sequence(result)
    if issue:
        raise ValueError(issue)
    return tuple(result)


def parse_public_export_event(
    event: Mapping[str, Any],
    *,
    source: str,
    source_event: str,
    retrieved_at: datetime,
    source_day: str,
    source_revision_at: datetime,
    retrieval_source_entity_id: str,
    unit: str = OCTOPUS_RATE_UNIT,
) -> tuple[ExportRateInterval, ...]:
    """Validate one public export event for use in the forecast."""

    raw_rates = event.get("rates")
    import json

    if isinstance(raw_rates, str):
        try:
            raw_rates = json.loads(raw_rates)
        except json.JSONDecodeError as exc:
            raise ValueError("public export event rates must be JSON") from exc
    if not isinstance(raw_rates, Sequence) or isinstance(raw_rates, (str, bytes)) or not raw_rates:
        raise ValueError("public export event rates must be a non-empty sequence")
    tariff = event.get("tariff_code", event.get("tariff"))
    result: list[ExportRateInterval] = []
    for raw in raw_rates:
        if not isinstance(raw, Mapping):
            raise ValueError("public export event rate must be an object")
        result.append(
            ExportRateInterval(
                start=_parse_datetime(raw.get("start"), "start"),
                end=_parse_datetime(raw.get("end"), "end"),
                export_price=_decimal(raw.get("value_inc_vat"), "value_inc_vat"),
                source=source,
                tariff=_text(tariff, "tariff_code"),
                retrieved_at=retrieved_at,
                source_day=source_day,
                source_event=source_event,
                source_revision_at=source_revision_at,
                retrieval_source_entity_id=retrieval_source_entity_id,
                unit=unit,
                is_capped=raw.get("is_capped"),
            )
        )
    issue = _validate_export_interval_sequence(result)
    if issue:
        raise ValueError(issue)
    return tuple(result)


def parse_fused_export_rates(rates: str | Sequence[Mapping[str, Any]]) -> tuple[ExportRateInterval, ...]:
    """Parse the provenance-rich export forecast emitted by the template."""

    import json

    records: Any = json.loads(rates) if isinstance(rates, str) else rates
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        raise ValueError("fused export rates must be a sequence")
    result: list[ExportRateInterval] = []
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError("each fused export rate must be an object")
        required = (
            "start", "end", "value_inc_vat", "unit", "source", "source_event",
            "source_day", "tariff", "retrieved_at", "source_revision_at",
            "retrieval_source_entity_id",
        )
        missing = [key for key in required if key not in record]
        if missing:
            raise ValueError(f"fused export rate missing mandatory fields: {', '.join(missing)}")
        result.append(
            ExportRateInterval(
                start=_parse_datetime(record["start"], "start"),
                end=_parse_datetime(record["end"], "end"),
                export_price=_decimal(record["value_inc_vat"], "value_inc_vat"),
                source=_text(record["source"], "source"),
                tariff=_text(record["tariff"], "tariff"),
                retrieved_at=_parse_datetime(record["retrieved_at"], "retrieved_at"),
                source_day=_text(record["source_day"], "source_day"),
                source_event=_text(record["source_event"], "source_event"),
                source_revision_at=_parse_datetime(
                    record["source_revision_at"], "source_revision_at"
                ),
                retrieval_source_entity_id=_text(
                    record["retrieval_source_entity_id"],
                    "retrieval_source_entity_id",
                ),
                unit=record["unit"],
                is_capped=record.get("is_capped"),
            )
        )
    issue = _validate_export_interval_sequence(result)
    if issue:
        raise ValueError(issue)
    return tuple(result)


def _parse_datetime(value: Any, label: str) -> datetime:
    if isinstance(value, datetime):
        return _aware(value, label)
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO datetime") from exc
    return _aware(parsed, label)


def value_for_stored_energy(margin_per_stored_kwh: Decimal, energy_kwh: Decimal) -> Decimal:
    """Return exact export-cycle value for stored energy withdrawn later."""

    margin = _decimal(margin_per_stored_kwh, "margin_per_stored_kwh")
    energy = _decimal(energy_kwh, "energy_kwh")
    if energy < 0:
        raise ValueError("energy_kwh must not be negative")
    return margin * energy


def _fresh(observation: RateSourceObservation | DispatchSourceObservation | None, now: datetime, maximum_age: timedelta, label: str) -> str | None:
    if observation is None:
        return f"{label} observation unavailable"
    try:
        retrieved = _aware(observation.retrieved_at, f"{label}.retrieved_at")
        source = _text(observation.source, f"{label}.source")
    except ValueError as exc:
        return str(exc)
    if retrieved > now + MAXIMUM_SOURCE_FUTURE_SKEW:
        return f"{label} observation is in the future"
    if now - retrieved > maximum_age:
        return f"{label} observation is stale"
    return None


def _coverage(intervals: Sequence[AdjustedRateInterval | ExportRateInterval], start: datetime, end: datetime) -> tuple[bool, str | None]:
    clipped: list[tuple[datetime, datetime, Any]] = []
    for item in intervals:
        item_start = _aware(item.start, "interval.start")
        item_end = _aware(item.end, "interval.end")
        if _instant(item_end) <= _instant(item_start):
            return False, "interval end must be after start"
        left = max(_instant(item_start), _instant(start))
        right = min(_instant(item_end), _instant(end))
        if left < right:
            clipped.append((left, right, item))
    clipped.sort(key=lambda x: (x[0], x[1]))
    if not clipped:
        return False, "source contains no requested coverage"
    cursor = _instant(start)
    for left, right, _item in clipped:
        if left > cursor:
            return False, "requested horizon has a gap"
        if left < cursor:
            return False, "requested horizon has overlapping coverage"
        cursor = right
    if cursor < _instant(end):
        return False, "requested horizon has a gap"
    return True, None


def _max_instant(a: datetime, b: datetime) -> datetime:
    return a if _instant(a) >= _instant(b) else b


def _min_instant(a: datetime, b: datetime) -> datetime:
    return a if _instant(a) <= _instant(b) else b


def _coverage_status(issue: str | None) -> CoverageStatus:
    if issue and ("overlap" in issue or "duplicate" in issue or "ordered" in issue):
        return CoverageStatus.INVALID
    return CoverageStatus.GAPPED


def evaluate_trusted_import_rates(
    *,
    import_rates: Sequence[AdjustedRateInterval],
    start: datetime,
    end: datetime,
    now: datetime,
    import_source: RateSourceObservation | None,
    dispatch_source: DispatchSourceObservation | None = None,
) -> TrustedImportResult:
    """Build a complete trusted import-classification view for reserve planning."""

    try:
        start = _aware(start, "requested start")
        end = _aware(end, "requested end")
        now = _aware(now, "now")
        if _instant(end) <= _instant(start):
            raise ValueError("requested horizon must be non-empty and ordered")
    except ValueError as exc:
        return TrustedImportResult(CoverageStatus.INVALID, issues=(str(exc),))
    if not isinstance(import_rates, Sequence) or isinstance(import_rates, (str, bytes)):
        return TrustedImportResult(CoverageStatus.INVALID, issues=("import rates must be a sequence",))
    issue = _validate_import_interval_sequence(import_rates)
    if issue:
        return TrustedImportResult(
            CoverageStatus.INVALID,
            diagnostic_intervals=tuple(
                item for item in import_rates if type(item) is AdjustedRateInterval
            ),
            issues=(issue,),
        )
    diagnostics = tuple(import_rates)
    freshness = _fresh(import_source, now, OCTOPUS_RATE_SOURCE_MAX_AGE, "import rate source")
    if freshness:
        return TrustedImportResult(
            CoverageStatus.UNAVAILABLE,
            diagnostic_intervals=diagnostics,
            issues=(freshness,),
        )
    assert import_source is not None
    if any(
        item.retrieval_source_entity_id != import_source.source for item in import_rates
    ):
        return TrustedImportResult(
            CoverageStatus.INVALID,
            diagnostic_intervals=diagnostics,
            issues=("import retrieval source does not match observation",),
        )
    if any(
        item.source_revision_at > now + MAXIMUM_SOURCE_FUTURE_SKEW
        for item in import_rates
    ):
        return TrustedImportResult(
            CoverageStatus.UNAVAILABLE,
            diagnostic_intervals=diagnostics,
            issues=("import rate source revision is in the future",),
        )
    covered, coverage_issue = _coverage(import_rates, start, end)
    if not covered:
        return TrustedImportResult(
            _coverage_status(coverage_issue),
            diagnostic_intervals=diagnostics,
            issues=(coverage_issue or "import coverage unavailable",),
        )
    bonus = tuple(
        item
        for item in import_rates
        if item.classification is CheapClassification.BONUS_DISPATCH
        and _instant(item.end) > _instant(start)
        and _instant(item.start) < _instant(end)
    )
    if bonus:
        dispatch_freshness = _fresh(
            dispatch_source,
            now,
            OCTOPUS_DISPATCH_SOURCE_MAX_AGE,
            "dispatch source",
        )
        if dispatch_freshness:
            return TrustedImportResult(
                CoverageStatus.UNAVAILABLE,
                diagnostic_intervals=diagnostics,
                issues=(dispatch_freshness,),
            )
        assert dispatch_source is not None
        if any(
            item.dispatch_source_entity_id != dispatch_source.source for item in bonus
        ):
            return TrustedImportResult(
                CoverageStatus.INVALID,
                diagnostic_intervals=diagnostics,
                issues=("dispatch source does not match bonus provenance",),
            )
    return TrustedImportResult(
        CoverageStatus.COMPLETE,
        intervals=diagnostics,
        diagnostic_intervals=diagnostics,
    )


def evaluate_cheap_windows(
    *,
    import_rates: Sequence[AdjustedRateInterval],
    export_rates: Sequence[ExportRateInterval],
    start: datetime,
    end: datetime,
    now: datetime,
    import_source: RateSourceObservation | None,
    export_source: RateSourceObservation | None,
    dispatch_source: DispatchSourceObservation | None = None,
    charge_efficiency: Decimal = Decimal("0.95"),
    discharge_efficiency: Decimal = Decimal("0.95"),
    cycle_cost_per_kwh: Decimal = BATTERY_CYCLE_COST_PER_KWH,
) -> CheapWindowResult:
    """Evaluate a complete trusted horizon without making a control choice."""

    try:
        start = _aware(start, "requested start")
        end = _aware(end, "requested end")
        now = _aware(now, "now")
        if _instant(end) <= _instant(start):
            raise ValueError("requested horizon must be non-empty and ordered")
        charge = _decimal(charge_efficiency, "charge_efficiency")
        discharge = _decimal(discharge_efficiency, "discharge_efficiency")
        cycle_cost = _decimal(cycle_cost_per_kwh, "cycle_cost_per_kwh")
        if not (Decimal(0) < charge <= Decimal(1) and Decimal(0) < discharge <= Decimal(1)):
            raise ValueError("efficiencies must be greater than zero and at most one")
        if cycle_cost < 0:
            raise ValueError("cycle cost must not be negative")
    except ValueError as exc:
        return CheapWindowResult(CoverageStatus.INVALID, issues=(str(exc),))

    if not isinstance(import_rates, Sequence) or isinstance(import_rates, (str, bytes)):
        return CheapWindowResult(CoverageStatus.INVALID, issues=("import rates must be a sequence",))
    if not isinstance(export_rates, Sequence) or isinstance(export_rates, (str, bytes)):
        return CheapWindowResult(CoverageStatus.INVALID, issues=("export rates must be a sequence",))
    import_validation_issue = _validate_import_interval_sequence(import_rates)
    export_validation_issue = _validate_export_interval_sequence(export_rates)
    if import_validation_issue or export_validation_issue:
        return CheapWindowResult(
            CoverageStatus.INVALID,
            issues=tuple(
                issue
                for issue in (import_validation_issue, export_validation_issue)
                if issue
            ),
        )

    import_freshness = _fresh(import_source, now, OCTOPUS_RATE_SOURCE_MAX_AGE, "import rate source")
    export_freshness = _fresh(export_source, now, OCTOPUS_EXPORT_SOURCE_MAX_AGE, "export rate source")
    if import_freshness or export_freshness:
        return CheapWindowResult(CoverageStatus.UNAVAILABLE, issues=tuple(x for x in (import_freshness, export_freshness) if x))

    assert import_source is not None
    assert export_source is not None
    provenance_issues: list[str] = []
    if any(item.retrieval_source_entity_id != import_source.source for item in import_rates):
        provenance_issues.append("import retrieval source does not match observation")
    if any(item.retrieval_source_entity_id != export_source.source for item in export_rates):
        provenance_issues.append("export retrieval source does not match observation")
    if any(item.retrieved_at != export_source.retrieved_at for item in export_rates):
        provenance_issues.append("export retrieval timestamp does not match observation")
    if provenance_issues:
        return CheapWindowResult(CoverageStatus.INVALID, issues=tuple(provenance_issues))

    future_limit = now + MAXIMUM_SOURCE_FUTURE_SKEW
    future_issues: list[str] = []
    if any(item.source_revision_at > future_limit for item in import_rates):
        future_issues.append("import rate source revision is in the future")
    if any(item.source_revision_at > future_limit for item in export_rates):
        future_issues.append("export rate source revision is in the future")
    if any(item.retrieved_at > future_limit for item in export_rates):
        future_issues.append("export rate retrieval is in the future")
    if any(now - item.retrieved_at > OCTOPUS_EXPORT_SOURCE_MAX_AGE for item in export_rates):
        future_issues.append("export rate interval provenance is stale")
    if future_issues:
        return CheapWindowResult(CoverageStatus.UNAVAILABLE, issues=tuple(future_issues))

    import_covered, import_issue = _coverage(import_rates, start, end)
    export_covered, export_issue = _coverage(export_rates, start, end)
    if not import_covered or not export_covered:
        status = _coverage_status(import_issue if not import_covered else export_issue)
        return CheapWindowResult(status, issues=tuple(x for x in (import_issue, export_issue) if x))

    diagnostics: list[CheapWindowComponent] = []
    candidates: list[CheapWindowComponent] = []
    for imported in import_rates:
        if imported.classification is CheapClassification.NOT_CHEAP:
            continue
        for exported in export_rates:
            intersection_start = _max_instant(imported.start, exported.start)
            intersection_end = _min_instant(imported.end, exported.end)
            left = max(_instant(intersection_start), _instant(start))
            right = min(_instant(intersection_end), _instant(end))
            if left >= right:
                continue
            # Use the import timestamp representation at exact boundaries,
            # retaining original local offsets and fold values where possible.
            component_start = intersection_start if _instant(intersection_start) >= _instant(start) else start
            component_end = intersection_end if _instant(intersection_end) <= _instant(end) else end
            margin = exported.export_price * discharge - imported.import_price / charge - cycle_cost
            component = CheapWindowComponent(
                interval=TimeInterval(start=component_start, end=component_end),
                rate_interval=imported,
                export_interval=exported,
                margin_per_stored_kwh=margin,
            )
            diagnostics.append(component)
            if margin > 0:
                candidates.append(component)

    bonus_actionable = any(
        item.rate_interval.classification is CheapClassification.BONUS_DISPATCH for item in candidates
    )
    if bonus_actionable:
        dispatch_freshness = _fresh(dispatch_source, now, OCTOPUS_DISPATCH_SOURCE_MAX_AGE, "dispatch source")
        if dispatch_freshness:
            return CheapWindowResult(CoverageStatus.UNAVAILABLE, diagnostic_components=tuple(diagnostics), issues=(dispatch_freshness,))
        assert dispatch_source is not None
        if any(
            item.rate_interval.classification is CheapClassification.BONUS_DISPATCH
            and item.rate_interval.dispatch_source_entity_id != dispatch_source.source
            for item in candidates
        ):
            return CheapWindowResult(
                CoverageStatus.INVALID,
                diagnostic_components=tuple(diagnostics),
                issues=("dispatch source does not match bonus provenance",),
            )

    if not candidates:
        return CheapWindowResult(CoverageStatus.TRUSTED_EMPTY, diagnostic_components=tuple(diagnostics))

    windows: list[CheapWindow] = []
    current: list[CheapWindowComponent] = []
    for component in candidates:
        if not current or _instant(component.interval.start) == _instant(current[-1].interval.end):
            current.append(component)
            continue
        windows.append(CheapWindow(current[0].interval.start, current[-1].interval.end, tuple(current)))
        current = [component]
    if current:
        windows.append(CheapWindow(current[0].interval.start, current[-1].interval.end, tuple(current)))
    return CheapWindowResult(CoverageStatus.COMPLETE, tuple(windows), tuple(diagnostics))


# A concise alias for callers that prefer a builder verb.
build_cheap_windows = evaluate_cheap_windows
