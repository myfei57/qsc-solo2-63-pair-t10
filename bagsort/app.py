"""Assembly of the handling line.

Every subsystem is constructed here and every operator visible action is a
method on :class:`Plant`, which is what keeps the console handlers thin and
lets the CLI drive the same operations the HTTP API exposes.
"""

from __future__ import annotations

from dataclasses import dataclass

from bagsort.audit.chain import AuditChain
from bagsort.belt.belt import Belt
from bagsort.checkin.desk import Desk
from bagsort.clock import Clock
from bagsort.errors import ConflictError
from bagsort.events.bus import EventBus
from bagsort.flight.book import FlightBook
from bagsort.flight.close import FlightCloser
from bagsort.flight.mapping import BagMapping
from bagsort.flight.schedule import ScheduleBoard
from bagsort.ids import IdFactory
from bagsort.inject.injector import Injector
from bagsort.model.entities import Bag, Flight, Handover, Reading, Shift, SortRecord
from bagsort.model.enums import CustodyPolicy
from bagsort.model.errors import unknown_bag
from bagsort.ns.chutes import ChuteRegistry
from bagsort.ns.namespace import Namespace
from bagsort.ns.topology import handling_line
from bagsort.quota.quota import QuotaBook
from bagsort.recheck.queue import RecheckQueue
from bagsort.reporting.report import PlantReport
from bagsort.sequence.verdict import VerdictTracker
from bagsort.shift.book import ShiftBook
from bagsort.shift.custody import ShiftCustody
from bagsort.shift.handover import HandoverManager
from bagsort.shift.history import HandoverHistory
from bagsort.shift.sheet import ShiftSheet
from bagsort.sorter.sorter import Sorter
from bagsort.store.store import Store
from bagsort.tag.history import ReadHistory
from bagsort.tag.reader import TagReader
from bagsort.chute.assigner import ChuteAssigner
from bagsort.chute.history import AssignmentHistory
from bagsort.console.metrics import Metrics
from bagsort.console.telemetry import Telemetry

DEFAULT_TERMINAL = "T1"


@dataclass
class Plant:
    """The wired plant plus the operator level operations the console calls."""

    store: Store
    clock: Clock
    namespace: Namespace
    registry: ChuteRegistry
    bus: EventBus
    telemetry: Telemetry
    metrics: Metrics
    reader: TagReader
    reads: ReadHistory
    book: FlightBook
    mapping: BagMapping
    schedule: ScheduleBoard
    closer: FlightCloser
    chutes: ChuteAssigner
    chute_history: AssignmentHistory
    sorter: Sorter
    injector: Injector
    desk: Desk
    recheck: RecheckQueue
    belt: Belt
    quota: QuotaBook
    audit: AuditChain
    report: PlantReport
    custody: ShiftCustody
    shifts: ShiftBook
    handovers: HandoverManager
    handover_history: HandoverHistory
    sheet: ShiftSheet

    @property
    def topology(self):
        return handling_line()

    # -- construction ------------------------------------------------------

    @classmethod
    def build(
        cls,
        store: Store,
        terminal: str = DEFAULT_TERMINAL,
        clock: Clock | None = None,
    ) -> "Plant":
        clock = clock or Clock()
        namespace = Namespace(terminal)
        registry = ChuteRegistry()
        bus = EventBus()
        telemetry = Telemetry()
        metrics = Metrics()
        bus.subscribe("*", lambda event: telemetry.observe(event.topic))

        reader = TagReader(store, namespace, clock)
        reads = ReadHistory(store)
        book = FlightBook(store, clock)
        mapping = BagMapping(store, clock)
        schedule = ScheduleBoard(store, clock)
        chutes = ChuteAssigner(store, clock)
        chute_history = AssignmentHistory(store)
        sorter = Sorter(store, mapping, chutes, clock, VerdictTracker())
        injector = Injector(store, book, sorter, clock)
        quota = QuotaBook(store, clock)
        desk = Desk(store, reader, book, mapping, injector, quota, clock, handling_line())
        closer = FlightCloser(book)
        recheck = RecheckQueue(store, book, clock)
        belt = Belt(store, clock, IdFactory("cmd"))

        audit = AuditChain(store, clock, handling_line())
        audit_subscriber = _AuditSubscriber(audit)
        for topic in (
            "bag.checked_in",
            "bag.sorted",
            "bag.diverted",
            "bag.recheck_enqueued",
            "flight.closed",
            "chute.assigned",
            "belt.switched",
            "bag.remapped",
            "shift.opened",
            "shift.closed",
            "handover.proposed",
            "handover.confirmed",
            "handover.rolled_back",
        ):
            bus.subscribe(topic, audit_subscriber)

        custody = ShiftCustody(store)
        shift_book = ShiftBook(store, custody, clock)
        handover_manager = HandoverManager(store, custody, clock, IdFactory("hnd"))
        handover_history = HandoverHistory(store)
        sheet = ShiftSheet(store, custody)

        report = PlantReport(
            store, book, chutes, belt, quota, audit, namespace, handling_line()
        )
        return cls(
            store=store,
            clock=clock,
            namespace=namespace,
            registry=registry,
            bus=bus,
            telemetry=telemetry,
            metrics=metrics,
            reader=reader,
            reads=reads,
            book=book,
            mapping=mapping,
            schedule=schedule,
            closer=closer,
            chutes=chutes,
            chute_history=chute_history,
            sorter=sorter,
            injector=injector,
            desk=desk,
            recheck=recheck,
            belt=belt,
            quota=quota,
            audit=audit,
            report=report,
            custody=custody,
            shifts=shift_book,
            handovers=handover_manager,
            handover_history=handover_history,
            sheet=sheet,
        )

    # -- operator operations ----------------------------------------------

    def register_flight(self, code: str, carousel: str = "", chute: str = "") -> Flight:
        flight = Flight(
            id=self.namespace.flight_id(code),
            code=code.strip().upper(),
            terminal=self.namespace.terminal,
            carousel=self.namespace.carousel_id(carousel) if carousel else "",
        )
        self.book.register(flight)
        if chute:
            self.assign_chute(flight.id, self.namespace.chute_id(chute))
        return self.book.get(flight.id)

    def assign_chute(self, flight_id: str, chute_id: str):
        assignment = self.chutes.assign(flight_id, chute_id)
        self.bus.publish("chute.assigned", flight_id=flight_id, chute_id=chute_id)
        return assignment

    def read_barcode(self, barcode: str) -> Reading:
        return self.reader.read(barcode)

    def check_in(self, barcode: str, flight_id: str) -> Bag:
        bag = self.desk.check_in(barcode, flight_id)
        self.shifts.claim(bag.id)
        self.bus.publish("bag.checked_in", bag_id=bag.id, flight_id=flight_id)
        return bag

    def cancel_check_in(self, bag_id: str) -> Bag:
        bag = self.desk.cancel(bag_id)
        self.bus.publish("bag.diverted", bag_id=bag.id, reason="checkin_cancelled")
        return bag

    def check_in_batch(self, entries: list) -> list[Bag]:
        return [self.check_in(entry.barcode, entry.flight_id) for entry in entries]

    def bag(self, bag_id: str) -> Bag:
        bag = self.store.state().bags.get(bag_id)
        if bag is None:
            raise unknown_bag(bag_id)
        return bag

    def push_bag(self, bag_id: str, chute_id: str) -> SortRecord:
        bag = self.bag(bag_id)
        record = self.injector.push(bag, chute_id)
        self.bus.publish("bag.sorted", bag_id=bag.id, chute_id=record.chute_id)
        return record

    def retry_bag(self, bag_id: str) -> bool:
        bag = self.bag(bag_id)
        sorted_now = self.injector.retry(bag)
        if sorted_now:
            self.bus.publish("bag.sorted", bag_id=bag.id, retry=True)
        return sorted_now

    def divert_bag(self, bag_id: str) -> Bag:
        bag = self.bag(bag_id)
        updated = self.sorter.divert(bag)
        self.bus.publish("bag.diverted", bag_id=bag.id)
        return updated

    def remap_bag(self, bag_id: str, flight_id: str) -> Bag:
        self.book.require_open(flight_id)
        self.mapping.remap(bag_id, flight_id)
        self.bus.publish("bag.remapped", bag_id=bag_id, flight_id=flight_id)
        return self.bag(bag_id)

    def revise_schedule(self, flight_id: str, cutoff_at: str):
        return self.schedule.revise(flight_id, cutoff_at)

    def close_flight(self, flight_id: str) -> Flight:
        flight = self.closer.close(flight_id, self.injector)
        self.bus.publish("flight.closed", flight_id=flight_id)
        return flight

    def enqueue_recheck(self, bag_id: str) -> Bag:
        bag = self.bag(bag_id)
        self.recheck.enqueue(bag)
        self.bus.publish("bag.recheck_enqueued", bag_id=bag_id)
        return bag

    def process_recheck(self) -> Bag | None:
        return self.recheck.dequeue()

    def switch_belt(self, carousel: str, batch: int) -> int:
        cleared = self.belt.switch(carousel, batch)
        self.bus.publish("belt.switched", carousel=carousel, batch=batch)
        return cleared

    def rotate_belt(self) -> int:
        return self.belt.rotate()

    def append_transfer(self, flight_id: str, carousel: str, batch: int):
        return self.belt.append_transfer(flight_id, carousel, batch)

    def set_quota(self, flight_id: str, limit: int) -> int:
        return self.quota.set_limit(flight_id, limit)

    def verify(self) -> bool:
        return self.audit.verify()

    # -- shifts and handovers ---------------------------------------------

    def open_shift(self, shift_id: str, code: str = "") -> Shift:
        shift = self.shifts.open(shift_id, code)
        self.bus.publish("shift.opened", shift_id=shift.id)
        return shift

    def close_shift(self, shift_id: str) -> Shift:
        pending = self.handovers.open_proposal()
        if pending is not None and shift_id in (
            pending.outgoing_shift_id,
            pending.incoming_shift_id,
        ):
            raise ConflictError(
                f"shift {shift_id!r} is part of pending handover {pending.id!r}; "
                "confirm or roll it back first"
            )
        shift = self.shifts.close(shift_id)
        self.bus.publish("shift.closed", shift_id=shift.id)
        return shift

    def attribute_bag(self, bag_id: str, shift_id: str) -> Bag:
        """Bind an existing active bag to a shift and return its current record."""

        self.bag(bag_id)
        self.shifts.claim(bag_id, shift_id)
        return self.bag(bag_id)

    def propose_handover(
        self,
        outgoing_shift_id: str,
        incoming_shift_id: str,
        policy: str = CustodyPolicy.TRANSFER.value,
        note: str = "",
    ) -> Handover:
        outgoing = self.shifts.require_open(outgoing_shift_id)
        incoming = self.shifts.require_open(incoming_shift_id)
        handover = self.handovers.propose(outgoing, incoming, policy, note)
        self.bus.publish(
            "handover.proposed",
            handover_id=handover.id,
            shift_id=outgoing_shift_id,
            incoming_shift_id=incoming_shift_id,
            policy=handover.policy.value,
            item_count=len(handover.items),
            inventory_digest=handover.inventory_digest,
            responsibility=handover.responsibility,
        )
        return handover

    def confirm_handover(self, handover_id: str) -> Handover:
        handover = self.handovers.confirm(handover_id)
        self.bus.publish(
            "handover.confirmed",
            handover_id=handover.id,
            shift_id=handover.outgoing_shift_id,
            incoming_shift_id=handover.incoming_shift_id,
            policy=handover.policy.value,
            item_count=len(handover.items),
            inventory_digest=handover.inventory_digest,
            responsibility=handover.responsibility,
        )
        return handover

    def rollback_handover(self, handover_id: str) -> Handover:
        handover = self.handovers.rollback(handover_id)
        self.bus.publish(
            "handover.rolled_back",
            handover_id=handover.id,
            shift_id=handover.outgoing_shift_id,
            incoming_shift_id=handover.incoming_shift_id,
            item_count=len(handover.items),
            inventory_digest=handover.inventory_digest,
        )
        return handover


class _AuditSubscriber:
    """Turns published operations into linked audit entries."""

    _STAGE = {
        "bag.checked_in": ("checkin", "checkin"),
        "bag.sorted": ("sort", "sorter"),
        "bag.diverted": ("diversion", "sorter"),
        "bag.recheck_enqueued": ("recheck", "recheck"),
        "flight.closed": ("flight_close", "audit"),
        "chute.assigned": ("chute_assign", "chute"),
        "belt.switched": ("belt_switch", "belt"),
        "bag.remapped": ("remap", "checkin"),
        "shift.opened": ("shift_open", "audit"),
        "shift.closed": ("shift_close", "audit"),
        "handover.proposed": ("handover_propose", "audit"),
        "handover.confirmed": ("handover_confirm", "audit"),
        "handover.rolled_back": ("handover_rollback", "audit"),
    }

    def __init__(self, audit: AuditChain) -> None:
        self._audit = audit

    def __call__(self, event) -> None:
        mapping = self._STAGE.get(event.topic)
        if mapping is None:
            raise ConflictError(f"no audit mapping for topic {event.topic!r}")
        kind, stage = mapping
        subject = str(
            event.payload.get("handover_id")
            or event.payload.get("bag_id")
            or event.payload.get("flight_id")
            or event.payload.get("shift_id")
            or event.payload.get("carousel")
            or ""
        )
        detail = {
            key: value
            for key, value in event.payload.items()
            if key not in {"bag_id", "flight_id", "handover_id", "shift_id"}
        }
        self._audit.record(kind, subject, stage, detail)
