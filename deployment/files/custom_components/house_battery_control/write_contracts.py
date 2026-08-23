"""Home Assistant-free contracts for verified entity writes.

The contracts in this module deliberately describe a request and its result but
do not know how Home Assistant stores state or executes services.  The writer
adapter is responsible for the best-effort compare-and-set and readback proof.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from collections.abc import Mapping
from typing import Callable, Literal

from .contracts import ObservedCapability


HAWriteDomain = Literal["switch", "select", "number", "text", "datetime"]


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class StatePrecondition:
    """The exact HA revision a caller is willing to replace."""

    entity_id: str
    state: str
    last_updated: datetime
    context_id: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.entity_id, str) or not self.entity_id:
            raise ValueError("entity_id must not be empty")
        if not isinstance(self.state, str):
            raise TypeError("state must be a string")
        _aware(self.last_updated, "last_updated")
        if self.context_id is not None and not isinstance(self.context_id, str):
            raise TypeError("context_id must be a string or None")


TextValidator = Callable[[str], bool]


@dataclass(frozen=True, slots=True)
class WriteRequest:
    """A typed, domain-aware mutation request.

    ``target`` is checked against ``domain`` by the writer.  Keeping this as a
    single immutable value makes transaction ordering and diagnostics uniform,
    while the factory subclasses below provide convenient typed constructors.
    """

    precondition: StatePrecondition
    target: object
    capability: ObservedCapability | None = None
    text_validator: TextValidator | None = None
    domain: HAWriteDomain = "switch"

    def __post_init__(self) -> None:
        if self.domain not in {"switch", "select", "number", "text", "datetime"}:
            raise ValueError(f"unsupported Home Assistant domain: {self.domain!r}")
        if self.domain == "number" and self.capability is None:
            raise ValueError("number requests require the observed capability")
        if self.domain != "number" and self.capability is not None:
            raise ValueError("capability is only valid for number requests")
        if self.domain != "text" and self.text_validator is not None:
            raise ValueError("text_validator is only valid for text requests")

    @property
    def entity_id(self) -> str:
        return self.precondition.entity_id


@dataclass(frozen=True, slots=True)
class SwitchWriteRequest(WriteRequest):
    domain: Literal["switch"] = "switch"

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.target, bool):
            raise TypeError("switch target must be bool")


@dataclass(frozen=True, slots=True)
class SelectWriteRequest(WriteRequest):
    domain: Literal["select"] = "select"

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.target, str):
            raise TypeError("select target must be a string")


@dataclass(frozen=True, slots=True)
class NumberWriteRequest(WriteRequest):
    domain: Literal["number"] = "number"

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.target, Decimal):
            raise TypeError("number target must be Decimal")


@dataclass(frozen=True, slots=True)
class TextWriteRequest(WriteRequest):
    domain: Literal["text"] = "text"

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.target, str):
            raise TypeError("text target must be a string")
        if self.text_validator is not None and not callable(self.text_validator):
            raise TypeError("text_validator must be callable")


@dataclass(frozen=True, slots=True)
class DatetimeWriteRequest(WriteRequest):
    domain: Literal["datetime"] = "datetime"

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.target, datetime):
            raise TypeError("datetime target must be datetime")
        _aware(self.target, "datetime target")


class WriteOutcome(str, Enum):
    NO_CHANGE = "no_change"
    APPLIED_HA_READBACK = "applied_ha_readback"
    CONFLICT = "conflict"
    REJECTED = "rejected"
    SERVICE_ERROR = "service_error"
    SERVICE_TIMEOUT = "service_timeout"
    READBACK_TIMEOUT = "readback_timeout"


class TransactionStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL_FAILURE = "partial_failure"


@dataclass(frozen=True, slots=True)
class WriteResult:
    """Result for one entity; HA readback is explicitly not device proof."""

    entity_id: str
    outcome: WriteOutcome
    message: str = ""
    service: str | None = None
    service_data: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if self.service_data is not None:
            object.__setattr__(self, "service_data", MappingProxyType(dict(self.service_data)))

    @property
    def success(self) -> bool:
        return self.outcome in {WriteOutcome.NO_CHANGE, WriteOutcome.APPLIED_HA_READBACK}

@dataclass(frozen=True, slots=True)
class TransactionResult:
    """Ordered, non-atomic results from one writer transaction."""

    results: tuple[WriteResult, ...]
    status: TransactionStatus
    complete: bool = True

__all__ = [
    "DatetimeWriteRequest",
    "HAWriteDomain",
    "NumberWriteRequest",
    "SelectWriteRequest",
    "StatePrecondition",
    "SwitchWriteRequest",
    "TextWriteRequest",
    "TransactionResult",
    "TransactionStatus",
    "WriteOutcome",
    "WriteRequest",
    "WriteResult",
]
