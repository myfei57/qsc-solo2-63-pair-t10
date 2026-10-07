"""Re-derive bag ownership from the handover timeline alone.

The materialised ``shift_id`` on a bag is a convenience index; the handover
history is the source of truth. This module replays that history without
reading any bag's current ownership, which is what lets the console prove the
two views agree and lets any shift's handover be recomputed after a restart.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Iterable

from .policy import RETAIN, TRANSFER

if TYPE_CHECKING:  # pragma: no cover - typing only
    from bagsort.model.entities import Handover
    from bagsort.model.state import State


def ownership_from_history(
    handovers: Iterable["Handover"], state: "State | None" = None
) -> dict[str, str]:
    """Map every affected bag to the shift the completed handovers leave it on.

    Completed ``transfer`` handovers move the frozen manifest to the receiving
    shift; completed ``retain`` handovers pin it to the outgoing shift. A later
    rollback simply leaves the bags where they were before the interrupted
    handover opened, so rolled-back and still-open handovers change nothing.
    """

    ownership: dict[str, str] = {}
    for handover in sorted(handovers, key=lambda item: item.id):
        if handover.status != "completed":
            continue
        target = (
            handover.shift_to if handover.policy == TRANSFER else handover.shift_from
        )
        for item in handover.items:
            # Only bags frozen under the outgoing shift are part of the deal;
            # a manifest also lists active bags other shifts own, untouched.
            if item.shift_id == handover.shift_from:
                ownership[item.bag_id] = target
    return ownership


def recompute_for_shift(
    handovers: Iterable["Handover"], shift_id: str
) -> dict[str, object]:
    """Reproduce one shift's handover ledger purely from committed history."""

    timeline = [
        handover
        for handover in sorted(handovers, key=lambda item: item.id)
        if handover.shift_from == shift_id or handover.shift_to == shift_id
    ]
    received: dict[str, list[str]] = defaultdict(list)
    kept: dict[str, list[str]] = defaultdict(list)
    returned: dict[str, list[str]] = defaultdict(list)
    for handover in timeline:
        bags = [
            item.bag_id
            for item in handover.items
            if item.shift_id == handover.shift_from
        ]
        if handover.status == "completed":
            if handover.policy == TRANSFER and handover.shift_to == shift_id:
                received[handover.id].extend(bags)
            if handover.policy == RETAIN and handover.shift_from == shift_id:
                kept[handover.id].extend(bags)
        elif handover.status == "rolled_back" and handover.shift_from == shift_id:
            returned[handover.id].extend(bags)
    return {
        "shift_id": shift_id,
        "handovers": [handover.id for handover in timeline],
        "received": {key: sorted(value) for key, value in sorted(received.items())},
        "retained": {key: sorted(value) for key, value in sorted(kept.items())},
        "returned": {key: sorted(value) for key, value in sorted(returned.items())},
    }


class HandoverHistory:
    """Read-only accessors over committed handovers."""

    def __init__(self, store: object) -> None:
        self._store = store

    def list(self) -> list["Handover"]:
        return [
            self._store.state().handovers[key]
            for key in sorted(self._store.state().handovers)
        ]

    def for_shift(self, shift_id: str) -> list["Handover"]:
        return [
            handover
            for handover in self.list()
            if handover.shift_from == shift_id or handover.shift_to == shift_id
        ]

    def ownership(self) -> dict[str, str]:
        return ownership_from_history(self.list())

    def recompute(self, shift_id: str) -> dict[str, object]:
        return recompute_for_shift(self.list(), shift_id)
