"""Ownership policies and the manifest digest that freezes an in-progress list.

Two policies are offered on purpose: assigning the in-progress bags to the
outgoing shift or to the receiving shift have opposite effects on who answers
for them later, so each policy carries the matching, human readable liability
text that is stored on the handover and written into the audit trail.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

TRANSFER = "transfer"
RETAIN = "retain"
POLICIES: tuple[str, ...] = (TRANSFER, RETAIN)

RESPONSIBILITY: dict[str, str] = {
    TRANSFER: (
        "在办行李随交接移交给接收班；自接班确认之时起，清单内行李的处置与异常"
        "由接收班负责，交班人仅对交接开始前已产生且记录在案的分拣行为负责。"
    ),
    RETAIN: (
        "在办行李不随交接移交，仍归交班班（挂账于交班班次）；清单内行李的处置"
        "与异常继续由交班班负责，接收班仅负责接班确认之后新产生的在办行李。"
    ),
}


def responsibility_text(policy: str) -> str:
    """Return the liability clause that must accompany a chosen policy."""

    try:
        return RESPONSIBILITY[policy]
    except KeyError:
        raise ValueError(f"unknown handover policy {policy!r}") from None


def manifest_digest(items: Iterable[dict[str, Any]]) -> str:
    """Stable digest over the frozen manifest content.

    Rows are ordered by ``bag_id`` and only identity, state and the ownership
    captured when the handover opened take part, so the digest pins both the
    membership of the in-progress list and the pre-handover ownership.
    """

    rows = sorted(
        (
            [str(item["bag_id"]), str(item.get("state", "")), str(item.get("shift_id", ""))]
            for item in items
        ),
        key=lambda row: row[0],
    )
    material = json.dumps(
        rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
