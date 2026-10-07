"""Console endpoints for shifts and handovers."""

from __future__ import annotations

import json

from tests.test_console_api import bootstrap, call


def _open_first_shift(server):
    status, morning = call(server, "POST", "/api/shifts", {"shift_id": "S1", "code": "morning"})
    assert status == 201
    return morning


def _open_second_shift(server):
    status, evening = call(server, "POST", "/api/shifts", {"shift_id": "S2", "code": "evening"})
    assert status == 201
    return evening


def _open_both(server):
    _open_first_shift(server)
    _open_second_shift(server)


def test_shift_lifecycle_endpoints(server):
    status, shifts = call(server, "GET", "/api/shifts")
    assert (status, shifts) == (200, [])

    status, shift = call(server, "POST", "/api/shifts", {"shift_id": "S1"})
    assert status == 201 and shift["state"] == "open"

    status, current = call(server, "GET", "/api/shifts/current")
    assert current["id"] == "S1"

    status, one = call(server, "GET", "/api/shifts/S1")
    assert one["code"] == "S1"

    status, closed = call(server, "POST", "/api/shifts/S1/close")
    assert status == 200 and closed["state"] == "closed"

    status, missing = call(server, "GET", "/api/shifts/NOPE")
    assert status == 404


def test_handover_round_trip_through_the_console(server):
    flight_id = bootstrap(server)
    _open_first_shift(server)

    status, bag = call(
        server, "POST", "/api/bags/checkin", {"barcode": "7001", "flight_id": flight_id}
    )
    assert status == 201
    status, owned = call(server, "GET", "/api/shifts/S1/bags")
    assert owned == [bag["id"]]

    _open_second_shift(server)
    payload = {
        "outgoing_shift_id": "S1",
        "incoming_shift_id": "S2",
        "policy": "transfer",
        "note": "end of morning",
    }
    status, handover = call(server, "POST", "/api/handovers", payload)
    assert status == 201 and handover["state"] == "proposed"
    handover_id = handover["id"]
    assert handover["items"][0]["bag_id"] == bag["id"]
    assert handover["responsibility"]

    status, inventory = call(server, "GET", f"/api/handovers/{handover_id}/inventory")
    assert status == 200
    assert inventory["intact"] is True
    assert inventory["digest"] == handover["inventory_digest"]

    status, confirmed = call(server, "POST", f"/api/handovers/{handover_id}/confirm")
    assert status == 200 and confirmed["state"] == "confirmed"

    status, history = call(server, "GET", f"/api/handovers/{handover_id}/history")
    assert [item["state"] for item in history] == ["proposed", "confirmed"]

    status, recomputed = call(server, "GET", "/api/shifts/S1/handovers")
    assert recomputed["confirmed"] == [handover_id]


def test_rollback_endpoint_restores_ownership(server):
    flight_id = bootstrap(server)
    _open_first_shift(server)
    status, bag = call(
        server, "POST", "/api/bags/checkin", {"barcode": "8001", "flight_id": flight_id}
    )
    assert status == 201 and bag["id"]
    _open_second_shift(server)
    status, handover = call(
        server,
        "POST",
        "/api/handovers",
        {"outgoing_shift_id": "S1", "incoming_shift_id": "S2"},
    )
    handover_id = handover["id"]
    status, rolled = call(server, "POST", f"/api/handovers/{handover_id}/rollback")
    assert status == 200 and rolled["state"] == "rolled_back"

    status, recomputed = call(server, "GET", "/api/shifts/S1/handovers")
    assert recomputed["rolled_back"] == [handover_id]
    assert recomputed["matches_state"] is True


def test_reports_and_reconcile_endpoints(server):
    flight_id = bootstrap(server)
    _open_first_shift(server)
    call(server, "POST", "/api/bags/checkin", {"barcode": "9001", "flight_id": flight_id})
    _open_second_shift(server)

    status, reports = call(server, "GET", "/api/shifts/report")
    by_shift = {row["shift_id"]: row for row in reports}
    assert by_shift["S1"]["active_bags"] == 1

    status, report = call(server, "GET", "/api/shifts/report", query={"shift_id": ["S2"]})
    assert status == 200 and report["shift_id"] == "S2"

    status, result = call(server, "GET", "/api/shifts/reconcile")
    assert status == 200 and result["consistent"] is True


def test_handover_conflict_is_reported_with_409(server):
    bootstrap(server)
    _open_both(server)
    call(
        server,
        "POST",
        "/api/handovers",
        {"outgoing_shift_id": "S1", "incoming_shift_id": "S2"},
    )
    status, body = call(
        server,
        "POST",
        "/api/handovers",
        {"outgoing_shift_id": "S1", "incoming_shift_id": "S2"},
    )
    assert status == 409 and "error" in body


def test_handovers_can_be_filtered_by_shift(server):
    bootstrap(server)
    _open_both(server)
    status, handover = call(
        server,
        "POST",
        "/api/handovers",
        {"outgoing_shift_id": "S1", "incoming_shift_id": "S2"},
    )
    status, listing = call(server, "GET", "/api/handovers", query={"shift_id": ["S2"]})
    assert status == 200 and [item["id"] for item in listing] == [handover["id"]]


def test_catalog_contains_shift_routes(server):
    status, catalog = call(server, "GET", "/api/catalog")
    paths = {row["path"] for row in catalog}
    assert "/api/shifts/{id}/handovers" in paths
    assert "/api/handovers/{id}/rollback" in paths
    body = json.dumps(catalog)
    assert status == 200 and body
