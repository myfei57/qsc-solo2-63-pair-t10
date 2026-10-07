"""Handover history rebuilt from the handover commands in the journal.

The journal holds every proposal, confirmation and rollback for the same
handover id in commit order, so replaying them for one shift reproduces exactly
what that shift saw during its handovers.
"""

from __future__ import annotations

from bagsort.model.entities import Handover
from bagsort.store.store import Store


class HandoverHistory:
    """Every version of every handover, in commit order."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def all(self) -> list[Handover]:
        return [
            Handover.from_dict(entry.payload["handover"])
            for entry in self._store.entries()
            if entry.kind == "handover.put"
        ]

    def versions(self, handover_id: str) -> list[Handover]:
        return [
            handover for handover in self.all() if handover.id == handover_id
        ]

    def for_shift(self, shift_id: str) -> list[Handover]:
        return [
            handover
            for handover in self.all()
            if shift_id in (handover.outgoing_shift_id, handover.incoming_shift_id)
        ]

    def recompute(self, shift_id: str) -> dict[str, object]:
        """Fold the handover journal for a shift back into its current picture."""

        current: dict[str, Handover] = {}
        for handover in self.for_shift(shift_id):
            current[handover.id] = handover
        proposed = [h.id for h in current.values() if h.state.value == "proposed"]
        confirmed = [h.id for h in current.values() if h.state.value == "confirmed"]
        rolled_back = [h.id for h in current.values() if h.state.value == "rolled_back"]
        return {
            "shift_id": shift_id,
            "handovers": sorted(current),
            "proposed": sorted(proposed),
            "confirmed": sorted(confirmed),
            "rolled_back": sorted(rolled_back),
            "matches_state": current
            == {
                key: value
                for key, value in self._store.state().handovers.items()
                if shift_id in (value.outgoing_shift_id, value.incoming_shift_id)
            },
        }
