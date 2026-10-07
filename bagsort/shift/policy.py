"""Custody policies for in-process bags at a handover.

The two policies trace responsibility in opposite directions, so a handover
must record the chosen policy together with an explicit clause stating how
responsibility is divided.
"""

from __future__ import annotations

from bagsort.errors import ValidationError
from bagsort.model.enums import CustodyPolicy


def policy_of(value: str | CustodyPolicy) -> CustodyPolicy:
    if isinstance(value, CustodyPolicy):
        return value
    try:
        return CustodyPolicy(str(value).strip().lower())
    except ValueError as exc:
        raise ValidationError(
            f"unknown custody policy {value!r}; "
            f"expected {CustodyPolicy.TRANSFER.value!r} or {CustodyPolicy.RELINQUISH.value!r}"
        ) from exc


def responsibility_clause(policy: str | CustodyPolicy) -> str:
    chosen = policy_of(policy)
    if chosen is CustodyPolicy.TRANSFER:
        return (
            "transfer: once the receiving shift confirms this handover, every listed "
            "in-process bag is attributed to the receiving shift; the receiving shift "
            "answers for every problem found after confirmation, even if the cause "
            "originated before the handover, while the outgoing shift answers only for "
            "events recorded before confirmation."
        )
    return (
        "relinquish: the outgoing shift remains attributed to every listed in-process "
        "bag and answers for every problem found on those bags until they are settled; "
        "the receiving shift is only a caretaker and its shift report does not credit "
        "or debit the transferred backlog."
    )


def owner_after_confirmation(policy: str | CustodyPolicy, outgoing: str, incoming: str) -> str:
    """Which shift owns the frozen items once a handover is confirmed."""

    chosen = policy_of(policy)
    return incoming if chosen is CustodyPolicy.TRANSFER else outgoing
