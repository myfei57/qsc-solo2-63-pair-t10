"""Shift custody: which shift an in-process bag currently belongs to."""

from __future__ import annotations

import hashlib
import json

from bagsort.model.entities import HandoverItem
from bagsort.store.store import Store


class ShiftCustody:
    """The bag-to-shift ownership projection and the in-process inventory."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def owner_of(self, bag_id: str) -> str:
        return self._store.state().bag_owners.get(bag_id, "")

    def bags_owned_by(self, shift_id: str) -> list[str]:
        return sorted(
            bag_id
            for bag_id, owner in self._store.state().bag_owners.items()
            if owner == shift_id
        )

    def assign(self, bag_id: str, shift_id: str) -> None:
        """Bind one bag to a shift. The journal is the only place this is written."""

        with self._store.session() as session:
            session.state.bag_owners[bag_id] = shift_id
            session.record(
                "bag.owner.put",
                {"bag_id": bag_id, "shift_id": shift_id},
            )

    def active_items(self, shift_id: str) -> list[HandoverItem]:
        """The in-process bags currently attributed to a shift, in id order."""

        state = self._store.state()
        items: list[HandoverItem] = []
        for bag_id in sorted(state.bag_owners):
            if self.owner_of(bag_id) != shift_id:
                continue
            bag = state.bags.get(bag_id)
            if bag is None or not bag.state.active:
                continue
            items.append(
                HandoverItem(
                    bag_id=bag.id,
                    flight_id=bag.flight_id,
                    state=bag.state.value,
                    chute_id=bag.chute_id,
                    owner_shift_id=shift_id,
                )
            )
        return items

    def unassigned_active_bags(self) -> list[str]:
        """Active bags that no shift currently owns."""

        state = self._store.state()
        owners = state.bag_owners
        return sorted(
            bag.id
            for bag in state.bags.values()
            if bag.state.active and bag.id not in owners
        )


def inventory_digest(items: list[HandoverItem] | tuple[HandoverItem, ...]) -> str:
    """Deterministic digest over a frozen handover inventory."""

    material = [
        [item.bag_id, item.flight_id, item.state, item.chute_id, item.owner_shift_id]
        for item in sorted(items, key=lambda item: item.bag_id)
    ]
    raw = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def sort_counts_from_journal(entries) -> dict[str, int]:
    """Attribute every sort record to the shift that owned its bag at commit time.

    The fold replays ownership changes in journal order, so the attribution is
    exactly what the shift report must reproduce, even after confirmations or
    rollbacks later move the same bags to another shift.
    """

    owners: dict[str, str] = {}
    sort_counts: dict[str, int] = {}
    for entry in entries:
        if entry.kind == "bag.owner.put":
            bag_id = str(entry.payload.get("bag_id", ""))
            shift_id = str(entry.payload.get("shift_id", ""))
            if shift_id:
                owners[bag_id] = shift_id
            else:
                owners.pop(bag_id, None)
        elif entry.kind == "bag.remove":
            owners.pop(str(entry.payload.get("bag_id", "")), None)
        elif entry.kind == "sort.record":
            record = entry.payload.get("record", {})
            shift_id = owners.get(str(record.get("bag_id", "")), "")
            sort_counts[shift_id] = sort_counts.get(shift_id, 0) + 1
    return sort_counts
