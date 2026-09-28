from __future__ import annotations

import logging
import random
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass

from .config import LinkConfig
from .framing import BROADCAST_ID, PacketKind, RadioPacket
from .mavlink import TrafficPriority
from .statistics import LinkStatistics

LOG = logging.getLogger(__name__)
RELIABLE_DATA_KINDS = (PacketKind.DATA, PacketKind.MAVLINK_BUNDLE)
DATA_KINDS = RELIABLE_DATA_KINDS + (
    PacketKind.UNRELIABLE_DATA,
    PacketKind.UNRELIABLE_MAVLINK_BUNDLE,
)


@dataclass
class QueuedPacket:
    packet: RadioPacket
    priority: TrafficPriority = TrafficPriority.NORMAL
    attempts: int = 0
    next_attempt: float = 0.0
    enqueued_at: float = 0.0
    flow_key: tuple[int | None, ...] | None = None
    mavlink_frames: tuple[tuple[str | None, float], ...] = ()
    max_age_seconds: float | None = None


class TokenBucket:
    def __init__(self, rate: float, burst: float, clock):
        self.rate = rate
        self.capacity = burst if burst > 0 else rate
        self.tokens = self.capacity
        self.clock = clock
        self.updated_at = clock()

    def consume(self, amount: int) -> bool:
        if self.rate <= 0:
            return True
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated_at) * self.rate)
        self.updated_at = now
        if self.tokens < amount:
            return False
        self.tokens -= amount
        return True


class ReliableLink:
    """Bounded TX queue, unicast ARQ, and receive duplicate suppression."""

    def __init__(
        self,
        node_id: int,
        config: LinkConfig,
        clock=time.monotonic,
        statistics: LinkStatistics | None = None,
        session_id: int = 0,
        encoded_size: Callable[[RadioPacket], int] | None = None,
    ):
        self.node_id = node_id
        self.config = config
        self.clock = clock
        self.statistics = statistics or LinkStatistics(clock)
        self.session_id = session_id
        self.encoded_size = encoded_size or (lambda packet: len(packet.encode()))
        self.queue: deque[QueuedPacket] = deque()
        self.awaiting_ack: dict[tuple[int, int], QueuedPacket] = {}
        self.seen: OrderedDict[tuple[int, int], float] = OrderedDict()
        self.telemetry_bucket = TokenBucket(
            config.telemetry_rate_bytes_per_second,
            config.telemetry_burst_bytes,
            clock,
        )
        self.bulk_bucket = TokenBucket(
            config.bulk_rate_bytes_per_second,
            config.bulk_burst_bytes,
            clock,
        )

    def enqueue(
        self,
        packet: RadioPacket,
        *,
        priority: TrafficPriority | None = None,
        flow_key: tuple[int | None, ...] | None = None,
        message_type: str | None = None,
        max_age_seconds: float | None = None,
    ) -> bool:
        if priority is None:
            priority = (
                TrafficPriority.LINK_CONTROL
                if packet.kind in (PacketKind.ACK, PacketKind.SYNC)
                else TrafficPriority.NORMAL
            )
        now = self.clock()
        self._expire_queued(now)
        if (
            self.config.coalesce_telemetry
            and priority <= TrafficPriority.NORMAL
            and flow_key is not None
        ):
            for queued in self.queue:
                if queued.flow_key == flow_key and queued.attempts == 0:
                    queued.packet = packet
                    queued.enqueued_at = now
                    queued.mavlink_frames = ((message_type, now),)
                    queued.max_age_seconds = max_age_seconds
                    self.statistics.increment("telemetry_coalesced")
                    self.statistics.increment_mavlink(message_type, "coalesced")
                    return True
        low_priority_limit = self.config.queue_size - self.config.reserved_high_priority_slots
        if priority >= TrafficPriority.NORMAL and len(self.queue) >= low_priority_limit:
            worst = self.queue[-1]
            if worst.priority > priority and worst.priority >= TrafficPriority.NORMAL:
                self.queue.pop()
                self.statistics.increment("queue_evictions")
            else:
                LOG.warning("reserved queue capacity; dropping priority-%s packet", priority.name)
                self.statistics.increment("queue_drops")
                return False
        if len(self.queue) >= self.config.queue_size:
            worst = self.queue[-1]
            if priority >= worst.priority:
                LOG.warning(
                    "TX queue full; dropping priority-%s packet for node %d",
                    priority.name,
                    packet.destination,
                )
                self.statistics.increment("queue_drops")
                return False
            dropped = self.queue.pop()
            self.statistics.increment("queue_evictions")
            LOG.warning(
                "TX queue full; evicted priority-%s packet for priority-%s traffic",
                dropped.priority.name,
                priority.name,
            )
        queued = QueuedPacket(
            packet,
            priority=priority,
            next_attempt=now,
            enqueued_at=now,
            flow_key=flow_key,
            mavlink_frames=((message_type, now),) if message_type is not None else (),
            max_age_seconds=max_age_seconds,
        )
        self._insert(queued)
        self.statistics.increment("packets_enqueued")
        self._update_gauges()
        return True

    def _insert(self, queued: QueuedPacket) -> None:
        for index, existing in enumerate(self.queue):
            if queued.priority < existing.priority:
                self.queue.insert(index, queued)
                return
        self.queue.append(queued)

    def next_packet(
        self,
        channel_clear: bool,
        tx_allowed: bool,
        preferred_message_types: frozenset[str] | None = None,
        deferred_message_types: frozenset[str] | None = None,
    ) -> QueuedPacket | None:
        now = self.clock()
        self._expire_seen(now)
        self._expire_queued(now)
        for key, queued in list(self.awaiting_ack.items()):
            if now < queued.next_attempt:
                continue
            if queued.attempts > self.config.max_retries:
                LOG.warning("no ACK from node %d for sequence %d", key[0], key[1])
                del self.awaiting_ack[key]
                self._update_gauges()
            elif queued not in self.queue:
                self._insert(queued)
                self.statistics.increment("retries")
                self._update_gauges()
        if not tx_allowed or not channel_clear or not self.queue:
            return None
        candidates = list(self.queue)
        if preferred_message_types:
            candidates.sort(
                key=lambda queued: not any(
                    message_type in preferred_message_types
                    for message_type, _enqueued_at in queued.mavlink_frames
                )
            )
        if deferred_message_types:
            candidates.sort(
                key=lambda queued: any(
                    message_type in deferred_message_types
                    for message_type, _enqueued_at in queued.mavlink_frames
                )
            )
        for queued in candidates:
            if (
                now < queued.next_attempt
                or self._blocked_by_inflight(queued)
                or not self._rate_allows(queued)
            ):
                continue
            self.queue.remove(queued)
            self._update_gauges()
            return queued
        return None

    def _blocked_by_inflight(self, queued: QueuedPacket) -> bool:
        """Hold new unicast data until its peer acknowledges the prior frame.

        B2xx radios operate half duplex in this application.  Sending a burst of
        new data frames before listening for their ACKs starves the peer's return
        path and turns normal traffic into a retry storm.  Retries of the current
        frame and link-control packets must remain eligible.
        """
        packet = queued.packet
        if (
            queued.attempts > 0
            or packet.kind not in RELIABLE_DATA_KINDS
            or packet.destination == BROADCAST_ID
            or packet.source != self.node_id
        ):
            return False
        return any(destination == packet.destination for destination, _sequence in self.awaiting_ack)

    def back_off(self) -> None:
        if self.queue:
            self.queue[0].next_attempt = self.clock() + random.uniform(
                self.config.backoff_min_seconds, self.config.backoff_max_seconds
            )

    def requeue(self, queued: QueuedPacket) -> None:
        self._insert(queued)
        self._update_gauges()

    def transmitted(
        self,
        queued: QueuedPacket,
        *,
        encoded_bytes: int | None = None,
        samples: int = 0,
        airtime_seconds: float = 0.0,
    ) -> None:
        queued.attempts += 1
        packet = queued.packet
        self.statistics.increment("packets_transmitted")
        self.statistics.increment(
            "bytes_transmitted",
            self.encoded_size(packet) if encoded_bytes is None else encoded_bytes,
        )
        self.statistics.increment("samples_transmitted", samples)
        self.statistics.increment("airtime_seconds_transmitted", airtime_seconds)
        if packet.kind == PacketKind.ACK:
            self.statistics.increment("acknowledgements_sent")
        if (
            packet.kind in RELIABLE_DATA_KINDS
            and packet.destination != BROADCAST_ID
            and packet.source == self.node_id
        ):
            queued.next_attempt = self.clock() + self.config.ack_timeout_seconds
            self.awaiting_ack[(packet.destination, packet.sequence)] = queued
            self._update_gauges()

    def acknowledge(self, packet: RadioPacket) -> bool:
        if packet.kind != PacketKind.ACK or packet.destination != self.node_id:
            return False
        queued = self.awaiting_ack.pop((packet.source, packet.sequence), None)
        if queued is None:
            return False
        # An ACK can arrive just after the retry timer put this object back in the
        # queue but before it was retransmitted.
        try:
            self.queue.remove(queued)
        except ValueError:
            pass
        self.statistics.increment("acknowledgements_received")
        self._update_gauges()
        return True

    def ack_for(self, packet: RadioPacket) -> RadioPacket | None:
        if packet.kind not in RELIABLE_DATA_KINDS or packet.destination != self.node_id:
            return None
        return RadioPacket(
            self.node_id,
            packet.source,
            packet.sequence,
            b"",
            PacketKind.ACK,
            self.session_id,
        )

    def is_duplicate(self, packet: RadioPacket) -> bool:
        if packet.kind not in DATA_KINDS:
            return False
        now = self.clock()
        self._expire_seen(now)
        key = (packet.source, packet.sequence)
        duplicate = key in self.seen
        self.seen[key] = now
        self.seen.move_to_end(key)
        if duplicate:
            self.statistics.increment("duplicates_suppressed")
        return duplicate

    def _expire_seen(self, now: float) -> None:
        cutoff = now - self.config.duplicate_window_seconds
        while self.seen and next(iter(self.seen.values())) < cutoff:
            self.seen.popitem(last=False)

    def _expire_queued(self, now: float) -> None:
        maximum_age = self.config.low_priority_max_age_seconds
        if maximum_age <= 0 and not any(q.max_age_seconds for q in self.queue):
            return
        retained = deque()
        dropped = 0
        for queued in self.queue:
            custom_stale = (
                queued.max_age_seconds is not None
                and now - queued.enqueued_at > queued.max_age_seconds
            )
            normal_stale = (
                maximum_age > 0
                and queued.priority >= TrafficPriority.NORMAL
                and queued.attempts == 0
                and now - queued.enqueued_at > maximum_age
            )
            if custom_stale or normal_stale:
                dropped += 1
                for message_type, _enqueued_at in queued.mavlink_frames:
                    self.statistics.increment_mavlink(message_type, "age_dropped")
            else:
                retained.append(queued)
        self.queue = retained
        if dropped:
            self.statistics.increment("stale_packets_dropped", dropped)
            self.statistics.increment("queue_drops", dropped)
            self._update_gauges()

    def _rate_allows(self, queued: QueuedPacket) -> bool:
        size = self.encoded_size(queued.packet)
        if queued.priority == TrafficPriority.NORMAL:
            allowed = self.telemetry_bucket.consume(size)
            if not allowed:
                self.statistics.increment("rate_limit_deferrals")
            return allowed
        if queued.priority == TrafficPriority.BULK:
            allowed = self.bulk_bucket.consume(size)
            if not allowed:
                self.statistics.increment("rate_limit_deferrals")
            return allowed
        return True

    def _update_gauges(self) -> None:
        self.statistics.set_gauge("queue_depth", len(self.queue))
        self.statistics.set_gauge("awaiting_acknowledgements", len(self.awaiting_ack))
