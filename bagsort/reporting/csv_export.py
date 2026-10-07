"""CSV rendering used by the export endpoints."""

from __future__ import annotations

import csv
import io
import json
from typing import Iterable, Sequence


def render(columns: Sequence[str], rows: Iterable[dict[str, object]]) -> str:
    """Render a header plus one line per row, in the column order given."""

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(columns), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in columns})
    return buffer.getvalue()


def audit_rows(entries: Iterable[object]) -> list[dict[str, object]]:
    return [
        {
            "sequence": entry.sequence,
            "kind": entry.kind,
            "subject": entry.subject,
            "stage": entry.detail.get("stage", ""),
            "at": entry.at,
            "digest": entry.digest,
        }
        for entry in entries
    ]


def sort_rows(records: Iterable[object]) -> list[dict[str, object]]:
    return [
        {
            "bag_id": record.bag_id,
            "flight_id": record.flight_id,
            "chute_id": record.chute_id,
            "sequence": record.sequence,
            "epoch": record.epoch,
            "at": record.at,
            "shift_id": getattr(record, "shift_id", ""),
        }
        for record in records
    ]


def load_rows(records: Iterable[object]) -> list[dict[str, object]]:
    return [
        {
            "bag_id": record.bag_id,
            "carousel": record.carousel,
            "batch": record.batch,
            "at": record.at,
        }
        for record in records
    ]


AUDIT_COLUMNS = ("sequence", "kind", "subject", "stage", "at", "digest")
SORT_COLUMNS = ("bag_id", "flight_id", "chute_id", "sequence", "epoch", "at", "shift_id")
LOAD_COLUMNS = ("bag_id", "carousel", "batch", "at")
HANDOVER_COLUMNS = (
    "id",
    "shift_from",
    "shift_to",
    "status",
    "policy",
    "started_at",
    "completed_at",
    "rolled_back_at",
    "in_progress",
    "responsibility",
)


def handover_rows(handovers: Iterable[object]) -> list[dict[str, object]]:
    return [
        {
            "id": handover.id,
            "shift_from": handover.shift_from,
            "shift_to": handover.shift_to,
            "status": handover.status,
            "policy": handover.policy,
            "started_at": handover.started_at,
            "completed_at": handover.completed_at,
            "rolled_back_at": handover.rolled_back_at,
            "in_progress": handover.summary.get("in_progress", 0),
            "responsibility": handover.responsibility,
        }
        for handover in handovers
    ]


def shift_rows(reports: Iterable[object]) -> list[dict[str, object]]:
    return [
        {
            "shift_id": report.shift_id,
            "active": report.active,
            "sorted_total": report.sorted_total,
            "by_state": json.dumps(report.by_state, ensure_ascii=False, sort_keys=True),
        }
        for report in reports
    ]


SHIFT_REPORT_COLUMNS = ("shift_id", "active", "sorted_total", "by_state")
