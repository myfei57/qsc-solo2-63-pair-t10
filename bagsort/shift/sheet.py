"""Shift reports reconciled against the in-process inventory and sort records.

Every figure here is recomputed from the committed journal rather than from
in-memory counters, so the inventory, the sort records and the shift report can
be checked against one another after a restart or an audit replay.
"""

from __future__ import annotations

from typing import Any

from bagsort.model.enums import CustodyPolicy, HandoverState, ShiftState
from bagsort.store.store import Store
from .custody import ShiftCustody, inventory_digest, sort_counts_from_journal


class ShiftSheet:
    """The shift report projection and the three-way reconciliation."""

    def __init__(self, store: Store, custody: ShiftCustody) -> None:
        self._store = store
        self._custody = custody

    def sort_counts(self) -> dict[str, int]:
        counts = sort_counts_from_journal(self._store.entries())
        return {key: counts[key] for key in sorted(counts)}

    def sort_count_for(self, shift_id: str) -> int:
        return self.sort_counts().get(shift_id, 0)

    def shift_report(self, shift_id: str) -> dict[str, Any]:
        state = self._store.state()
        shift = state.shifts[shift_id]
        handed_out: list[dict[str, Any]] = []
        received: list[dict[str, Any]] = []
        for handover in sorted(state.handovers.values(), key=lambda item: item.id):
            if handover.state is not HandoverState.CONFIRMED:
                continue
            summary = {
                "handover_id": handover.id,
                "policy": handover.policy.value,
                "bags": len(handover.items),
                "confirmed_at": handover.confirmed_at,
            }
            if handover.outgoing_shift_id == shift_id:
                handed_out.append(summary)
            if (
                handover.incoming_shift_id == shift_id
                and handover.policy is CustodyPolicy.TRANSFER
            ):
                received.append(summary)
        return {
            "shift_id": shift.id,
            "code": shift.code,
            "state": shift.state.value,
            "opened_at": shift.opened_at,
            "closed_at": shift.closed_at,
            "active_bags": len(self._custody.active_items(shift_id)),
            "sort_records": self.sort_count_for(shift_id),
            "handed_out": handed_out,
            "received": received,
            "handovers": len(handed_out) + len(received),
        }

    def reports(self) -> list[dict[str, Any]]:
        return [self.shift_report(shift_id) for shift_id in sorted(self._store.state().shifts)]

    def reconcile(self) -> dict[str, Any]:
        """Check the inventory, the sort records and the shift reports agree."""

        state = self._store.state()
        known_shifts = set(state.shifts)

        # Every active bag has exactly one owner: the mapping holds one shift id
        # per bag, it must point at a registered shift, and no active bag may be
        # unowned while the line is mid-handover bookkeeping.
        orphan_owners = sorted(
            bag_id for bag_id, shift_id in state.bag_owners.items() if shift_id not in known_shifts
        )
        active = {bag.id for bag in state.bags.values() if bag.state.active}
        owned_active = sorted(bag_id for bag_id in state.bag_owners if bag_id in active)
        unowned_active = sorted(active - set(state.bag_owners))

        # Every frozen handover inventory still hashes to its recorded digest.
        inventories_intact = True
        handover_checks: list[dict[str, Any]] = []
        for handover in sorted(state.handovers.values(), key=lambda item: item.id):
            intact = inventory_digest(handover.items) == handover.inventory_digest
            inventories_intact = inventories_intact and intact
            handover_checks.append(
                {"handover_id": handover.id, "state": handover.state.value, "intact": intact}
            )

        # Sort records reproduce the shift report: attributed plus unattributed
        # records must equal the committed sort record total.
        counts = self.sort_counts()
        attributed = sum(counts.values())
        unattributed = counts.get("", 0)
        sort_total = len(state.sort_records)
        reports = self.reports()
        report_total = sum(int(row["sort_records"]) for row in reports)
        sort_reproduces = attributed == sort_total and report_total == sort_total - unattributed

        # Active counts in the reports equal the live ownership projection.
        live_total = sum(
            1 for bag in state.bags.values() if bag.state.active and bag.id in state.bag_owners
        )
        report_active_total = sum(int(row["active_bags"]) for row in reports)
        report_consistent = report_active_total == live_total

        consistent = (
            not orphan_owners
            and not unowned_active
            and inventories_intact
            and sort_reproduces
            and report_consistent
        )
        return {
            "consistent": consistent,
            "active_bags": len(active),
            "owned_active_bags": len(owned_active),
            "unowned_active_bags": unowned_active,
            "owners_unknown_shift": orphan_owners,
            "sort_records_total": sort_total,
            "sort_records_unattributed": unattributed,
            "sort_records_reproduce_reports": sort_reproduces,
            "reports_reproduce_inventory": report_consistent,
            "handovers": handover_checks,
            "open_shift": shift.id if (shift := self._current_open()) is not None else "",
        }

    def _current_open(self):
        opens = [
            shift
            for shift in self._store.state().shifts.values()
            if shift.state is ShiftState.OPEN
        ]
        return sorted(opens, key=lambda shift: (shift.opened_at, shift.id))[-1] if opens else None
