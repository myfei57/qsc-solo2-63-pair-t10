"""The in-progress list, sort log and shift report must reproduce each other."""

from __future__ import annotations

from bagsort.store.replay import replay
from tests.support import check_in, register_flight
from tests.test_handover import _two_shifts


def _work(plant):
    flight, _, _ = _two_shifts(plant)
    bags = check_in(plant, flight.id, 3)
    plant.push_bag(bags[0].id, "T1-CHA")
    plant.push_bag(bags[1].id, "T1-CHA")
    return flight, bags


def test_shift_report_matches_the_in_progress_inventory(plant):
    flight, bags = _work(plant)
    verification = plant.reconciler.verify()
    assert verification["ok"] is True
    report = plant.reconciler.report("SH-A")
    inventory = plant.reconciler.inventory("SH-A")
    assert report.active == len(inventory) == 3
    assert report.sorted_total == 2
    assert report.by_state["sorted"] == 2
    assert report.by_state["injected"] == 1


def test_transfer_keeps_all_three_views_consistent(plant):
    flight, bags = _work(plant)
    handover = plant.start_handover("SH-A", "SH-B", "transfer")
    plant.confirm_handover(handover.id)
    verification = plant.reconciler.verify()
    assert verification["checks"]["ownership_matches_history"] is True
    assert verification["ok"] is True
    incoming = plant.reconciler.report("SH-B")
    outgoing = plant.reconciler.report("SH-A")
    assert incoming.active == 3
    assert outgoing.active == 0
    # The sort log stays with the shift that dispatched; ownership moved.
    assert outgoing.sorted_total == 2
    assert incoming.sorted_total == 0


def test_rollback_keeps_report_and_inventory_consistent(plant):
    flight, bags = _work(plant)
    handover = plant.start_handover("SH-A", "SH-B", "transfer")
    plant.rollback_handover(handover.id)
    verification = plant.reconciler.verify()
    assert verification["ok"] is True
    assert plant.reconciler.inventory_counts() == {"SH-A": 3}
    assert plant.reconciler.report("SH-A").active == 3
    assert plant.reconciler.report("SH-A").sorted_total == 2


def test_ownership_recomputed_from_history_matches_materialised_state(plant):
    flight, bags = _work(plant)
    first = plant.start_handover("SH-A", "SH-B", "transfer")
    plant.confirm_handover(first.id)
    # The history alone says where transferred bags must live.
    derived = plant.handover_history.ownership()
    assert {bag_id: shift for bag_id, shift in derived.items()} == {
        bag.id: "SH-B" for bag in bags
    }
    assert plant.handovers.ownership_conflicts() == []


def test_handover_history_recomputes_one_shift_ledger(plant):
    flight, bags = _work(plant)
    first = plant.start_handover("SH-A", "SH-B", "retain")
    plant.confirm_handover(first.id)
    ledger_a = plant.handover_history.recompute("SH-A")
    assert ledger_a["handovers"] == [first.id]
    assert ledger_a["retained"][first.id] == sorted(bag.id for bag in bags)
    second = plant.start_handover("SH-A", "SH-B", "transfer")
    plant.rollback_handover(second.id)
    ledger_a = plant.handover_history.recompute("SH-A")
    assert ledger_a["returned"][second.id] == sorted(bag.id for bag in bags)
    ledger_b = plant.handover_history.recompute("SH-B")
    assert ledger_b["handovers"] == [first.id, second.id]
    assert ledger_b["received"] == {}


def test_folded_state_reproduces_after_replay(plant):
    flight, bags = _work(plant)
    handover = plant.start_handover("SH-A", "SH-B", "transfer")
    plant.confirm_handover(handover.id)
    rebuilt = replay(plant.store.entries())
    assert rebuilt.as_dict() == plant.store.state().as_dict()
    assert {bag.shift_id for bag in rebuilt.bags.values()} == {"SH-B"}
    assert rebuilt.handovers[handover.id].status == "completed"


def test_manifest_digest_pins_membership_and_prior_ownership():
    from bagsort.handover.policy import manifest_digest

    base = [{"bag_id": "b1", "state": "injected", "shift_id": "SH-A"}]
    changed_member = [
        {"bag_id": "b2", "state": "injected", "shift_id": "SH-A"},
    ]
    changed_owner = [{"bag_id": "b1", "state": "injected", "shift_id": "SH-B"}]
    digest = manifest_digest(base)
    assert digest == manifest_digest(list(reversed(base)))
    assert digest != manifest_digest(changed_member)
    assert digest != manifest_digest(changed_owner)
