"""Shift handover: freeze the in-process inventory, confirm custody, or roll back.

A handover moves through three states::

    proposed --confirm--> confirmed
       |
       +---rollback-----> rolled_back

Nothing is attributed to the receiving shift until confirmation, so an
interrupted handover rolls back to the ownership that existed before the
proposal. Every frozen item carries an inventory digest, which lets a console
caller recompute the inventory from the journal and prove the shift report and
the sort records describe the same handover.
"""

from __future__ import annotations

from bagsort.clock import Clock
from bagsort.errors import ConflictError, NotFoundError, ValidationError
from bagsort.ids import IdFactory
from bagsort.model.entities import Handover, HandoverItem, Shift
from bagsort.model.enums import CustodyPolicy, HandoverState, ShiftState
from bagsort.store.store import Store
from .custody import ShiftCustody, inventory_digest, sort_counts_from_journal
from .policy import owner_after_confirmation, policy_of, responsibility_clause


class HandoverManager:
    """Coordinates the handover lifecycle against the committed document."""

    def __init__(
        self,
        store: Store,
        custody: ShiftCustody,
        clock: Clock,
        ids: IdFactory | None = None,
    ) -> None:
        self._store = store
        self._custody = custody
        self._clock = clock
        self._ids = ids or IdFactory("hnd")

    # -- queries -----------------------------------------------------------

    def get(self, handover_id: str) -> Handover:
        handover = self._store.state().handovers.get(handover_id)
        if handover is None:
            raise NotFoundError(f"handover {handover_id!r} is not registered")
        return handover

    def list(self) -> list[Handover]:
        handovers = self._store.state().handovers
        return [handovers[key] for key in sorted(handovers)]

    def for_shift(self, shift_id: str) -> list[Handover]:
        return [
            handover
            for handover in self.list()
            if shift_id in (handover.outgoing_shift_id, handover.incoming_shift_id)
        ]

    def open_proposal(self) -> Handover | None:
        proposed = [h for h in self.list() if h.state is HandoverState.PROPOSED]
        return sorted(proposed, key=lambda handover: handover.id)[-1] if proposed else None

    def inventory(self, handover_id: str) -> list[HandoverItem]:
        return list(self.get(handover_id).items)

    # -- lifecycle ---------------------------------------------------------

    def propose(
        self,
        outgoing: Shift,
        incoming: Shift,
        policy: str | CustodyPolicy = CustodyPolicy.TRANSFER,
        note: str = "",
    ) -> Handover:
        if outgoing.id == incoming.id:
            raise ValidationError("a shift cannot hand over to itself")
        if outgoing.state is not ShiftState.OPEN:
            raise ConflictError(f"outgoing shift {outgoing.id!r} is not open")
        if incoming.state is not ShiftState.OPEN:
            raise ConflictError(f"incoming shift {incoming.id!r} is not open")
        if self.open_proposal() is not None:
            raise ConflictError("another handover is already awaiting confirmation")

        unassigned = self._custody.unassigned_active_bags()
        if unassigned:
            raise ConflictError(
                f"{len(unassigned)} active bag(s) belong to no shift: {unassigned}"
            )

        chosen = policy_of(policy)
        items = self._custody.active_items(outgoing.id)
        state = self._store.state()
        sort_watermark = state.sort_records[-1].sequence if state.sort_records else 0
        timeline_counts = sort_counts_from_journal(self._store.entries())
        sort_count = timeline_counts.get(outgoing.id, 0)
        handover = Handover(
            id=self._ids.next(),
            outgoing_shift_id=outgoing.id,
            incoming_shift_id=incoming.id,
            state=HandoverState.PROPOSED,
            policy=chosen,
            responsibility=responsibility_clause(chosen),
            items=tuple(items),
            owners_before={item.bag_id: outgoing.id for item in items},
            inventory_digest=inventory_digest(items),
            sort_watermark=sort_watermark,
            sort_count=sort_count,
            proposed_at=self._clock.now_iso(),
            note=(note or "").strip(),
        )
        with self._store.session() as session:
            session.state.handovers[handover.id] = handover
            session.record("handover.put", {"handover": handover.as_dict()}, handover.proposed_at)
        return handover

    def confirm(self, handover_id: str) -> Handover:
        handover = self._require_state(handover_id, HandoverState.PROPOSED)
        current_items = self._custody.active_items(handover.outgoing_shift_id)
        if inventory_digest(current_items) != handover.inventory_digest:
            raise ConflictError(
                "the in-process inventory changed during the handover; roll back and re-propose"
            )
        new_owner = owner_after_confirmation(
            handover.policy, handover.outgoing_shift_id, handover.incoming_shift_id
        )
        at = self._clock.now_iso()
        with self._store.session() as session:
            if new_owner != handover.outgoing_shift_id:
                for bag_id in handover.owners_before:
                    session.state.bag_owners[bag_id] = new_owner
                    session.record(
                        "bag.owner.put", {"bag_id": bag_id, "shift_id": new_owner}, at
                    )
            updated = handover.evolved(state=HandoverState.CONFIRMED, confirmed_at=at)
            session.state.handovers[updated.id] = updated
            session.record("handover.put", {"handover": updated.as_dict()}, at)
        return updated

    def rollback(self, handover_id: str) -> Handover:
        """Abort an interrupted handover and restore pre-proposal ownership."""

        handover = self._require_state(handover_id, HandoverState.PROPOSED)
        at = self._clock.now_iso()
        with self._store.session() as session:
            for bag_id, shift_id in handover.owners_before.items():
                session.state.bag_owners[bag_id] = shift_id
                session.record("bag.owner.put", {"bag_id": bag_id, "shift_id": shift_id}, at)
            updated = handover.evolved(state=HandoverState.ROLLED_BACK, rolled_back_at=at)
            session.state.handovers[updated.id] = updated
            session.record("handover.put", {"handover": updated.as_dict()}, at)
        return updated

    def verify_inventory(self, handover_id: str) -> bool:
        """Recompute the digest over the frozen items from the committed state."""

        handover = self.get(handover_id)
        return inventory_digest(handover.items) == handover.inventory_digest

    # -- internals ---------------------------------------------------------

    def _require_state(self, handover_id: str, state: HandoverState) -> Handover:
        handover = self.get(handover_id)
        if handover.state is not state:
            raise ConflictError(
                f"handover {handover_id!r} is {handover.state.value}, not {state.value}"
            )
        return handover
