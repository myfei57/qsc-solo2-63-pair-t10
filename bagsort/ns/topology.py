"""The physical order of the handling line.

Every stage the plant performs is placed on one ordered line. Steps that claim
to happen out of that order are rejected here rather than being silently
accepted, which is what makes the ordering contracts testable.
"""

from __future__ import annotations

from dataclasses import dataclass

from bagsort.errors import OrderingError

STAGES: tuple[str, ...] = (
    "checkin",
    "tag",
    "inject",
    "sorter",
    "chute",
    "recheck",
    "belt",
    "audit",
)

# Governance steps are not positions on the physical handling line; they sit
# outside it and order themselves after every plant stage for the timeline.
GOVERNANCE_STAGES: tuple[str, ...] = ("shift", "handover")
ALL_STAGES: tuple[str, ...] = STAGES[:-1] + GOVERNANCE_STAGES + ("audit",)


@dataclass(frozen=True)
class Topology:
    """An ordered line of stage names."""

    name: str
    stages: tuple[str, ...]
    extra_stages: tuple[str, ...] = ()

    def index_of(self, stage: str) -> int:
        ordered = self.stages + self.extra_stages
        try:
            return ordered.index(stage)
        except ValueError:
            return -1

    def contains(self, stage: str) -> bool:
        return stage in self.stages or stage in self.extra_stages

    def before(self, first: str, second: str) -> bool:
        first_index = self.index_of(first)
        second_index = self.index_of(second)
        return first_index >= 0 and second_index >= 0 and first_index < second_index

    def ensure_before(self, first: str, second: str) -> None:
        if not self.before(first, second):
            raise OrderingError(f"{first!r} must be handled before {second!r}")

    def next_stage(self, stage: str) -> str | None:
        index = self.stages.index(stage) if stage in self.stages else -1
        if index < 0 or index + 1 >= len(self.stages):
            return None
        return self.stages[index + 1]

    def following(self, stage: str) -> tuple[str, ...]:
        try:
            index = self.stages.index(stage)
        except ValueError:
            return ()
        return tuple(self.stages[index + 1 :])

    def describe(self) -> str:
        return " -> ".join(self.stages)

    def count(self) -> int:
        return len(self.stages)


def handling_line() -> Topology:
    """The canonical order of the baggage handling line."""

    return Topology(name="handling", stages=STAGES, extra_stages=GOVERNANCE_STAGES)
