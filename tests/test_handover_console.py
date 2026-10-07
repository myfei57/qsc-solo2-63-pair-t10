"""Console endpoints for shifts, handovers, inventory and reconciliation."""

from __future__ import annotations

from tests.support import check_in, register_flight


def call(server, method, path, payload=None, query=None):
    response = server.call(method, path, payload, query)
    import json

    return response.status, json.loads(response.body.decode("utf-8"))


def _prepare(server, policy="transfer", bags=2):
    status, flight = call(
        server, "POST", "/api/flights", {"code": "CA1234", "carousel": "CR1"}
    )
    call(server, "POST", "/api/chutes/assign", {"flight_id": flight["id"], "chute_id": "T1-CHA"})
    call(server, "POST", "/api/shifts", {"id": "SH-A", "label": "morning"})
    call(server, "POST", "/api/shifts", {"id": "SH-B", "label": "evening"})
    checked = check_in(server.plant, flight["id"], bags)
    server.plant.push_bag(checked[0].id, "T1-CHA")
    return flight, checked


def test_full_handover_lifecycle_over_http(server):
    flight, bags = _prepare(server)

    status, current = call(server, "GET", "/api/shifts/current")
    assert current["id"] == "SH-A"

    status, handover = call(
        server,
        "POST",
        "/api/handovers",
        {"shift_from": "SH-A", "shift_to": "SH-B", "policy": "transfer"},
    )
    assert status == 201 and handover["status"] == "open"
    handover_id = handover["id"]

    status, pending = call(server, "GET", "/api/handovers/pending")
    assert pending["id"] == handover_id

    status, manifest = call(server, "GET", f"/api/handovers/{handover_id}/manifest")
    assert manifest["summary"]["in_progress"] == 2
    assert len(manifest["items"]) == 2

    status, done = call(server, "POST", f"/api/handovers/{handover_id}/confirm")
    assert status == 200 and done["status"] == "completed"
    assert done["reconciliation"]["ok"] is True

    status, bag = call(server, "GET", f"/api/bags/{bags[0].id}")
    assert bag["shift_id"] == "SH-B"

    status, report_b = call(server, "GET", "/api/shifts/SH-B/report")
    assert report_b["active"] == 2
    assert report_b["sorted_total"] == 0

    status, ledger = call(server, "GET", "/api/shifts/SH-B/recompute")
    assert ledger["received"][handover_id] == sorted(bag.id for bag in bags)


def test_rollback_endpoint_restores_ownership_and_reports_consistency(server):
    flight, bags = _prepare(server)
    _, handover = call(
        server,
        "POST",
        "/api/handovers",
        {"shift_from": "SH-A", "shift_to": "SH-B", "policy": "transfer"},
    )
    status, rolled = call(
        server,
        "POST",
        f"/api/handovers/{handover['id']}/rollback",
        {"note": "discrepancy"},
    )
    assert status == 200
    assert rolled["status"] == "rolled_back"
    assert rolled["reconciliation"]["ok"] is True

    status, counts = call(server, "GET", "/api/in-progress/counts")
    assert counts == {"SH-A": 2}


def test_in_progress_and_reconciliation_endpoints(server):
    flight, bags = _prepare(server)
    status, rows = call(
        server, "GET", "/api/in-progress", query={"shift_id": ["SH-A"]}
    )
    assert status == 200 and rows["count"] == 2

    status, verdict = call(server, "GET", "/api/reconciliation")
    assert verdict["ok"] is True
    assert verdict["checks"]["single_owner"] is True
    assert verdict["ownership_conflicts"] == []
    assert verdict["audit_intact"] is True


def test_handover_history_and_shift_filter(server):
    flight, bags = _prepare(server, policy="retain")
    _, handover = call(
        server,
        "POST",
        "/api/handovers",
        {"shift_from": "SH-A", "shift_to": "SH-B", "policy": "retain"},
    )
    call(server, "POST", f"/api/handovers/{handover['id']}/confirm")
    status, listed = call(
        server, "GET", "/api/handovers", query={"shift_id": ["SH-B"]}
    )
    assert [item["id"] for item in listed] == [handover["id"]]
    status, all_handovers = call(server, "GET", "/api/handovers")
    assert len(all_handovers) == 1


def test_csv_exports_cover_handovers_and_shift_reports(server):
    flight, bags = _prepare(server)
    _, handover = call(
        server,
        "POST",
        "/api/handovers",
        {"shift_from": "SH-A", "shift_to": "SH-B", "policy": "transfer"},
    )
    call(server, "POST", f"/api/handovers/{handover['id']}/confirm")

    response = server.call("GET", "/api/export/handovers.csv")
    text = response.body.decode("utf-8")
    assert response.status == 200
    assert "shift_from" in text and handover["id"] in text
    assert "接收班负责" in text

    response = server.call("GET", "/api/export/shifts.csv")
    text = response.body.decode("utf-8")
    assert "SH-B" in text and "shift_id" in text


def test_unknown_handover_and_bad_policy_are_404_and_400(server):
    _prepare(server)
    status, body = call(server, "GET", "/api/handovers/HD-999999")
    assert status == 404
    status, body = call(
        server,
        "POST",
        "/api/handovers",
        {"shift_from": "SH-A", "shift_to": "SH-B", "policy": "nope"},
    )
    assert status == 400
