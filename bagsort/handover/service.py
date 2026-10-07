"""The handover state machine: open shift, start handover, confirm, roll back.

A handover is the only thing allowed to move a bag between shifts, and the move
only happens when the receiver confirms. While a handover is ``open`` the
handover is not complete: ownership, the in-progress list and the shift report
still speak the pre-handover language, and the duty pointer stays on the
outgoing shift. Confirmation, ownership moves and the duty change commit in one
journal transaction, so ownership can never be left half transferred and one
bag can never be attached to two shifts at once.
"""

from __future__ import annotations

from bagsort.clock import Clock
from bagsort.errors import ConflictError, NotFoundError, ValidationError
from bagsort.ids import IdFactory
from bagsort.model.entities import Handover, ManifestItem, Shift
from bagsort.store.store import Store
from .policy import POLICIES, manifest_digest, responsibility_text

OPEN = "open"
COMPLETED = "completed"
ROLLED_BACK = "rolled_back"


class HandoverService:
    """Shift registry and handover workflow over the append-only store."""

    def __init__(self, store: Store, clock: Clock, prefix: str = "HD") -> None:
        self._store = store
        self._clock = clock
        self._ids = IdFactory(prefix)

    # -- shifts ------------------------------------------------------------

    def open_shift(self, label: str, shift_id: str = "") -> Shift:
        """Register a shift. The first shift opened takes the duty pointer.

        Later shifts may be created ahead of time (the incoming crew clocks
        in), but they do not become the duty shift until a handover to them is
        confirmed; a handover that never confirms cannot move duty by the back
        door of "the newest opened shift".
        """

        label = (label or "").strip()
        if not label:
            raise ValidationError("shift label must not be empty")
        if self._open_handover() is not None:
            raise ConflictError("an open handover must finish before a shift opens")
        shift_id = shift_id or self._next_shift_id(label)
        if self._store.state().shifts.get(shift_id) is not None:
            raise ConflictError(f"shift {shift_id!r} already exists")
        at = self._clock.now_iso()
        shift = Shift(id=shift_id, label=label, opened_at=at)
        takes_duty = not self._store.state().duty_shift
        with self._store.session() as session:
            session.state.shifts[shift.id] = shift
            session.record("shift.put", {"shift": shift.as_dict()}, at)
            if takes_duty:
                session.state.duty_shift = shift.id
                session.record("shift.set_duty", {"shift_id": shift.id}, at)
        return shift

    def close_shift(self, shift_id: str) -> Shift:
        shift = self.require_shift(shift_id)
        if not shift.open:
            return shift
        if self._open_handover() is not None:
            raise ConflictError("cannot close a shift while a handover is open")
        owning = [
            bag.id
            for bag in self._store.state().bags.values()
            if bag.state.active and bag.shift_id == shift_id
        ]
        if owning:
            raise ConflictError(
                "shift still owns in-progress bags; hand them over first: "
                + ", ".join(sorted(owning))
            )
        at = self._clock.now_iso()
        updated = shift.evolved(closed_at=at)
        with self._store.session() as session:
            session.state.shifts[updated.id] = updated
            session.record("shift.put", {"shift": updated.as_dict()}, at)
            if session.state.duty_shift == shift_id:
                session.state.duty_shift = ""
                session.record("shift.set_duty", {"shift_id": ""}, at)
        return updated

    def get_shift(self, shift_id: str) -> Shift | None:
        return self._store.state().shifts.get(shift_id)

    def require_shift(self, shift_id: str) -> Shift:
        shift = self.get_shift(shift_id)
        if shift is None:
            raise NotFoundError(f"shift {shift_id!r} does not exist")
        return shift

    def list_shifts(self) -> list[Shift]:
        return [self._store.state().shifts[key] for key in sorted(self._store.state().shifts)]

    def open_shifts(self) -> list[Shift]:
        return [shift for shift in self.list_shifts() if shift.open]

    def current_shift(self) -> Shift | None:
        """The shift that owns newly induced bags.

        An open handover keeps the outgoing shift on duty: the receiver has not
        confirmed yet, so work done during the handover is not silently booked
        to a shift nobody accepted.
        """

        handover = self._open_handover()
        if handover is not None:
            return self.get_shift(handover.shift_from)
        duty = self._store.state().duty_shift
        return self.get_shift(duty) if duty else None

    def current_shift_id(self) -> str:
        shift = self.current_shift()
        return shift.id if shift else ""

    # -- handovers ---------------------------------------------------------

    def start_handover(
        self,
        shift_from: str,
        shift_to: str,
        policy: str,
        note: str = "",
    ) -> Handover:
        if policy not in POLICIES:
            raise ValidationError(f"policy must be one of {', '.join(POLICIES)}")
        outgoing = self.require_shift(shift_from)
        incoming = self.require_shift(shift_to)
        if not outgoing.open:
            raise ConflictError(f"outgoing shift {shift_from!r} is already closed")
        if not incoming.open:
            raise ConflictError(f"receiving shift {shift_to!r} is already closed")
        if shift_from == shift_to:
            raise ConflictError("a handover needs two different shifts")
        if self._open_handover() is not None:
            raise ConflictError("another handover is already awaiting confirmation")

        at = self._clock.now_iso()
        items = self._freeze_items()
        digest = manifest_digest(item.as_dict() for item in items)
        summary = self._summarise(items, outgoing.id)
        handover = Handover(
            id=self._ids.next(),
            shift_from=outgoing.id,
            shift_to=incoming.id,
            status=OPEN,
            policy=policy,
            responsibility=responsibility_text(policy),
            started_at=at,
            manifest_digest=digest,
            items=tuple(items),
            summary=summary,
        )
        if note:
            handover = handover.evolved(summary={**summary, "note": note})
        with self._store.session() as session:
            session.state.handovers[handover.id] = handover
            session.record("handover.put", {"handover": handover.as_dict()}, at)
        return handover

    def confirm_handover(self, handover_id: str) -> Handover:
        """Receiver accepts the manifest; ownership and duty move together."""

        handover = self._require_open(handover_id)
        self.require_shift(handover.shift_from)
        self.require_shift(handover.shift_to)
        at = self._clock.now_iso()

        if handover.manifest_digest != manifest_digest(
            item.as_dict() for item in handover.items
        ):
            raise ConflictError("stored manifest digest does not match its items")

        target = handover.shift_to if handover.policy == "transfer" else handover.shift_from
        moved: list[tuple[str, dict]] = []
        with self._store.session() as session:
            for item in handover.items:
                bag = session.state.bags.get(item.bag_id)
                # Only bags the outgoing shift owned are on offer; bags frozen
                # while belonging to another shift keep that owner.
                if (
                    bag is not None
                    and bag.state.active
                    and item.shift_id == handover.shift_from
                    and bag.shift_id != target
                ):
                    updated = bag.evolved(shift_id=target)
                    session.state.bags[item.bag_id] = updated
                    moved.append((item.bag_id, updated.as_dict()))
            completed = handover.evolved(status=COMPLETED, completed_at=at)
            session.state.handovers[completed.id] = completed
            session.record("handover.put", {"handover": completed.as_dict()}, at)
            for _bag_id, bag_dict in moved:
                session.record("bag.put", {"bag": bag_dict}, at)
            session.state.duty_shift = target
            session.record("shift.set_duty", {"shift_id": target}, at)
        return completed

    def rollback_handover(self, handover_id: str, note: str = "") -> Handover:
        """Interrupt the handover and restore pre-handover ownership.

        Every manifest bag is returned to the shift that owned it when the
        handover opened (bags belonging to other shifts simply stay put). Bags
        that have left the plant in the meantime are left alone and counted as
        ``resolved``, so the restored in-progress list still agrees with the
        shift report after the rollback. The duty pointer never moved, so duty
        is still on the outgoing shift afterwards.
        """

        handover = self._require_open(handover_id)
        at = self._clock.now_iso()
        restored: list[str] = []
        resolved: list[str] = []

        with self._store.session() as session:
            restored_dicts: list[dict] = []
            for item in handover.items:
                bag = session.state.bags.get(item.bag_id)
                if bag is not None and bag.state.active:
                    if bag.shift_id != item.shift_id:
                        updated = bag.evolved(shift_id=item.shift_id)
                        session.state.bags[item.bag_id] = updated
                        restored_dicts.append(updated.as_dict())
                    restored.append(item.bag_id)
                else:
                    resolved.append(item.bag_id)
            updated_summary = dict(handover.summary)
            updated_summary["rollback_restored"] = len(restored)
            updated_summary["rollback_resolved"] = len(resolved)
            rolled_back = handover.evolved(
                status=ROLLED_BACK,
                rolled_back_at=at,
                rollback_note=note,
                summary=updated_summary,
            )
            session.state.handovers[rolled_back.id] = rolled_back
            session.record("handover.put", {"handover": rolled_back.as_dict()}, at)
            for bag_dict in restored_dicts:
                session.record("bag.put", {"bag": bag_dict}, at)
        return rolled_back

    # -- read side ---------------------------------------------------------

    def get(self, handover_id: str) -> Handover | None:
        return self._store.state().handovers.get(handover_id)

    def require(self, handover_id: str) -> Handover:
        handover = self.get(handover_id)
        if handover is None:
            raise NotFoundError(f"handover {handover_id!r} does not exist")
        return handover

    def list_handovers(self) -> list[Handover]:
        return [
            self._store.state().handovers[key]
            for key in sorted(self._store.state().handovers)
        ]

    def open_handover(self) -> Handover | None:
        return self._open_handover()

    def for_shift(self, shift_id: str) -> list[Handover]:
        """Every handover that touched a shift, chronological by id."""

        return [
            handover
            for handover in self.list_handovers()
            if handover.shift_from == shift_id or handover.shift_to == shift_id
        ]

    def ownership_conflicts(self) -> list[str]:
        """Bag ids whose current ownership the handover history cannot explain.

        The materialised ``shift_id`` on every active bag must equal what an
        independent replay of the handover timeline derives; a mismatch means
        the ownership bookkeeping and the audit story disagree.
        """

        from .history import ownership_from_history

        derived = ownership_from_history(self.list_handovers())
        conflicts: list[str] = []
        for bag_id, bag in sorted(self._store.state().bags.items()):
            if not bag.state.active:
                continue
            if bag_id in derived and derived[bag_id] != bag.shift_id:
                conflicts.append(bag_id)
        return conflicts

    # -- internals ---------------------------------------------------------

    def _next_shift_id(self, label: str) -> str:
        count = len(self._store.state().shifts) + 1
        base = "".join(char for char in label.upper() if char.isalnum())[:6] or "SHIFT"
        return f"SH-{base}-{count:03d}"

    def _freeze_items(self) -> list[ManifestItem]:
        """Freeze every bag still in the plant, with the owner it has now."""

        items = [
            ManifestItem(
                bag_id=bag.id,
                state=bag.state.value,
                shift_id=bag.shift_id,
            )
            for bag in self._store.state().bags.values()
            if bag.state.active
        ]
        return sorted(items, key=lambda item: item.bag_id)

    @staticmethod
    def _summarise(items: list[ManifestItem], shift_from: str) -> dict[str, object]:
        by_state: dict[str, int] = {}
        for item in items:
            by_state[item.state] = by_state.get(item.state, 0) + 1
        return {
            "in_progress": len(items),
            "handoff": sum(1 for item in items if item.shift_id == shift_from),
            "by_state": dict(sorted(by_state.items())),
        }

    def _open_handover(self) -> Handover | None:
        for handover in self._store.state().handovers.values():
            if handover.status == OPEN:
                return handover
        return None

    def _require_open(self, handover_id: str) -> Handover:
        handover = self.require(handover_id)
        if handover.status != OPEN:
            raise ConflictError(
                f"handover {handover_id!r} is already {handover.status}"
            )
        return handover
