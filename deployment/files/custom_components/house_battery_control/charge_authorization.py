"""Household charging permission, independent of scheduled Octopus prices."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

UTC = timezone.utc
LONDON = ZoneInfo("Europe/London")
POWER_THRESHOLD_W = Decimal("50")
MAX_POWER_AGE = timedelta(minutes=2)
MAX_SMART_AGE = timedelta(minutes=5)


def instant(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("authorization timestamps must be timezone-aware")
    return value.astimezone(UTC)


def settlement_start(now: datetime) -> datetime:
    now = instant(now)
    return now.replace(minute=30 * (now.minute // 30), second=0, microsecond=0)


@dataclass(frozen=True, slots=True)
class QualifiedHalfHour:
    start: datetime
    end: datetime
    qualified_at: datetime
    power_reported_at: datetime
    smart_retrieved_at: datetime


@dataclass(frozen=True, slots=True)
class ChargeAuthorization:
    now: datetime
    qualified: QualifiedHalfHour | None = None

    def authorized_until(self, start: datetime) -> datetime | None:
        """End of contiguous permission containing start, including adjacency."""
        start = instant(start)
        day = start.astimezone(LONDON).date()
        intervals = []
        for offset in (-1, 0, 1):
            evening = day + timedelta(days=offset)
            intervals.append((
                datetime.combine(evening, time(23, 30), LONDON).astimezone(UTC),
                datetime.combine(evening + timedelta(days=1), time(5, 30), LONDON).astimezone(UTC),
            ))
        q = self.qualified
        if q is not None and q.start <= instant(self.now) < q.end:
            intervals.append((q.start, q.end))
        cursor = start
        for left, right in sorted(intervals):
            if left <= cursor < right:
                cursor = right
        return cursor if cursor > start else None

    def charge_is_authorized(self, start: datetime, end: datetime) -> bool:
        start, end = instant(start), instant(end)
        cutoff = self.authorized_until(start)
        return start < end and cutoff is not None and end <= cutoff


class ChargeGuard:
    """One transient current-period latch; never restored from a bare boolean."""

    def __init__(self, started_at: datetime) -> None:
        self.started_at = instant(started_at)
        self.qualified: QualifiedHalfHour | None = None
        self.reason = "waiting_for_current_period_evidence"
        self.power_w: Decimal | None = None
        self.power_reported_at: datetime | None = None
        self.smart_state: str | None = None
        self.smart_retrieved_at: datetime | None = None

    def observe(self, hass: object, config: object, now: datetime) -> ChargeAuthorization:
        now = instant(now)
        period = settlement_start(now)
        if self.qualified is not None and self.qualified.start != period:
            self.qualified = None
        power = hass.states.get(config.ev_power_entity_id)
        smart = hass.states.get(config.intelligent_state_entity_id)
        retrieved = hass.states.get(config.dispatches_retrieved_entity_id)
        self.smart_state = getattr(smart, "state", None)
        self.power_reported_at = _timestamp(getattr(power, "last_reported", None))
        self.smart_retrieved_at = _timestamp(getattr(retrieved, "state", None))
        try:
            value = Decimal(getattr(power, "state", "unknown"))
            self.power_w = value if value.is_finite() else None
        except (InvalidOperation, TypeError, ValueError):
            self.power_w = None
        valid_power = self._fresh(self.power_reported_at, now, period, MAX_POWER_AGE)
        valid_smart = self._fresh(self.smart_retrieved_at, now, period, MAX_SMART_AGE)
        # Restored values and an API timestamp accompanied by a failed refresh
        # cannot mint permission. Already latched historical evidence survives.
        restored = any(getattr(s, "attributes", {}).get("restored", False) for s in (power, smart, retrieved))
        error = getattr(retrieved, "attributes", {}).get("last_error")
        if self.qualified is None:
            if restored or not valid_power or not valid_smart or error not in (None, "None", ""):
                self.reason = "waiting_for_current_period_evidence"
            elif self.smart_state != "SMART_CONTROL_IN_PROGRESS":
                self.reason = "not_smart_control"
            elif self.power_w is None or self.power_w <= POWER_THRESHOLD_W:
                self.reason = "ev_not_charging"
            else:
                self.qualified = QualifiedHalfHour(
                    period, period + timedelta(minutes=30), now,
                    self.power_reported_at, self.smart_retrieved_at,
                )
        snapshot = ChargeAuthorization(now, self.qualified)
        if ChargeAuthorization(now).authorized_until(now) is not None:
            self.reason = "static_off_peak"
        elif self.qualified is not None:
            self.reason = "qualified_smart_charge"
        return snapshot

    def _fresh(self, timestamp: datetime | None, now: datetime, period: datetime, age: timedelta) -> bool:
        return timestamp is not None and max(period, self.started_at) <= timestamp <= now and now - timestamp <= age

    def diagnostics(self, now: datetime) -> dict[str, object]:
        def iso(value: datetime | None) -> str | None:
            return None if value is None else value.isoformat()
        q = self.qualified
        return {
            "charge_authorization_reason": self.reason,
            "charge_authorized_until": iso(ChargeAuthorization(now, q).authorized_until(now)),
            "settlement_start": iso(settlement_start(now)),
            "settlement_end": iso(settlement_start(now) + timedelta(minutes=30)),
            "qualified_at": iso(None if q is None else q.qualified_at),
            "ev_power_w": None if self.power_w is None else float(self.power_w),
            "ev_power_reported_at": iso(self.power_reported_at),
            "intelligent_state": self.smart_state,
            "dispatches_retrieved_at": iso(self.smart_retrieved_at),
        }


def _timestamp(value: object) -> datetime | None:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return instant(parsed)
    except (TypeError, ValueError):
        return None
