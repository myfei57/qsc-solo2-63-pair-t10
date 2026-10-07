"""Shift custody and handover lifecycle."""

from __future__ import annotations

import pytest

from bagsort.model.enums import CustodyPolicy, HandoverState, ShiftState
from bagsort.store.replay import replay
from bagsort.store.store import Store
from tests.support import check_in, register_flight


def _start(plant):
    flight = register_flight(plant, chute="A")
    plant.assign_chute(flight.id, "T1-CHA")
    morning = plant.open_shift("S1", "morning")
    return flight, morning


def _begin_handover(plant, policy=CustodyPolicy.TRANSFER, note=""):
    evening = plant.open_shift("S2", "evening")
    return plant.propose_handover("S1", "S2", policy, note), evening


def test_checked_in_bag_is_attributed_to_the_open_shift(plant):
    flight = register_flight(plant)
    shift = plant.open_shift("S1")
    bag = check_in(plant, flight.id, 1)[0]
    assert plant.custody.owner_of(bag.id) == shift.id
    assert plant.shifts.held_bags(shift.id) == [bag.id]


def test_bags_without_a_shift_stay_unowned_and_block_a_handover(plant):
    flight = register_flight(plant)
    bags = check_in(plant, flight.id, 1)
    plant.open_shift("S1")
    plant.open_shift("S2")
    assert plant.custody.unassigned_active_bags() == [bags[0].id]
    with pytest.raises(Exception):
        plant.propose_handover("S1", "S2")
    plant.attribute_bag(bags[0].id, "S1")
    handover = plant.propose_handover("S1", "S2")
    assert [item.bag_id for item in handover.items] == [bags[0].id]


def test_a_shift_id_cannot_be_opened_twice(plant):
    plant.open_shift("S1")
    with pytest.raises(Exception):
        plant.open_shift("S1")


def test_handover_proposal_freezes_the_in_process_inventory(plant):
    flight, morning = _start(plant)
    bags = check_in(plant, flight.id, 3)
    handover, evening = _begin_handover(plant)
    assert handover.state is HandoverState.PROPOSED
    assert len(handover.items) == 3
    assert handover.sort_watermark == 0
    assert handover.inventory_digest
    assert handover.responsibility.startswith("transfer:")
    # Ownership must not move before the receiving shift confirms.
    assert all(plant.custody.owner_of(bag.id) == "S1" for bag in bags)
    assert handover.proposed_at and not handover.confirmed_at


def test_a_second_proposal_is_rejected_while_one_is_pending(plant):
    flight, morning = _start(plant)
    check_in(plant, flight.id, 1)
    plant.open_shift("S2")
    plant.propose_handover("S1", "S2")
    with pytest.raises(Exception):
        plant.propose_handover("S1", "S2")


def test_transfer_policy_moves_the_inventory_on_confirmation(plant):
    flight, morning = _start(plant)
    bags = check_in(plant, flight.id, 2)
    handover, evening = _begin_handover(plant, CustodyPolicy.TRANSFER)
    confirmed = plant.confirm_handover(handover.id)
    assert confirmed.state is HandoverState.CONFIRMED
    assert confirmed.confirmed_at
    assert all(plant.custody.owner_of(bag.id) == "S2" for bag in bags)
    assert plant.sheet.shift_report("S1")["active_bags"] == 0
    s2_report = plant.sheet.shift_report("S2")
    assert s2_report["active_bags"] == 2
    assert s2_report["received"][0]["handover_id"] == handover.id


def test_relinquish_policy_keeps_the_inventory_with_the_outgoing_shift(plant):
    flight, morning = _start(plant)
    bags = check_in(plant, flight.id, 2)
    handover, evening = _begin_handover(plant, CustodyPolicy.RELINQUISH)
    assert handover.responsibility.startswith("relinquish:")
    plant.confirm_handover(handover.id)
    # The two policies trace responsibility in opposite directions.
    assert all(plant.custody.owner_of(bag.id) == "S1" for bag in bags)
    assert plant.sheet.shift_report("S2")["received"] == []
    with pytest.raises(Exception):
        plant.close_shift("S1")


def test_an_interrupted_handover_rolls_back_to_the_pre_proposal_owners(plant):
    flight, morning = _start(plant)
    bags = check_in(plant, flight.id, 2)
    handover, evening = _begin_handover(plant)
    rolled_back = plant.rollback_handover(handover.id)
    assert rolled_back.state is HandoverState.ROLLED_BACK
    assert rolled_back.rolled_back_at
    assert all(plant.custody.owner_of(bag.id) == "S1" for bag in bags)
    # A finished handover cannot be rolled back or confirmed again.
    with pytest.raises(Exception):
        plant.rollback_handover(handover.id)
    with pytest.raises(Exception):
        plant.confirm_handover(handover.id)
    # After rolling back, the report and the live list still agree.
    assert plant.sheet.reconcile()["consistent"] is True


def test_confirmation_refuses_an_inventory_that_changed_during_the_window(plant):
    flight, morning = _start(plant)
    bags = check_in(plant, flight.id, 2)
    handover, evening = _begin_handover(plant)
    plant.divert_bag(bags[0].id)
    with pytest.raises(Exception):
        plant.confirm_handover(handover.id)
    # Rollback clears the pending proposal so a fresh one can be made.
    plant.rollback_handover(handover.id)
    fresh = plant.propose_handover("S1", "S2")
    confirmed = plant.confirm_handover(fresh.id)
    assert confirmed.state is HandoverState.CONFIRMED
    assert plant.custody.owner_of(bags[1].id) == "S2"


def test_a_bag_can_hang_on_only_one_shift(plant):
    flight, morning = _start(plant)
    bag = check_in(plant, flight.id, 1)[0]
    handover, evening = _begin_handover(plant)
    plant.confirm_handover(handover.id)
    owners = list(plant.store.state().bag_owners.values())
    assert owners.count("S2") == 1
    assert plant.custody.owner_of(bag.id) == "S2"


def test_handover_audit_entries_are_chained(plant):
    flight, morning = _start(plant)
    check_in(plant, flight.id, 1)
    handover, evening = _begin_handover(plant)
    plant.rollback_handover(handover.id)
    replacement = plant.propose_handover("S1", "S2")
    plant.confirm_handover(replacement.id)
    kinds = plant.audit.by_kind()
    assert kinds["handover_propose"] == 2
    assert kinds["handover_rollback"] == 1
    assert kinds["handover_confirm"] == 1
    assert plant.audit.verify() is True


def test_sort_records_attribute_to_the_shift_that_owned_the_bag(plant):
    flight, morning = _start(plant)
    bags = check_in(plant, flight.id, 2)
    plant.push_bag(bags[0].id, "T1-CHA")
    handover, evening = _begin_handover(plant)
    assert handover.sort_count == 1
    plant.confirm_handover(handover.id)
    plant.push_bag(bags[1].id, "T1-CHA")
    counts = plant.sheet.sort_counts()
    assert counts["S1"] == 1
    assert counts["S2"] == 1


def test_reconcile_reproduces_inventory_sorts_and_reports(plant):
    flight, morning = _start(plant)
    bags = check_in(plant, flight.id, 2)
    plant.push_bag(bags[0].id, "T1-CHA")
    handover, evening = _begin_handover(plant)
    plant.confirm_handover(handover.id)
    result = plant.sheet.reconcile()
    assert result["consistent"] is True
    assert result["sort_records_total"] == 1
    assert result["unowned_active_bags"] == []
    assert plant.handovers.verify_inventory(handover.id) is True


def test_handover_history_recomputes_per_shift(plant):
    flight, morning = _start(plant)
    check_in(plant, flight.id, 1)
    plant.open_shift("S2")
    first = plant.propose_handover("S1", "S2")
    plant.rollback_handover(first.id)
    second = plant.propose_handover("S1", "S2")
    plant.confirm_handover(second.id)
    recomputed = plant.handover_history.recompute("S1")
    assert recomputed["confirmed"] == [second.id]
    assert recomputed["rolled_back"] == [first.id]
    assert recomputed["matches_state"] is True
    versions = plant.handover_history.versions(first.id)
    assert [handover.state for handover in versions] == [
        HandoverState.PROPOSED,
        HandoverState.ROLLED_BACK,
    ]


def test_sort_counts_stay_with_the_shift_that_sorted_after_a_transfer(plant):
    flight, morning = _start(plant)
    bags = check_in(plant, flight.id, 1)
    plant.push_bag(bags[0].id, "T1-CHA")
    first, evening = _begin_handover(plant)
    plant.confirm_handover(first.id)
    # A later proposal (after the first transfer) must not recount S1's sort.
    s3 = plant.open_shift("S3", "night")
    second = plant.propose_handover("S2", "S3")
    assert second.sort_count == 0
    assert plant.sheet.sort_count_for("S1") == 1


def test_handover_state_rebuilds_from_the_journal(plant):
    flight, morning = _start(plant)
    bags = check_in(plant, flight.id, 1)
    handover, evening = _begin_handover(plant)
    plant.confirm_handover(handover.id)
    plant.store.persist()

    reopened = Store.open(plant.store.path)
    from bagsort.app import Plant as PlantModel

    rebuilt_plant = PlantModel.build(reopened, clock=plant.clock)
    assert replay(reopened.entries()).as_dict() == reopened.state().as_dict()
    assert rebuilt_plant.custody.owner_of(bags[0].id) == "S2"
    assert rebuilt_plant.sheet.reconcile()["consistent"] is True
    assert rebuilt_plant.audit.verify() is True


def test_closed_shift_is_marked(plant):
    plant.open_shift("S1")
    closed = plant.close_shift("S1")
    assert closed.state is ShiftState.CLOSED
    assert closed.closed_at
    assert plant.shifts.current() is None


def test_a_shift_in_a_pending_handover_cannot_close(plant):
    flight, morning = _start(plant)
    check_in(plant, flight.id, 1)
    handover, evening = _begin_handover(plant)
    with pytest.raises(Exception):
        plant.close_shift("S2")
    plant.rollback_handover(handover.id)
    assert plant.close_shift("S2").state is ShiftState.CLOSED


def test_handover_audit_entry_carries_the_conclusion(plant):
    flight, morning = _start(plant)
    bags = check_in(plant, flight.id, 1)
    handover, evening = _begin_handover(plant, CustodyPolicy.TRANSFER)
    plant.confirm_handover(handover.id)
    entry = next(
        item for item in plant.audit.entries() if item.kind == "handover_confirm"
    )
    assert entry.subject == handover.id
    assert entry.detail["policy"] == "transfer"
    assert entry.detail["item_count"] == 1
    assert entry.detail["inventory_digest"] == handover.inventory_digest
    assert "responsibility" in entry.detail and entry.detail["responsibility"]


def test_unknown_custody_policy_is_rejected(plant):
    _start(plant)
    plant.open_shift("S2")
    with pytest.raises(Exception):
        plant.propose_handover("S1", "S2", "sideways")
