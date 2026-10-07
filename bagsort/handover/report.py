"""Human readable view of shifts, the pending handover and reconciliation."""

from __future__ import annotations

from typing import Any

from .reconcile import Reconciler
from .service import COMPLETED, HandoverService, OPEN, ROLLED_BACK

_STATUS_TEXT = {
    OPEN: "awaiting confirmation",
    COMPLETED: "completed",
    ROLLED_BACK: "rolled back",
}


class ShiftReportView:
    """Formats the handover subsystem for operators and the report endpoint."""

    def __init__(self, service: HandoverService, reconciler: Reconciler) -> None:
        self._service = service
        self._reconciler = reconciler

    def summary(self) -> dict[str, Any]:
        open_handover = self._service.open_handover()
        current = self._service.current_shift()
        return {
            "current_shift": current.id if current else "",
            "open_shifts": len(self._service.open_shifts()),
            "handovers": len(self._service.list_handovers()),
            "pending_handover": open_handover.id if open_handover else "",
            "handover_status": open_handover.status if open_handover else "",
            "reconciliation": self._reconciler.verify(),
        }

    def text(self) -> str:
        summary = self.summary()
        lines = [
            f"current shift     : {summary['current_shift'] or '-'}",
            f"open shifts       : {summary['open_shifts']}",
            f"handover pending  : {summary['pending_handover'] or '-'}",
        ]
        pending = self._service.open_handover()
        if pending is not None:
            lines.append(
                "  in progress     : "
                f"{pending.summary.get('in_progress', 0)} "
                f"({pending.policy}, {_STATUS_TEXT[pending.status]})"
            )
        verification = summary["reconciliation"]
        failed = [name for name, ok in verification["checks"].items() if not ok]
        lines.append(
            "reconciliation    : " + ("ok" if verification["ok"] else f"broken: {', '.join(failed)}")
        )
        for report in self._reconciler.reports():
            lines.append(
                f"shift {report.shift_id:<12}: active={report.active} "
                f"sorted={report.sorted_total} {report.by_state}"
            )
        return "\n".join(lines) + "\n"
