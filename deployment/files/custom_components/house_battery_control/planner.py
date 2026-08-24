"""Pure tariff, interval, load and reserve planning algorithms."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal, InvalidOperation
from enum import Enum
from math import gcd, lcm
from typing import Any, Mapping, Sequence

from .model import (
    CycleState,
    FULL_SOC_PERCENT,
    LogicalIntent,
    MINIMUM_SOC_PERCENT,
    SlotDirection,
    SlotIntent,
    SlotOwner,
    StrategyAction,
)

BATTERY_CYCLE_COST_PER_KWH = Decimal("0.0165")
MAXIMUM_GRID_IMPORT_POWER_KW = Decimal("0.1")
MAXIMUM_SOURCE_FUTURE_SKEW = timedelta(minutes=2)
OCTOPUS_EXPORT_SOURCE_MAX_AGE = timedelta(hours=26)
OCTOPUS_RATE_SOURCE_MAX_AGE = timedelta(hours=26)
BONUS_CHARGE_LEASE_DURATION = timedelta(minutes=15)
OCTOPUS_RATE_UNIT = "GBP/kWh"
# Solis reports whole-percent SOC. Reserve export must clear the one-percent
# reporting uncertainty before it can be physically actionable.
RESERVE_SOC_UNCERTAINTY_PERCENT = Decimal("1")


@dataclass(frozen=True, slots=True)
class TimeInterval:
    """A half-open, timezone-aware interval."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        _aware_interval_endpoint(self.start, "interval start")
        _aware_interval_endpoint(self.end, "interval end")
        if self.end.astimezone(timezone.utc) <= self.start.astimezone(timezone.utc):
            raise ValueError("interval must be non-empty and ordered")


@dataclass(frozen=True, slots=True)
class EnergyInterval:
    """Energy attributed to one interval."""

    interval: TimeInterval
    energy_kwh: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.energy_kwh, Decimal) or not self.energy_kwh.is_finite():
            raise ValueError("interval energy must be a finite Decimal")
        if self.energy_kwh < 0:
            raise ValueError("interval energy must not be negative")


def _aware_interval_endpoint(value: datetime, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


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


def _fold(value: Any, label: str) -> int:
    """Validate the explicit PEP 495 fold bit carried by fused records."""

    if type(value) is not int or value not in (0, 1):
        raise ValueError(f"{label} must be the integer PEP 495 fold bit 0 or 1")
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
            "start", "end", "start_fold", "end_fold", "value_inc_vat", "unit", "is_intelligent_adjusted",
            "classification", "source", "source_event", "source_day", "tariff",
            "source_revision_at", "retrieval_source_entity_id",
            "dispatch_source_entity_id", "event_min_rate", "event_unique_price_count",
        )
        missing = [key for key in required if key not in record]
        if missing:
            raise ValueError(f"fused rate missing mandatory fields: {', '.join(missing)}")
        start = _parse_datetime_with_fold(record["start"], record["start_fold"], "start")
        end = _parse_datetime_with_fold(record["end"], record["end_fold"], "end")
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


def _ordered_interval_issue(
    intervals: Sequence[Any], expected_type: type[Any], label: str
) -> str | None:
    previous: Any | None = None
    for interval in intervals:
        if type(interval) is not expected_type:
            return f"{label} rate has an unexpected concrete type"
        try:
            _aware(interval.start, "start")
            _aware(interval.end, "end")
        except (AttributeError, ValueError) as exc:
            return str(exc)
        if _instant(interval.end) <= _instant(interval.start):
            return f"{label} interval end must be after start"
        if previous is not None and _instant(interval.start) < _instant(previous.start):
            return f"{label} intervals are not ordered by UTC instant"
        if previous is not None and _instant(interval.start) < _instant(previous.end):
            return f"{label} intervals overlap or are duplicated"
        previous = interval
    return None


def _uniform_metadata(items: Sequence[Any], names: tuple[str, ...]) -> bool:
    expected = tuple(getattr(items[0], name) for name in names)
    return all(tuple(getattr(item, name) for name in names) == expected for item in items)


def _validate_import_interval_sequence(intervals: Sequence[AdjustedRateInterval]) -> str | None:
    """Validate direct objects as strictly as fused parser output."""

    if issue := _ordered_interval_issue(intervals, AdjustedRateInterval, "import"):
        return issue
    allowed = {
        CheapClassification.STANDARD_CHEAP,
        CheapClassification.BONUS_DISPATCH,
        CheapClassification.NOT_CHEAP,
    }
    groups: dict[str, list[AdjustedRateInterval]] = {}
    for interval in intervals:
        if type(interval.classification) is not CheapClassification or interval.classification not in allowed:
            return "import rate classification is not allowlisted"
        try:
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
        groups.setdefault(interval.source_event, []).append(interval)

    for event, group in groups.items():
        first = group[0]
        if not _uniform_metadata(group, (
            "source", "tariff", "source_day", "source_revision_at", "unit",
            "retrieval_source_entity_id", "dispatch_source_entity_id",
            "event_minimum", "event_unique_price_count",
        )):
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


def _validate_export_interval_sequence(intervals: Sequence[ExportRateInterval]) -> str | None:
    if issue := _ordered_interval_issue(intervals, ExportRateInterval, "export"):
        return issue
    groups: dict[str, list[ExportRateInterval]] = {}
    for interval in intervals:
        try:
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
        groups.setdefault(interval.source_event, []).append(interval)
    for event, group in groups.items():
        if not _uniform_metadata(group, (
            "source", "tariff", "source_day", "source_revision_at",
            "retrieved_at", "unit", "retrieval_source_entity_id",
        )):
            return f"export event provenance metadata is inconsistent: {event}"
    return None


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
            "start", "end", "start_fold", "end_fold", "value_inc_vat", "unit", "source", "source_event",
            "source_day", "tariff", "retrieved_at", "source_revision_at",
            "retrieval_source_entity_id",
        )
        missing = [key for key in required if key not in record]
        if missing:
            raise ValueError(f"fused export rate missing mandatory fields: {', '.join(missing)}")
        result.append(
            ExportRateInterval(
                start=_parse_datetime_with_fold(record["start"], record["start_fold"], "start"),
                end=_parse_datetime_with_fold(record["end"], record["end_fold"], "end"),
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


def _parse_datetime_with_fold(value: Any, fold: Any, label: str) -> datetime:
    """Parse a fused timestamp and restore its separately serialized fold bit.

    ``datetime.isoformat()`` serializes the UTC offset but not ``fold``.  The
    producer therefore emits the bit beside each interval endpoint; restore it
    after parsing so consumers retain the original PEP 495 wall-clock
    occurrence rather than silently collapsing it.
    """

    parsed = _parse_datetime(value, label)
    return parsed.replace(fold=_fold(fold, f"{label}_fold"))


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


def _dispatch_observation_issue(
    observation: DispatchSourceObservation | None, now: datetime
) -> str | None:
    """Validate change-driven dispatch metadata without imposing an age."""

    if observation is None:
        return "dispatch source observation unavailable"
    try:
        retrieved = _aware(observation.retrieved_at, "dispatch source.retrieved_at")
        _text(observation.source, "dispatch source.source")
    except ValueError as exc:
        return str(exc)
    if retrieved > now + MAXIMUM_SOURCE_FUTURE_SKEW:
        return "dispatch source observation is in the future"
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


def _trusted_import_base(
    *,
    import_rates: Sequence[AdjustedRateInterval],
    start: datetime,
    end: datetime,
    now: datetime,
    import_source: RateSourceObservation | None,
) -> TrustedImportResult:
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
    return TrustedImportResult(
        CoverageStatus.COMPLETE,
        intervals=diagnostics,
        diagnostic_intervals=diagnostics,
    )


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

    result = _trusted_import_base(
        import_rates=import_rates,
        start=start,
        end=end,
        now=now,
        import_source=import_source,
    )
    if result.coverage_status is not CoverageStatus.COMPLETE:
        return result
    bonus = tuple(
        item
        for item in result.intervals
        if item.classification is CheapClassification.BONUS_DISPATCH
        and _instant(item.end) > _instant(start)
        and _instant(item.start) < _instant(end)
    )
    if not bonus:
        return result
    # Dispatch is change-driven. Its identity and structure remain checked
    # below; last_reported is not a heartbeat freshness gate.
    dispatch_issue = _dispatch_observation_issue(dispatch_source, now)
    if dispatch_issue:
        return TrustedImportResult(
            CoverageStatus.UNAVAILABLE,
            diagnostic_intervals=result.diagnostic_intervals,
            issues=(dispatch_issue,),
        )
    if any(item.dispatch_source_entity_id != dispatch_source.source for item in bonus):
        return TrustedImportResult(
            CoverageStatus.INVALID,
            diagnostic_intervals=result.diagnostic_intervals,
            issues=("dispatch source does not match bonus provenance",),
        )
    return result


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

    trusted_import = _trusted_import_base(
        import_rates=import_rates,
        start=start,
        end=end,
        now=now,
        import_source=import_source,
    )
    if trusted_import.coverage_status is not CoverageStatus.COMPLETE:
        return CheapWindowResult(
            trusted_import.coverage_status,
            issues=trusted_import.issues,
        )
    if not isinstance(export_rates, Sequence) or isinstance(export_rates, (str, bytes)):
        return CheapWindowResult(CoverageStatus.INVALID, issues=("export rates must be a sequence",))
    export_validation_issue = _validate_export_interval_sequence(export_rates)
    if export_validation_issue:
        return CheapWindowResult(CoverageStatus.INVALID, issues=(export_validation_issue,))

    export_freshness = _fresh(export_source, now, OCTOPUS_EXPORT_SOURCE_MAX_AGE, "export rate source")
    if export_freshness:
        return CheapWindowResult(CoverageStatus.UNAVAILABLE, issues=(export_freshness,))

    assert export_source is not None
    provenance_issues: list[str] = []
    if any(item.retrieval_source_entity_id != export_source.source for item in export_rates):
        provenance_issues.append("export retrieval source does not match observation")
    if any(item.retrieved_at != export_source.retrieved_at for item in export_rates):
        provenance_issues.append("export retrieval timestamp does not match observation")
    if provenance_issues:
        return CheapWindowResult(CoverageStatus.INVALID, issues=tuple(provenance_issues))

    future_limit = now + MAXIMUM_SOURCE_FUTURE_SKEW
    future_issues: list[str] = []
    if any(item.source_revision_at > future_limit for item in export_rates):
        future_issues.append("export rate source revision is in the future")
    if any(item.retrieved_at > future_limit for item in export_rates):
        future_issues.append("export rate retrieval is in the future")
    if any(now - item.retrieved_at > OCTOPUS_EXPORT_SOURCE_MAX_AGE for item in export_rates):
        future_issues.append("export rate interval provenance is stale")
    if future_issues:
        return CheapWindowResult(CoverageStatus.UNAVAILABLE, issues=tuple(future_issues))

    export_covered, export_issue = _coverage(export_rates, start, end)
    if not export_covered:
        return CheapWindowResult(
            _coverage_status(export_issue),
            issues=(export_issue or "export coverage unavailable",),
        )

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
        dispatch_issue = _dispatch_observation_issue(dispatch_source, now)
        if dispatch_issue:
            return CheapWindowResult(
                CoverageStatus.UNAVAILABLE,
                diagnostic_components=tuple(diagnostics),
                issues=(dispatch_issue,),
            )
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


# Median non-EV household load by local clock hour, derived from eight weeks of
# complete HA statistics. The profiles retain exact analysed daily totals.
_HOUR = timedelta(hours=1)
_WEEKDAY_KWH = tuple(map(Decimal, (
    "0.2546 0.2546 0.2546 0.2572 0.2520 0.2494 0.1436 0.2520 0.2468 0.2768 0.2585 0.2651 "
    "0.2677 0.2559 0.2703 0.2742 0.2468 0.2742 0.2572 0.2794 0.2990 0.3473 0.2690 0.2708"
).split()))
_WEEKEND_KWH = tuple(map(Decimal, (
    "0.2773 0.2377 0.2731 0.2363 0.2363 0.2363 0.1853 0.2334 0.3070 0.3296 0.3579 0.3169 "
    "0.3042 0.2490 0.2490 0.2561 0.2646 0.2759 0.2773 0.2872 0.3381 0.4216 0.3268 0.2801"
).split()))


def forecast_load(
    *, now: datetime, horizon_end: datetime, timezone: tzinfo
) -> tuple[EnergyInterval, ...]:
    """Return hourly non-EV load covering the requested horizon."""

    _aware(now, "Forecast start")
    _aware(horizon_end, "Forecast end")
    if horizon_end <= now:
        return ()
    start = now.replace(minute=0, second=0, microsecond=0)
    result: list[EnergyInterval] = []
    while start < horizon_end:
        local_start = start.astimezone(timezone)
        profile = _WEEKEND_KWH if local_start.weekday() >= 5 else _WEEKDAY_KWH
        result.append(
            EnergyInterval(
                TimeInterval(start, start + _HOUR),
                profile[local_start.hour],
            )
        )
        start += _HOUR
    return tuple(result)


def prorated_energy(
    interval: TimeInterval,
    items: Sequence[EnergyInterval],
    *,
    required: bool,
) -> Decimal:
    """Prorate energy over an interval, optionally requiring full coverage."""

    total = Decimal(0)
    covered = timedelta(0)
    for item in items:
        left = max(interval.start, item.interval.start)
        right = min(interval.end, item.interval.end)
        if right <= left:
            continue
        overlap = right - left
        covered += overlap
        total += (
            item.energy_kwh
            * Decimal(str(overlap.total_seconds()))
            / Decimal(str((item.interval.end - item.interval.start).total_seconds()))
        )
    if required and covered != interval.end - interval.start:
        raise ValueError("energy forecast does not cover the requested interval")
    return total


@dataclass(frozen=True, slots=True)
class ReserveInputInterval:
    interval: TimeInterval
    load_kwh: Decimal
    solar_kwh: Decimal
    classification: CheapClassification


@dataclass(frozen=True, slots=True)
class ReservePlanResult:
    """The required starting reserve, or one concise failure."""

    reserve_energy_kwh: Decimal | None = None
    issue: str | None = None


def plan_reserve(
    *,
    intervals: Sequence[ReserveInputInterval],
    capacity_kwh: Decimal,
    minimum_energy_kwh: Decimal,
    reserve_margin_kwh: Decimal,
    charge_efficiency: Decimal,
    discharge_efficiency: Decimal,
    maximum_charge_power_kw: Decimal,
    maximum_discharge_power_kw: Decimal,
) -> ReservePlanResult:
    """Reverse-plan energy needed until the next trusted charge opportunity."""

    try:
        values = (
            capacity_kwh,
            minimum_energy_kwh,
            reserve_margin_kwh,
            charge_efficiency,
            discharge_efficiency,
            maximum_charge_power_kw,
            maximum_discharge_power_kw,
        )
        if any(not isinstance(value, Decimal) or not value.is_finite() for value in values):
            raise ValueError("planner values must be finite Decimals")
        if capacity_kwh <= 0 or not Decimal(0) <= minimum_energy_kwh <= capacity_kwh:
            raise ValueError("battery energy bounds are invalid")
        if reserve_margin_kwh < 0:
            raise ValueError("reserve margin must not be negative")
        if not Decimal(0) < charge_efficiency <= 1 or not Decimal(0) < discharge_efficiency <= 1:
            raise ValueError("efficiencies must be in (0, 1]")
        if maximum_charge_power_kw <= 0 or maximum_discharge_power_kw <= 0:
            raise ValueError("runtime power limits must be positive")
        if not intervals:
            raise ValueError("at least one forecast interval is required")
        previous_end: datetime | None = None
        for item in intervals:
            if type(item) is not ReserveInputInterval:
                raise ValueError("forecast interval has an unexpected type")
            _aware(item.interval.start, "forecast interval start")
            _aware(item.interval.end, "forecast interval end")
            if _instant(item.interval.end) <= _instant(item.interval.start):
                raise ValueError("forecast interval must be ordered")
            if previous_end is not None and _instant(item.interval.start) != _instant(previous_end):
                raise ValueError("forecast intervals must be contiguous")
            if any(not isinstance(value, Decimal) or not value.is_finite() for value in (item.load_kwh, item.solar_kwh)):
                raise ValueError("forecast energy must be finite Decimals")
            if item.solar_kwh < 0:
                raise ValueError("solar forecast must not be negative")
            previous_end = item.interval.end
    except (AttributeError, TypeError, ValueError) as exc:
        return ReservePlanResult(issue=str(exc))

    floor = minimum_energy_kwh + reserve_margin_kwh
    if floor > capacity_kwh:
        return ReservePlanResult(issue="minimum energy plus reserve margin exceeds capacity")

    required = floor
    for item in reversed(intervals):
        hours = _hours(item.interval.start, item.interval.end)
        deficit = item.load_kwh - item.solar_kwh
        if item.classification is not CheapClassification.NOT_CHEAP:
            required = max(
                floor,
                required - maximum_charge_power_kw * hours * charge_efficiency,
            )
        elif deficit > 0:
            battery_output = max(
                Decimal(0),
                deficit - MAXIMUM_GRID_IMPORT_POWER_KW * hours,
            )
            if battery_output > maximum_discharge_power_kw * hours:
                return ReservePlanResult(issue="forecast demand exceeds battery and grid power")
            required += battery_output / discharge_efficiency
        else:
            # External-PV surplus appears as negative load and charges in
            # Feed-In Priority.
            stored = min(-deficit, maximum_charge_power_kw * hours) * charge_efficiency
            required = max(floor, required - stored)
        if required > capacity_kwh:
            return ReservePlanResult(issue="required household reserve exceeds battery capacity")
    return ReservePlanResult(required)


def _hours(start: datetime, end: datetime) -> Decimal:
    delta = _instant(end) - _instant(start)
    micros = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    return Decimal(micros) / Decimal(3_600_000_000)


@dataclass(frozen=True, slots=True)
class Plan:
    """One complete economic plan or one recoverable planning issue."""

    action: StrategyAction
    intent: LogicalIntent | None
    next_cycle_state: CycleState
    cycle_deadline: datetime | None
    charge_lease_deadline: datetime | None = None
    current_cheap_window: CheapWindow | None = None
    next_cheap_window: CheapWindow | None = None
    reserve_soc_percent: Decimal | None = None
    reserve_energy_kwh: Decimal | None = None
    control_reserve_soc_percent: Decimal | None = None
    control_reserve_energy_kwh: Decimal | None = None
    battery_energy_kwh: Decimal | None = None
    reserve_balance_kwh: Decimal | None = None
    control_reserve_balance_kwh: Decimal | None = None
    maximum_charge_power_kw: Decimal | None = None
    maximum_discharge_power_kw: Decimal | None = None
    issue: str | None = None


@dataclass(frozen=True, slots=True)
class _StrategyFacts:
    now: datetime
    soc_percent: Decimal
    reserve_soc_percent: Decimal
    cheap_window: CheapWindow | None
    cheap_charge: SlotIntent | None
    reserve_discharge: SlotIntent | None
    cycle_discharge: SlotIntent | None
    recharge_duration: timedelta
    cycle_duration: timedelta
    cycle_state: CycleState
    cycle_deadline: datetime | None


@dataclass(frozen=True, slots=True)
class _Choice:
    action: StrategyAction
    intent: SlotIntent | None
    cycle_state: CycleState
    cycle_deadline: datetime | None


async def build_plan(
    hass: Any,
    config: Any,
    solis_state: Any,
    *,
    now: datetime,
    cycle_state: CycleState,
    cycle_deadline: datetime | None,
    charge_lease_deadline: datetime | None = None,
) -> Plan:
    """Read planning inputs and return one model-only UTC plan."""

    actual_energy = _actual_battery_energy(config, solis_state)
    try:
        _aware(now, "now")
        if type(cycle_state) is not CycleState:
            raise ValueError("cycle state is invalid")
        if cycle_deadline is not None:
            _aware(cycle_deadline, "cycle deadline")
        if charge_lease_deadline is not None:
            _aware(charge_lease_deadline, "charge lease deadline")

        import_state = _state(hass, config.tariff.import_rates_entity_id)
        export_state = _state(hass, config.tariff.export_rates_entity_id)
        duration_state = _state(hass, config.cycle_discharge_duration_entity_id)
        cycle_duration = _cycle_duration(duration_state)
        import_rates = parse_fused_import_rates(_attribute(import_state, "rates"))
        export_rates = parse_fused_export_rates(_attribute(export_state, "rates"))
        if not import_rates or not export_rates:
            raise ValueError("tariff forecast is empty")
        horizon_end = min(max(item.end for item in import_rates), max(item.end for item in export_rates))
        if horizon_end <= now:
            raise ValueError("tariff forecast does not extend beyond now")

        import_source = _rate_source(import_state, "rate_source_entity_id", "rate_source_last_retrieved")
        export_source = _rate_source(export_state, "rate_source_entity_id", "rate_source_last_retrieved")
        dispatch_source = _dispatch_source(import_state)
        windows_result = evaluate_cheap_windows(
            import_rates=import_rates,
            export_rates=export_rates,
            start=now,
            end=horizon_end,
            now=now,
            import_source=import_source,
            export_source=export_source,
            dispatch_source=dispatch_source,
            charge_efficiency=config.battery.charge_efficiency,
            discharge_efficiency=config.battery.discharge_efficiency,
        )
        if windows_result.coverage_status not in (CoverageStatus.COMPLETE, CoverageStatus.TRUSTED_EMPTY):
            raise ValueError("tariff window input is not trusted: " + "; ".join(windows_result.issues))
        current_window = next(
            (
                window
                for window in windows_result.windows
                if _instant(window.start) <= _instant(now) < _instant(window.end)
            ),
            None,
        )
        next_window = next(
            (window for window in windows_result.windows if _instant(window.start) > _instant(now)),
            None,
        )

        # A bonus interval is authoritative only while the configured
        # dispatch source is directly on.  The fused entity remains the
        # dependency that wakes this controller when the source changes;
        # last_reported is deliberately not used as a heartbeat deadline.
        if _window_has_bonus_at(current_window, now):
            dispatch_state = _state(hass, dispatch_source.source)
            if dispatch_state.state != "on":
                raise ValueError("dispatch source is not on")

        reserve_end = next_window.start if next_window is not None else horizon_end
        reserve_end = _minute_floor(min(reserve_end, now + timedelta(hours=23, minutes=59)))
        if reserve_end <= now:
            reserve_end = _minute_floor(now + timedelta(minutes=1))
        trusted_import = evaluate_trusted_import_rates(
            import_rates=import_rates,
            start=now,
            end=reserve_end,
            now=now,
            import_source=import_source,
            dispatch_source=dispatch_source,
        )
        if trusted_import.coverage_status is not CoverageStatus.COMPLETE:
            raise ValueError("reserve tariff input is not trusted: " + "; ".join(trusted_import.issues))

        maximum_charge_power, maximum_discharge_power = _runtime_powers(solis_state)
        forecast = await _forecast_intervals(
            hass,
            config,
            now,
            reserve_end,
            trusted_import.intervals,
        )
        reserve = plan_reserve(
            intervals=forecast,
            capacity_kwh=config.battery.capacity_kwh,
            minimum_energy_kwh=config.battery.minimum_energy_kwh,
            reserve_margin_kwh=config.battery.reserve_margin_kwh,
            charge_efficiency=config.battery.charge_efficiency,
            discharge_efficiency=config.battery.discharge_efficiency,
            maximum_charge_power_kw=maximum_charge_power,
            maximum_discharge_power_kw=maximum_discharge_power,
        )
        if reserve.reserve_energy_kwh is None:
            raise ValueError("household reserve is unavailable: " + (reserve.issue or "unknown planner failure"))

        exact_reserve_soc = max(
            Decimal(MINIMUM_SOC_PERCENT),
            reserve.reserve_energy_kwh * Decimal(FULL_SOC_PERCENT) / config.battery.capacity_kwh,
        )
        telemetry = solis_state.telemetry
        if telemetry.state_of_charge_percent < Decimal(MINIMUM_SOC_PERCENT):
            raise ValueError("battery SOC is below the absolute safety floor")
        charge_state = solis_state.slots[0].charge
        cycle_slot_state = solis_state.slots[0].discharge
        reserve_slot_state = solis_state.slots[1].discharge
        reserve_capabilities = [solis_state.persistent.battery_reserve_soc]
        reserve_capabilities.extend(
            solis_state.direction(key).target_soc
            for key in config.solis.allocation(SlotOwner.RESERVE_EXPORT)
        )
        reserve_capabilities.extend(
            solis_state.direction(key).target_soc
            for key in config.solis.allocation(SlotOwner.FULL_SOC_CYCLING)
        )
        if any(capability is None for capability in reserve_capabilities):
            raise ValueError("reserve target capability is unavailable")
        control_reserve_soc = _common_quantize_target(
            exact_reserve_soc,
            tuple(capability for capability in reserve_capabilities if capability is not None),
        )
        control_reserve_energy = (
            config.battery.capacity_kwh
            * control_reserve_soc
            / Decimal(FULL_SOC_PERCENT)
        )

        charge_intent: SlotIntent | None = None
        effective_charge_lease_deadline = charge_lease_deadline
        cycle_intent: SlotIntent | None = None
        if current_window is not None:
            start = _slot_start(now, current_window.start)
            component_end, is_bonus = _charge_phase_end(current_window, now)
            charge_end = component_end
            if is_bonus:
                effective_charge_lease_deadline = _min_instant(
                    _min_instant(
                        effective_charge_lease_deadline or start + BONUS_CHARGE_LEASE_DURATION,
                        start + BONUS_CHARGE_LEASE_DURATION,
                    ),
                    component_end,
                )
                charge_end = _min_instant(charge_end, effective_charge_lease_deadline)
            elif charge_lease_deadline is not None:
                # A previously observed bonus lease is never extended by a
                # re-plan, even if the source reclassifies the interval.
                charge_end = _min_instant(charge_end, charge_lease_deadline)
            if _instant(charge_end) > _instant(start):
                charge_intent = _intent(
                    SlotOwner.CHEAP_CHARGING,
                    SlotDirection.CHARGE,
                    start,
                    charge_end,
                    charge_state.current.maximum,
                    min(Decimal(FULL_SOC_PERCENT), charge_state.target_soc.maximum),
                    charge_end,
                )
            cycle_start = start
            cycle_end = min(start + cycle_duration, current_window.end)
            if cycle_deadline is not None:
                cycle_end = min(cycle_deadline, current_window.end)
                cycle_start = cycle_end - cycle_duration
            if cycle_end > cycle_start:
                cycle_intent = _intent(
                    SlotOwner.FULL_SOC_CYCLING,
                    SlotDirection.DISCHARGE,
                    cycle_start,
                    cycle_end,
                    cycle_slot_state.current.maximum,
                    max(control_reserve_soc, cycle_slot_state.target_soc.minimum),
                    cycle_end,
                )

        reserve_intent: SlotIntent | None = None
        if (
            current_window is None
            and _reserve_export_allowed(telemetry.state_of_charge_percent, control_reserve_soc)
        ):
            reserve_intent = _intent(
                SlotOwner.RESERVE_EXPORT,
                SlotDirection.DISCHARGE,
                _minute_floor(now),
                reserve_end,
                reserve_slot_state.current.maximum,
                control_reserve_soc,
                reserve_end,
            )

        recharge = timedelta(0)
        if cycle_intent is not None:
            withdrawn = (
                maximum_discharge_power
                * Decimal(str(cycle_duration.total_seconds()))
                / Decimal(3600)
                / config.battery.discharge_efficiency
            )
            recharge_hours = withdrawn / (maximum_charge_power * config.battery.charge_efficiency)
            recharge = timedelta(seconds=float(recharge_hours * Decimal(3600)))

        choice = _select(
            _StrategyFacts(
                now=now,
                soc_percent=telemetry.state_of_charge_percent,
                reserve_soc_percent=control_reserve_soc,
                cheap_window=current_window,
                cheap_charge=charge_intent,
                reserve_discharge=reserve_intent,
                cycle_discharge=cycle_intent,
                recharge_duration=recharge,
                cycle_duration=cycle_duration,
                cycle_state=cycle_state,
                cycle_deadline=cycle_deadline,
            )
        )
        logical = None if choice.intent is None else LogicalIntent((choice.intent,))
        return Plan(
            action=choice.action,
            intent=logical,
            next_cycle_state=choice.cycle_state,
            cycle_deadline=choice.cycle_deadline,
            charge_lease_deadline=effective_charge_lease_deadline,
            current_cheap_window=current_window,
            next_cheap_window=next_window,
            reserve_soc_percent=control_reserve_soc,
            reserve_energy_kwh=reserve.reserve_energy_kwh,
            battery_energy_kwh=actual_energy,
            reserve_balance_kwh=actual_energy - reserve.reserve_energy_kwh,
            control_reserve_soc_percent=control_reserve_soc,
            control_reserve_energy_kwh=control_reserve_energy,
            control_reserve_balance_kwh=actual_energy - control_reserve_energy,
            maximum_charge_power_kw=maximum_charge_power,
            maximum_discharge_power_kw=maximum_discharge_power,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return Plan(
            action=StrategyAction.IDLE,
            intent=None,
            next_cycle_state=cycle_state,
            cycle_deadline=cycle_deadline,
            charge_lease_deadline=charge_lease_deadline,
            battery_energy_kwh=actual_energy,
            issue=f"{type(exc).__name__}: {exc}",
        )


def _select(facts: _StrategyFacts) -> _Choice:
    cheap = _window_active(facts.cheap_window, facts.now)
    if facts.cycle_state is CycleState.STOPPING:
        return _Choice(
            StrategyAction.IDLE if cheap else StrategyAction.RESERVE_FOLLOW,
            None,
            CycleState.IDLE,
            None,
        )

    if facts.cycle_state is CycleState.RESERVE_DISCHARGING:
        target = _effective_reserve(facts.reserve_soc_percent, facts.reserve_discharge)
        if not cheap and facts.reserve_discharge is not None and _reserve_export_allowed(facts.soc_percent, target):
            return _Choice(
                StrategyAction.RESERVE_DISCHARGE,
                _safe_intent(facts.reserve_discharge, target),
                CycleState.RESERVE_DISCHARGING,
                None,
            )
        return _Choice(StrategyAction.IDLE, None, CycleState.STOPPING, facts.cycle_deadline)

    if facts.cycle_state is CycleState.CHARGING:
        if cheap and facts.cheap_charge is not None and facts.soc_percent < Decimal(FULL_SOC_PERCENT):
            return _Choice(StrategyAction.CHEAP_CHARGE, facts.cheap_charge, CycleState.CHARGING, None)
        return _Choice(StrategyAction.IDLE, None, CycleState.STOPPING, facts.cycle_deadline)

    if facts.cycle_state is CycleState.CYCLE_DISCHARGING:
        if _cycle_can_continue(facts):
            assert facts.cycle_discharge is not None and facts.cheap_window is not None
            deadline = min(
                facts.cycle_deadline or facts.cycle_discharge.end,
                facts.cycle_discharge.end,
                facts.cheap_window.end,
            )
            bounded = replace(
                facts.cycle_discharge,
                end=deadline,
                expiry=min(facts.cycle_discharge.expiry, deadline),
            )
            return _Choice(
                StrategyAction.CYCLE_DISCHARGE,
                _safe_intent(bounded, facts.reserve_soc_percent),
                CycleState.CYCLE_DISCHARGING,
                deadline,
            )
        return _Choice(StrategyAction.IDLE, None, CycleState.STOPPING, facts.cycle_deadline)

    if cheap and facts.cheap_charge is not None and facts.soc_percent < Decimal(FULL_SOC_PERCENT):
        return _Choice(StrategyAction.CHEAP_CHARGE, facts.cheap_charge, CycleState.CHARGING, None)
    if _cycle_can_start(facts):
        assert facts.cycle_discharge is not None
        return _Choice(
            StrategyAction.CYCLE_DISCHARGE,
            _safe_intent(facts.cycle_discharge, facts.reserve_soc_percent),
            CycleState.CYCLE_DISCHARGING,
            facts.cycle_discharge.end,
        )
    target = _effective_reserve(facts.reserve_soc_percent, facts.reserve_discharge)
    if not cheap and facts.reserve_discharge is not None and _reserve_export_allowed(facts.soc_percent, target):
        return _Choice(
            StrategyAction.RESERVE_DISCHARGE,
            _safe_intent(facts.reserve_discharge, target),
            CycleState.RESERVE_DISCHARGING,
            None,
        )
    if cheap:
        return _Choice(StrategyAction.IDLE, None, CycleState.IDLE, None)
    # Normal load following is an active policy: Feed-In Priority with
    # Battery Reserve and Peak Shaving enabled, and no native slot.
    return _Choice(StrategyAction.RESERVE_FOLLOW, None, CycleState.IDLE, None)


def _window_active(window: CheapWindow | None, now: datetime) -> bool:
    return (
        window is not None
        and _instant(window.start) <= _instant(now) < _instant(window.end)
    )


def _window_has_bonus_at(window: CheapWindow | None, now: datetime) -> bool:
    if window is None:
        return False
    return any(
        item.rate_interval.classification is CheapClassification.BONUS_DISPATCH
        and _instant(item.interval.start) <= _instant(now) < _instant(item.interval.end)
        for item in window.components
    )


def _charge_phase_end(window: CheapWindow, now: datetime) -> tuple[datetime, bool]:
    """Return the end of the contiguous same-class phase containing now."""

    components = sorted(window.components, key=lambda item: _instant(item.interval.start))
    current_index = next(
        (
            index
            for index, item in enumerate(components)
            if _instant(item.interval.start) <= _instant(now) < _instant(item.interval.end)
        ),
        None,
    )
    if current_index is None:
        return window.end, False
    classification = components[current_index].rate_interval.classification
    end = components[current_index].interval.end
    if classification is CheapClassification.BONUS_DISPATCH:
        # A lease is tied to exactly one observed bonus component. Adjacent
        # bonus components may have different source/repricing semantics and
        # must never inherit one another's native boundary.
        return end, True
    for item in components[current_index + 1 :]:
        if (
            item.rate_interval.classification is not classification
            or _instant(item.interval.start) != _instant(end)
        ):
            break
        end = item.interval.end
    return end, classification is CheapClassification.BONUS_DISPATCH


def _effective_reserve(reserve_soc: Decimal, intent: SlotIntent | None) -> Decimal:
    return max(Decimal(MINIMUM_SOC_PERCENT), reserve_soc, Decimal(0) if intent is None else intent.target_soc)


def _reserve_export_allowed(soc_percent: Decimal, control_target: Decimal) -> bool:
    return soc_percent > control_target + RESERVE_SOC_UNCERTAINTY_PERCENT


def _safe_intent(intent: SlotIntent, minimum_soc: Decimal) -> SlotIntent:
    return intent if intent.target_soc >= minimum_soc else replace(intent, target_soc=minimum_soc)


def _cycle_can_start(facts: _StrategyFacts) -> bool:
    intent = facts.cycle_discharge
    if not _window_active(facts.cheap_window, facts.now) or intent is None or facts.cheap_charge is None:
        return False
    if facts.soc_percent < Decimal(FULL_SOC_PERCENT) or not intent.start <= facts.now < intent.end:
        return False
    assert facts.cheap_window is not None
    return facts.cheap_window.end - facts.now >= facts.cycle_duration + facts.recharge_duration


def _cycle_can_continue(facts: _StrategyFacts) -> bool:
    intent = facts.cycle_discharge
    if not _window_active(facts.cheap_window, facts.now) or intent is None:
        return False
    assert facts.cheap_window is not None
    deadline = min(facts.cycle_deadline or intent.end, intent.end, facts.cheap_window.end)
    if facts.now >= deadline or facts.soc_percent <= max(Decimal(MINIMUM_SOC_PERCENT), intent.target_soc):
        return False
    return facts.cheap_window.end - facts.now >= deadline - facts.now + facts.recharge_duration


def _intent(
    owner: SlotOwner,
    direction: SlotDirection,
    start: datetime,
    end: datetime,
    current: Decimal,
    target_soc: Decimal,
    expiry: datetime,
) -> SlotIntent:
    return SlotIntent(
        owner=owner,
        direction=direction,
        start=start.astimezone(timezone.utc),
        end=end.astimezone(timezone.utc),
        current=current,
        target_soc=target_soc,
        expiry=expiry.astimezone(timezone.utc),
    )


def _state(hass: Any, entity_id: str) -> Any:
    from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

    state = hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        raise ValueError(f"required entity is unavailable: {entity_id}")
    return state


def _attribute(state: Any, name: str) -> Any:
    from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

    value = state.attributes.get(name)
    if value in (None, "", STATE_UNKNOWN, STATE_UNAVAILABLE):
        raise ValueError(f"{state.entity_id} has no usable {name} attribute")
    return value


def _rate_source(state: Any, id_attr: str, retrieved_attr: str) -> RateSourceObservation:
    return RateSourceObservation(
        _parse_datetime(_attribute(state, retrieved_attr), retrieved_attr),
        _text(_attribute(state, id_attr), id_attr),
    )


def _dispatch_source(state: Any) -> DispatchSourceObservation:
    return DispatchSourceObservation(
        _parse_datetime(_attribute(state, "dispatch_source_last_retrieved"), "dispatch_source_last_retrieved"),
        _text(_attribute(state, "dispatch_source_entity_id"), "dispatch_source_entity_id"),
    )


def _runtime_powers(snapshot: Any) -> tuple[Decimal, Decimal]:
    voltage = snapshot.telemetry.battery_voltage_v
    charge_current = min(
        snapshot.slots[0].charge.current.maximum,
        snapshot.capabilities.maximum_charge_current.maximum,
    )
    discharge_current = min(
        snapshot.slots[0].discharge.current.maximum,
        snapshot.slots[1].discharge.current.maximum,
        snapshot.capabilities.maximum_discharge_current.maximum,
    )
    charge = voltage * charge_current / Decimal(1000)
    discharge = voltage * discharge_current / Decimal(1000)
    if not Decimal("0.5") <= charge <= Decimal("10") or not Decimal("0.5") <= discharge <= Decimal("10"):
        raise ValueError("derived runtime charge/discharge power is implausible")
    return charge, discharge


def _cycle_duration(state: Any) -> timedelta:
    try:
        value = Decimal(str(state.state))
    except (ArithmeticError, TypeError, ValueError):
        raise ValueError("cycle discharge duration is not numeric") from None
    if not value.is_finite() or value != value.to_integral_value() or not Decimal(1) <= value <= Decimal(60):
        raise ValueError("cycle discharge duration must be an integer from 1 to 60 minutes")
    return timedelta(minutes=int(value))


def _common_quantize_target(requested: Decimal, capabilities: Sequence[Any]) -> Decimal:
    """Return the smallest common native SOC supported by every target."""

    if not isinstance(requested, Decimal) or not requested.is_finite() or not capabilities:
        raise ValueError("reserve target capabilities are unavailable")
    decimals = [requested, Decimal(MINIMUM_SOC_PERCENT)]
    for capability in capabilities:
        values = (capability.minimum, capability.maximum, capability.step)
        if any(not isinstance(value, Decimal) or not value.is_finite() for value in values):
            raise ValueError("reserve target capability is invalid")
        if capability.step <= 0 or capability.minimum > capability.maximum:
            raise ValueError("reserve target capability is invalid")
        decimals.extend(values)
    scale = _decimal_scale(decimals)
    lower = max(
        _scale_decimal(requested, scale),
        _scale_decimal(Decimal(MINIMUM_SOC_PERCENT), scale),
        *(_scale_decimal(capability.minimum, scale) for capability in capabilities),
    )
    upper = min(_scale_decimal(capability.maximum, scale) for capability in capabilities)
    if lower > upper:
        raise ValueError("reserve target capabilities have no common representable SOC")

    first = capabilities[0]
    residue = _scale_decimal(first.minimum, scale) % _scale_decimal(first.step, scale)
    modulus = _scale_decimal(first.step, scale)
    for capability in capabilities[1:]:
        residue, modulus = _merge_congruences(
            residue,
            modulus,
            _scale_decimal(capability.minimum, scale),
            _scale_decimal(capability.step, scale),
        )
    candidate = residue
    if candidate < lower:
        candidate += ((lower - candidate + modulus - 1) // modulus) * modulus
    if candidate > upper or any(
        (candidate - _scale_decimal(capability.minimum, scale))
        % _scale_decimal(capability.step, scale)
        != 0
        for capability in capabilities
    ):
        raise ValueError("reserve target capabilities have no common representable SOC")
    return Decimal(candidate) / Decimal(scale)


def _decimal_scale(values: Sequence[Decimal]) -> int:
    places = max(0, *(-value.as_tuple().exponent for value in values))
    return 10 ** places


def _scale_decimal(value: Decimal, scale: int) -> int:
    return int(value * scale)


def _merge_congruences(
    residue: int,
    modulus: int,
    other_residue: int,
    other_modulus: int,
) -> tuple[int, int]:
    common = gcd(modulus, other_modulus)
    difference = other_residue - residue
    if difference % common:
        raise ValueError("reserve target capabilities have no common representable SOC")
    left = modulus // common
    right = other_modulus // common
    offset = 0 if right == 1 else (difference // common * pow(left, -1, right)) % right
    combined = lcm(modulus, other_modulus)
    return (residue + modulus * offset) % combined, combined


async def _forecast_intervals(
    hass: Any,
    config: Any,
    start: datetime,
    end: datetime,
    rates: Sequence[AdjustedRateInterval],
) -> tuple[ReserveInputInterval, ...]:
    from homeassistant.components.forecast_solar.energy import async_get_solar_forecast
    from homeassistant.util import dt as dt_util

    zone = dt_util.get_time_zone(hass.config.time_zone)
    if zone is None:
        raise ValueError("Home Assistant timezone is invalid")
    load_items = forecast_load(now=start, horizon_end=end, timezone=zone)
    solar_items = _solar_intervals(await async_get_solar_forecast(hass, config.solar.config_entry_id))
    boundaries = {start, end}
    for items in (load_items, solar_items, rates):
        for item in items:
            interval = item.interval if hasattr(item, "interval") else TimeInterval(item.start, item.end)
            if start < interval.start < end:
                boundaries.add(interval.start)
            if start < interval.end < end:
                boundaries.add(interval.end)
    ordered_boundaries = sorted(boundaries)
    result: list[ReserveInputInterval] = []
    for left, right in zip(ordered_boundaries, ordered_boundaries[1:]):
        interval = TimeInterval(left, right)
        rate = next((item for item in rates if item.start <= left and item.end >= right), None)
        if rate is None:
            raise ValueError("import tariff does not cover the reserve interval")
        result.append(ReserveInputInterval(
            interval,
            prorated_energy(interval, load_items, required=True),
            prorated_energy(interval, solar_items, required=False),
            rate.classification,
        ))
    return tuple(result)


def _solar_intervals(raw: Mapping[str, Any] | None) -> tuple[EnergyInterval, ...]:
    if not raw or not isinstance(raw.get("wh_hours"), Mapping):
        raise ValueError("Forecast.Solar response is unavailable")
    periods = sorted(
        (_parse_datetime(str(stamp), "solar forecast timestamp"), Decimal(str(value)))
        for stamp, value in raw["wh_hours"].items()
    )
    if len(periods) < 2:
        raise ValueError("Forecast.Solar returned too few periods")
    result: list[EnergyInterval] = []
    for index, (start, wh) in enumerate(periods):
        end = periods[index + 1][0] if index + 1 < len(periods) else start + (start - periods[index - 1][0])
        result.append(EnergyInterval(TimeInterval(start, end), wh / Decimal(1000)))
    return tuple(result)


def _actual_battery_energy(config: Any, solis_state: Any) -> Decimal | None:
    try:
        soc = solis_state.telemetry.state_of_charge_percent
        if not isinstance(soc, Decimal) or not soc.is_finite():
            return None
        return config.battery.capacity_kwh * soc / Decimal(FULL_SOC_PERCENT)
    except (AttributeError, TypeError, ValueError):
        return None


def _minute_floor(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0)


def _slot_start(now: datetime, window_start: datetime) -> datetime:
    start = _minute_floor(now)
    if window_start > now:
        start = _minute_floor(window_start)
        if start < window_start:
            start += timedelta(minutes=1)
    return start


__all__ = [
    "AdjustedRateInterval", "BONUS_CHARGE_LEASE_DURATION", "CheapClassification", "CheapWindow",
    "CheapWindowComponent", "CheapWindowResult", "CoverageStatus",
    "DispatchSourceObservation", "EnergyInterval", "ExportRateInterval",
    "LogicalIntent", "Plan", "RateSourceObservation", "ReserveInputInterval", "ReservePlanResult",
    "TimeInterval", "TrustedImportResult", "evaluate_cheap_windows",
    "evaluate_trusted_import_rates", "forecast_load", "parse_fused_export_rates",
    "parse_fused_import_rates", "plan_reserve", "prorated_energy", "build_plan",
    "RESERVE_SOC_UNCERTAINTY_PERCENT", "_common_quantize_target",
]
