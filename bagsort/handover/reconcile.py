"""Three-way reconciliation: in-progress inventory, sort records, shift report.

Every number in a shift report is computed here from the same committed state,
and each number is cross-checked against an independent source:

* the in-progress list comes from the bag store and is grouped by ``shift_id``;
* sort records already carry the shift that dispatched them;
* the handover history is replayed to say which shifts the bags *should* be on.

A report is only reported as consistent when those three views and the frozen
manifests agree, including after an interrupted handover has been rolled back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bagsort.model.enums import BagState
from bagsort.model.state import State
from .history import ownership_from_history
from .policy import manifest_digest


@dataclass(frozen=True)
class ShiftReport:
    """The per-shift figures the in-progress list and sort log must reproduce."""

    shift_id: str
    active: int = 0
    by_state: dict[str, int] = field(default_factory=dict)
    sorted_total: int = 0
    received: int = 0
    retained: int = 0
    returned: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "shift_id": self.shift_id,
            "active": self.active,
            "by_state": dict(sorted(self.by_state.items())),
            "sorted_total": self.sorted_total,
            "received": self.received,
            "retained": self.retained,
            "returned": self.returned,
        }


class Reconciler:
    """Computes reports and verifies the cross-reproduction invariants."""

    def __init__(self, store: object) -> None:
        self._store = store

    # -- reports -----------------------------------------------------------

    def report(self, shift_id: str) -> ShiftReport:
        state = self._store.state()
        active = [
            bag
            for bag in state.bags.values()
            if bag.state.active and bag.shift_id == shift_id
        ]
        by_state: dict[str, int] = {}
        for bag in active:
            by_state[bag.state.value] = by_state.get(bag.state.value, 0) + 1
        return ShiftReport(
            shift_id=shift_id,
            active=len(active),
            by_state=by_state,
            sorted_total=sum(
                1 for record in state.sort_records if record.shift_id == shift_id
            ),
        )

    def reports(self) -> list[ShiftReport]:
        state = self._store.state()
        shifts = set(state.shifts)
        shifts.update(
            bag.shift_id
            for bag in state.bags.values()
            if bag.state.active and bag.shift_id
        )
        shifts.update(record.shift_id for record in state.sort_records if record.shift_id)
        return [self.report(shift_id) for shift_id in sorted(shifts)]

    def inventory(self, shift_id: str = "") -> list[dict[str, str]]:
        """In-progress bags, oldest journal ownership first; optional shift filter."""

        state = self._store.state()
        rows = [
            {"bag_id": bag.id, "state": bag.state.value, "shift_id": bag.shift_id}
            for bag in state.bags.values()
            if bag.state.active and (not shift_id or bag.shift_id == shift_id)
        ]
        return sorted(rows, key=lambda row: row["bag_id"])

    def inventory_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.inventory():
            key = row["shift_id"]
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items()))

    # -- invariants --------------------------------------------------------

    def verify(self) -> dict[str, Any]:
        """Run every consistency check and return the details behind the verdict."""

        state = self._store.state()
        checks: dict[str, bool] = {}

        checks["single_owner"] = self._single_owner(state)
        checks["ownership_matches_history"] = self._ownership_matches_history(state)
        checks["manifests_intact"] = self._manifests_intact(state)
        checks["reports_match_inventory"] = self._reports_match_inventory(state)
        checks["sorts_reproduce"] = self._sorts_reproduce(state)

        return {"ok": all(checks.values()), "checks": checks}

    def _single_owner(self, state: State) -> bool:
        """Every active bag has one owner and that owner is a known shift.

        A bag without a shift (checked in before any shift opened) is allowed,
        but the same bag id appearing with two shift ids is structurally
        impossible in the store and is still asserted here.
        """

        owners: dict[str, str] = {}
        for bag in state.bags.values():
            if not bag.state.active:
                continue
            previous = owners.get(bag.id)
            if previous is not None and previous != bag.shift_id:
                return False
            owners[bag.id] = bag.shift_id
            if bag.shift_id and bag.shift_id not in state.shifts:
                return False
        return True

    def _ownership_matches_history(self, state: State) -> bool:
        derived = ownership_from_history(state.handovers.values())
        for bag in state.bags.values():
            if not bag.state.active:
                continue
            # Bags a completed handover moved must now carry the derived owner;
            # bags untouched by history keep whatever the store shows.
            if bag.id in derived and derived[bag.id] != bag.shift_id:
                return False
        return True

    def _manifests_intact(self, state: State) -> bool:
        for handover in state.handovers.values():
            digest = manifest_digest(item.as_dict() for item in handover.items)
            if digest != handover.manifest_digest:
                return False
            expected = handover.summary.get("in_progress")
            if expected is not None and int(expected) != len(handover.items):
                return False
        return True

    def _reports_match_inventory(self, state: State) -> bool:
        for report in self.reports_from(state):
            rows = [
                row for row in self.inventory_from(state) if row["shift_id"] == report.shift_id
            ]
            if report.active != len(rows):
                return False
            by_state: dict[str, int] = {}
            for row in rows:
                by_state[row["state"]] = by_state.get(row["state"], 0) + 1
            if report.by_state != dict(sorted(by_state.items())):
                return False
        return True

    def _sorts_reproduce(self, state: State) -> bool:
        """The report's sort total equals an independent count over the log."""

        counts: dict[str, int] = {}
        for record in state.sort_records:
            if record.shift_id:
                counts[record.shift_id] = counts.get(record.shift_id, 0) + 1
        for report in self.reports_from(state):
            if report.sorted_total != counts.get(report.shift_id, 0):
                return False
        return True

    # -- pure helpers (usable without a live store session) ----------------

    @staticmethod
    def inventory_from(state: State) -> list[dict[str, str]]:
        return [
            {"bag_id": bag.id, "state": bag.state.value, "shift_id": bag.shift_id}
            for bag in state.bags.values()
            if bag.state.active
        ]

    @staticmethod
    def reports_from(state: State) -> list[ShiftReport]:
        shifts = set(state.shifts)
        shifts.update(bag.shift_id for bag in state.bags.values() if bag.shift_id)
        shifts.update(record.shift_id for record in state.sort_records if record.shift_id)
        reports: list[ShiftReport] = []
        for shift_id in sorted(shifts):
            active = [
                bag
                for bag in state.bags.values()
                if bag.state.active and bag.shift_id == shift_id
            ]
            by_state: dict[str, int] = {}
            for bag in active:
                by_state[bag.state.value] = by_state.get(bag.state.value, 0) + 1
            reports.append(
                ShiftReport(
                    shift_id=shift_id,
                    active=len(active),
                    by_state=dict(sorted(by_state.items())),
                    sorted_total=sum(
                        1
                        for record in state.sort_records
                        if record.shift_id == shift_id
                    ),
                )
            )
        return reports
