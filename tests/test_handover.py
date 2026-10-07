"""Shift handover: inventory freeze, ownership transfer and reconciliation."""

from __future__ import annotations

import pytest

from bagsort.errors import ConflictError, NotFoundError, ValidationError
from bagsort.handover.policy import (
    POLICIES,
    RETAIN,
    TRANSFER,
    manifest_digest,
    responsibility_text,
)
from tests.support import check_in, register_flight


def _two_shifts(plant):
    flight = register_flight(plant)
    plant.assign_chute(flight.id, "T1-CHA")
    morning = plant.open_shift("morning", "SH-A")
    evening = plant.open_shift("evening", "SH-B")
    return flight, morning, evening


def test_open_shift_registers_and_tracks_the_current_shift(plant):
    shift = plant.open_shift("morning", "SH-A")
    assert shift.open is True
    assert plant.handovers.current_shift().id == "SH-A"
    assert plant.handovers.list_shifts() == [shift]


def test_shift_label_is_required(plant):
    with pytest.raises(ValidationError):
        plant.open_shift("   ")


def test_checked_in_bag_is_owned_by_the_current_shift(plant):
    _two_shifts(plant)
    flight = plant.book.list()[0]
    bag = plant.check_in("BAG0001", flight.id)
    assert bag.shift_id == "SH-A"


def test_start_handover_freezes_the_active_inventory(plant):
    flight, morning, evening = _two_shifts(plant)
    bag = check_in(plant, flight.id, 1)[0]
    plant.push_bag(bag.id, "T1-CHA")
    handover = plant.start_handover("SH-A", "SH-B", TRANSFER)
    assert handover.status == "open"
    assert handover.summary["in_progress"] == 1
    assert [item.bag_id for item in handover.items] == [bag.id]
    assert handover.manifest_digest == manifest_digest(
        item.as_dict() for item in handover.items
    )
    # While awaiting confirmation the handover is not complete and ownership
    # has not moved.
    assert plant.bag(bag.id).shift_id == "SH-A"
    assert plant.handovers.open_handover().id == handover.id


def test_handover_is_not_complete_until_receiver_confirms(plant):
    flight, _, _ = _two_shifts(plant)
    bag = check_in(plant, flight.id, 1)[0]
    handover = plant.start_handover("SH-A", "SH-B", TRANSFER)
    assert plant.handovers.current_shift().id == "SH-A"
    with pytest.raises(ConflictError):
        plant.start_handover("SH-A", "SH-B", TRANSFER)
    plant.confirm_handover(handover.id)
    assert plant.bag(bag.id).shift_id == "SH-B"
    assert plant.handovers.open_handover() is None
    assert plant.handovers.current_shift().id == "SH-B"


def test_transfer_policy_moves_ownership_but_retain_policy_keeps_it(plant):
    flight, _, _ = _two_shifts(plant)
    transferred = check_in(plant, flight.id, 1, prefix="TX")[0]
    first = plant.start_handover("SH-A", "SH-B", TRANSFER)
    plant.confirm_handover(first.id)
    assert plant.bag(transferred.id).shift_id == "SH-B"

    # The receiving shift later hands over to a fresh shift. A retain policy
    # keeps the bags on the outgoing shift and keeps it on duty.
    night = plant.open_shift("night", "SH-C")
    retained = check_in(plant, flight.id, 1, prefix="RT")[0]
    assert plant.bag(retained.id).shift_id == "SH-B"
    second = plant.start_handover("SH-B", "SH-C", RETAIN)
    plant.confirm_handover(second.id)
    assert plant.bag(retained.id).shift_id == "SH-B"
    assert plant.bag(transferred.id).shift_id == "SH-B"
    assert plant.handovers.current_shift().id == "SH-B"

    # A following transfer then moves everything retained to the new shift.
    third = plant.start_handover("SH-B", "SH-C", TRANSFER)
    plant.confirm_handover(third.id)
    assert plant.bag(retained.id).shift_id == "SH-C"
    assert plant.bag(transferred.id).shift_id == "SH-C"
    assert plant.handovers.current_shift().id == "SH-C"


def test_policy_must_be_known_and_carries_opposite_liability_text(plant):
    _two_shifts(plant)
    with pytest.raises(ValidationError):
        plant.start_handover("SH-A", "SH-B", "whoever_is_nearby")
    transfer_text = responsibility_text(TRANSFER)
    retain_text = responsibility_text(RETAIN)
    assert transfer_text != retain_text
    assert "接收班负责" in transfer_text
    assert "交班班负责" in retain_text or "继续由交班班负责" in retain_text
    assert set(POLICIES) == {TRANSFER, RETAIN}


def test_unknown_or_closed_shifts_are_rejected(plant):
    _two_shifts(plant)
    with pytest.raises(NotFoundError):
        plant.start_handover("SH-A", "SH-Z", TRANSFER)
    plant.close_shift("SH-A")
    with pytest.raises(ConflictError):
        plant.start_handover("SH-A", "SH-B", TRANSFER)


def test_rollback_restores_pre_handover_ownership(plant):
    flight, _, _ = _two_shifts(plant)
    bags = check_in(plant, flight.id, 2)
    handover = plant.start_handover("SH-A", "SH-B", TRANSFER)
    rolled_back = plant.rollback_handover(handover.id, "receiver walked away")
    assert rolled_back.status == "rolled_back"
    assert rolled_back.rollback_note == "receiver walked away"
    assert all(plant.bag(bag.id).shift_id == "SH-A" for bag in bags)
    assert plant.handovers.open_handover() is None
    # A new handover can take its place after the interruption.
    second = plant.start_handover("SH-A", "SH-B", TRANSFER)
    plant.confirm_handover(second.id)
    assert all(plant.bag(bag.id).shift_id == "SH-B" for bag in bags)


def test_rollback_counts_bags_that_left_the_plant_while_open(plant):
    flight, _, _ = _two_shifts(plant)
    bag = check_in(plant, flight.id, 1)[0]
    handover = plant.start_handover("SH-A", "SH-B", TRANSFER)
    plant.divert_bag(bag.id)
    rolled_back = plant.rollback_handover(handover.id)
    assert rolled_back.summary["rollback_resolved"] == 1
    assert rolled_back.summary["rollback_restored"] == 0


def test_completed_handover_cannot_be_confirmed_or_rolled_back_again(plant):
    flight, _, _ = _two_shifts(plant)
    check_in(plant, flight.id, 1)
    handover = plant.start_handover("SH-A", "SH-B", TRANSFER)
    plant.confirm_handover(handover.id)
    with pytest.raises(ConflictError):
        plant.confirm_handover(handover.id)
    with pytest.raises(ConflictError):
        plant.rollback_handover(handover.id)


def test_one_bag_cannot_hang_on_two_shifts(plant):
    flight, _, _ = _two_shifts(plant)
    bag = check_in(plant, flight.id, 1)[0]
    handover = plant.start_handover("SH-A", "SH-B", TRANSFER)
    # Still open: the bag must be found on exactly one shift in every view.
    counts = plant.reconciler.inventory_counts()
    assert counts == {"SH-A": 1}
    plant.confirm_handover(handover.id)
    counts = plant.reconciler.inventory_counts()
    assert counts == {"SH-B": 1}
    assert plant.handovers.ownership_conflicts() == []
