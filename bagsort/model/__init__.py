"""Domain entities, enumerations, validation and command folding."""

from __future__ import annotations

from .entities import (
    AuditEntry,
    Bag,
    ChuteAssignment,
    Flight,
    Handover,
    HandoverItem,
    LoadRecord,
    Reading,
    Sequence,
    Shift,
    SortRecord,
    TransferCommand,
)
from .enums import BagState, CustodyPolicy, FlightState, HandoverState, ShiftState, Verdict
from .state import State, apply_entry, fold
from .validate import (
    is_active_bag_state,
    is_open_state,
    require_text,
    validate_bag,
    validate_flight,
    validate_reading,
    validate_transfer,
)

__all__ = [
    "AuditEntry",
    "Bag",
    "BagState",
    "ChuteAssignment",
    "CustodyPolicy",
    "Flight",
    "FlightState",
    "Handover",
    "HandoverItem",
    "HandoverState",
    "LoadRecord",
    "Reading",
    "Sequence",
    "Shift",
    "ShiftState",
    "SortRecord",
    "State",
    "TransferCommand",
    "Verdict",
    "apply_entry",
    "fold",
    "is_active_bag_state",
    "is_open_state",
    "require_text",
    "validate_bag",
    "validate_flight",
    "validate_reading",
    "validate_transfer",
]
