"""Time interval types."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class TimeInterval:
    """Represents a half-open time interval."""

    start: datetime
    """Inclusive, timezone-aware start."""

    end: datetime
    """Exclusive, timezone-aware end."""
