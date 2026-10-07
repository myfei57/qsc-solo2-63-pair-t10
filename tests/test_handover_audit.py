"""Handover conclusions land in the tamper evident audit chain."""

from __future__ import annotations

from tests.support import check_in
from tests.test_handover import _two_shifts


def test_each_handover_phase_writes_an_audit_entry(plant):
    flight, _, _ = _two_shifts(plant)
    check_in(plant, flight.id, 1)
    handover = plant.start_handover("SH-A", "SH-B", "transfer")
    plant.confirm_handover(handover.id)
    kinds = plant.audit.by_kind()
    assert kinds["shift_open"] == 2
    assert kinds["handover_start"] == 1
    assert kinds["handover_confirm"] == 1
    stages = plant.audit.by_stage()
    assert stages["shift"] == 2
    assert stages["handover"] == 2
    assert plant.audit.verify() is True


def test_audit_entry_carries_the_responsibility_clause(plant):
    flight, _, _ = _two_shifts(plant)
    check_in(plant, flight.id, 1)
    handover = plant.start_handover("SH-A", "SH-B", "retain")
    entries = plant.audit.for_subject(handover.id)
    start = entries[0]
    assert start.detail["policy"] == "retain"
    assert "继续由交班班负责" in start.detail["responsibility"]
    plant.rollback_handover(handover.id)
    rollback = plant.audit.for_subject(handover.id)[-1]
    assert rollback.kind == "handover_rollback"
    assert rollback.detail["restored"] == 1
    assert plant.audit.verify() is True


def test_handover_audit_entries_share_the_handover_subject(plant):
    flight, _, _ = _two_shifts(plant)
    check_in(plant, flight.id, 1)
    handover = plant.start_handover("SH-A", "SH-B", "transfer")
    plant.confirm_handover(handover.id)
    subjects = [entry.subject for entry in plant.audit.for_subject(handover.id)]
    assert subjects == [handover.id, handover.id]
