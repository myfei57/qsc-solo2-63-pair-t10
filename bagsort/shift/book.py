"""The shift register: opening, listing and closing working shifts."""

from __future__ import annotations

from bagsort.clock import Clock
from bagsort.errors import ConflictError, NotFoundError, ValidationError
from bagsort.model.entities import Shift
from bagsort.model.enums import ShiftState
from bagsort.model.validate import require_text
from bagsort.store.store import Store
from .custody import ShiftCustody


class ShiftBook:
    """Holds every shift the terminal has run, in the committed document."""

    def __init__(self, store: Store, custody: ShiftCustody, clock: Clock) -> None:
        self._store = store
        self._custody = custody
        self._clock = clock

    def open(self, shift_id: str, code: str = "") -> Shift:
        identifier = require_text(shift_id, "shift_id").upper()
        existing = self._store.state().shifts.get(identifier)
        if existing is not None:
            raise ConflictError(f"shift {identifier!r} is already registered")
        label = (code or "").strip() or identifier
        shift = Shift(id=identifier, code=label, opened_at=self._clock.now_iso())
        with self._store.session() as session:
            session.state.shifts[shift.id] = shift
            session.record("shift.put", {"shift": shift.as_dict()}, shift.opened_at)
        return shift

    def close(self, shift_id: str) -> Shift:
        shift = self.require(shift_id)
        if shift.state is not ShiftState.OPEN:
            raise ConflictError(f"shift {shift_id!r} is already closed")
        active = self._custody.bags_owned_by(shift_id)
        held: list[str] = []
        for bag_id in active:
            bag = self._store.state().bags.get(bag_id)
            if bag is not None and bag.state.active:
                held.append(bag_id)
        if held:
            raise ConflictError(
                f"shift {shift_id!r} still holds {len(held)} in-process bag(s); "
                "hand them over before closing"
            )
        updated = shift.evolved(state=ShiftState.CLOSED, closed_at=self._clock.now_iso())
        with self._store.session() as session:
            session.state.shifts[updated.id] = updated
            session.record("shift.put", {"shift": updated.as_dict()}, updated.closed_at)
        return updated

    def get(self, shift_id: str) -> Shift:
        return self._store.state().shifts.get(shift_id, None)

    def require(self, shift_id: str) -> Shift:
        shift = self.get(shift_id)
        if shift is None:
            raise NotFoundError(f"shift {shift_id!r} is not registered")
        return shift

    def require_open(self, shift_id: str) -> Shift:
        shift = self.require(shift_id)
        if shift.state is not ShiftState.OPEN:
            raise ConflictError(f"shift {shift_id!r} is not open")
        return shift

    def current(self) -> Shift | None:
        """The most recently opened shift that is still open, in id order."""

        opens = [
            shift
            for shift in self._store.state().shifts.values()
            if shift.state is ShiftState.OPEN
        ]
        return sorted(opens, key=lambda shift: (shift.opened_at, shift.id))[-1] if opens else None

    def list(self) -> list[Shift]:
        shifts = self._store.state().shifts
        return [shifts[key] for key in sorted(shifts)]

    def held_bags(self, shift_id: str) -> list[str]:
        self.require(shift_id)
        return self._custody.bags_owned_by(shift_id)

    def claim(self, bag_id: str, shift_id: str | None = None) -> str:
        """Attribute a newly active bag to an open shift, if one is running.

        When no shift is open the bag stays unowned, which keeps the line usable
        before any shift has been opened; the handover inventory flags such bags.
        """

        target = shift_id
        if target is None:
            current = self.current()
            target = current.id if current is not None else ""
        if target:
            self.require_open(target)
            self._custody.assign(bag_id, target)
        return target
