"""Enumerations for the two state machines the plant tracks."""

from __future__ import annotations

from enum import Enum


class FlightState(str, Enum):
    """Lifecycle of one flight batch."""

    OPEN = "open"
    BOARDING = "boarding"
    CUTOFF = "cutoff"
    CLOSED = "closed"


class BagState(str, Enum):
    """Lifecycle of one piece of baggage."""

    CHECKED_IN = "checked_in"
    INJECTED = "injected"
    SORTED = "sorted"
    RECHECKED = "rechecked"
    LOADED = "loaded"
    DIVERTED = "diverted"

    @property
    def active(self) -> bool:
        """Whether the bag still occupies plant capacity."""

        return self in (
            BagState.CHECKED_IN,
            BagState.INJECTED,
            BagState.SORTED,
            BagState.RECHECKED,
        )

class Verdict(str, Enum):
    """Result of comparing an observed (epoch, sequence) pair with the high-water mark."""

    FRESH = "fresh"
    REPLAYED = "replayed"
    ROLLED_OVER = "rolled_over"


class ShiftState(str, Enum):
    """Lifecycle of one working shift."""

    OPEN = "open"
    CLOSED = "closed"

    @property
    def active(self) -> bool:
        return self is ShiftState.OPEN


class HandoverState(str, Enum):
    """Lifecycle of one shift handover.

    A handover only counts as completed once the receiving shift confirms it.
    Until then the proposed handover may be rolled back, which restores the
    ownership exactly as it was before the proposal.
    """

    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    ROLLED_BACK = "rolled_back"


class CustodyPolicy(str, Enum):
    """Who answers for the in-process bags at the moment of a handover.

    The two policies have opposite effects on responsibility tracing, so every
    handover records the chosen policy together with an explicit clause that
    states how responsibility is divided.
    """

    RELINQUISH = "relinquish"
    TRANSFER = "transfer"
