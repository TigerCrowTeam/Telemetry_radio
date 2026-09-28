from __future__ import annotations

import logging
import math
import secrets
import signal
import struct
import threading
import time

import numpy as np

from .authentication import load_private_key, load_public_keys, public_key_fingerprint
from .clock_sync import HardwareClockSynchronizer
from .config import RadioConfig
from .decoder import DecoderWorker
from .framing import (
    BROADCAST_ID,
    AuthenticationError,
    PacketKind,
    ProtocolVersionError,
    RadioPacket,
    decode_mavlink_bundle,
    encode_mavlink_bundle,
)
from .mavlink import (
    MavlinkInterface,
    TrafficPriority,
    inspect_frame,
    is_replaceable_telemetry,
    traffic_priority,
)
from .modulation import BPSKModem, QPSKModem
from .recording import CaptureRecorder
from .reliability import ReliableLink
from .replay_protection import ReplayProtector, ReplayResult
from .routing import Router
from .scheduler import FrequencyScheduler
from .sdr import BufferedRadio, SoapyRadio
from .statistics import LinkStatistics
from .tuning import TimedTuneWorker, TuneCommand
from .status import StatusService

LOG = logging.getLogger(__name__)


def _new_session_id() -> int:
    """Create a time-ordered boot/session identifier with collision entropy."""
    return ((time.time_ns() // 1_000_000) << 16 | secrets.randbits(16)) & 0xFFFFFFFFFFFFFFFF


class TelemetryRadioApp:
    def __init__(
        self,
        config: RadioConfig,
        *,
        sdr=None,
        mavlink=None,
        wall_clock=time.time,
        monotonic_clock=time.monotonic,
    ):
        self.config = config
        self.signing_key = (
            load_private_key(config.authentication.private_key_file)
            if config.authentication.private_key_file is not None
            else None
        )
        self.verification_keys = (
            load_public_keys(config.authentication.public_key_files)
            if self.signing_key is not None
            else None
        )
        if self.signing_key is None:
            LOG.warning("radio packet authentication is disabled")
        else:
            assert self.verification_keys is not None
            for node_id, keys in self.verification_keys.items():
                LOG.info(
                    "trusted node %d identity fingerprints: %s",
                    node_id,
                    ", ".join(public_key_fingerprint(key) for key in keys),
                )
        self.session_id = _new_session_id() or 1
        self.replay_protector = ReplayProtector(
            config.authentication.replay_window,
            config.authentication.replay_state_file,
        )
        self.last_sync_master_time: float | None = None
        self.last_sync_received_at: float | None = None
        self.last_quaternion_at: float | None = None
        self.last_orientation_tx = float("-inf")
        self._last_orientation_stream_request = float("-inf")
        self._last_gcs_request_at: dict[str, float] = {}
        self._last_flight_controller_heartbeat: float | None = None
        self.wall_clock = wall_clock
        self.monotonic_clock = monotonic_clock
        self.scheduler = FrequencyScheduler(config.frequency, clock=wall_clock)
        self.router = Router(config)
        modem_class = QPSKModem if config.sdr.modulation == "qpsk" else BPSKModem
        self.modem = modem_class(
            config.sdr.samples_per_symbol,
            sample_rate=config.sdr.sample_rate,
            interleaver_depth=config.sdr.fec_interleaver_depth,
            bit_repetitions=config.sdr.bit_repetitions,
            rs_parity_bytes=config.sdr.rs_parity_bytes,
            rrc_rolloff=config.sdr.rrc_rolloff,
            rrc_span_symbols=config.sdr.rrc_span_symbols,
        )
        self.rx_modem = self.modem
        if config.sdr.decoder_worker_enabled:
            self.rx_modem = modem_class(
                config.sdr.samples_per_symbol,
                sample_rate=config.sdr.sample_rate,
                interleaver_depth=config.sdr.fec_interleaver_depth,
                bit_repetitions=config.sdr.bit_repetitions,
                rs_parity_bytes=config.sdr.rs_parity_bytes,
                rrc_rolloff=config.sdr.rrc_rolloff,
                rrc_span_symbols=config.sdr.rrc_span_symbols,
            )
        self.sdr = sdr
        if self.sdr is None:
            self.sdr = BufferedRadio(
                SoapyRadio(config.sdr, config.frequency.frequencies_hz)
            )
        self.hardware_clock_sync = HardwareClockSynchronizer()
        if config.link.hardware_clock_sync:
            self._initialize_hardware_epoch()
        self.mavlink = (
            mavlink
            if mavlink is not None
            else MavlinkInterface(
                config.mavlink.connection, config.mavlink.baud, config.mavlink.source_system
            )
        )
        self.statistics = LinkStatistics(monotonic_clock)
        self.statistics.set_gauge("queue_capacity", config.link.queue_size)
        self.link = ReliableLink(
            config.node_id,
            config.link,
            clock=monotonic_clock,
            statistics=self.statistics,
            session_id=self.session_id,
            encoded_size=self._on_air_size,
        )
        self.sequence = 0
        self.running = threading.Event()
        self.running.set()
        self.last_rx = self.monotonic_clock()
        self.current_frequency: float | None = None
        self.channel_power = 0.0
        self._receive_diagnostics: dict[str, int] = {}
        self.recovering = False
        self.scan_frequencies: list[float] = []
        self.scan_index = 0
        self.next_scan_at = 0.0
        self.next_sync_at = 0.0
        # Monotonic end time of the most recent RF burst.  TDD admission uses
        # this to enforce a quiet guard before another burst or retry.
        self._last_tx_end = 0.0
        self._tdd_slot_index: int | None = None
        self._tdd_bursts_sent = 0
        self._last_tdd_opportunity_id: int | None = None
        self._last_orientation_tdd_cycle: int | None = None
        self.status_service: StatusService | None = None
        self.recorder: CaptureRecorder | None = None
        self.decoder_worker: DecoderWorker | None = None
        self.tune_worker: TimedTuneWorker | None = None
        self._scheduled_tune_slot: int | None = None
        if config.recording.directory is not None:
            try:
                self.recorder = CaptureRecorder(
                    config.recording.directory,
                    sample_rate=config.sdr.sample_rate,
                    samples_per_symbol=config.sdr.samples_per_symbol,
                    rrc_rolloff=config.sdr.rrc_rolloff,
                    rrc_span_symbols=config.sdr.rrc_span_symbols,
                    modulation=config.sdr.modulation,
                    interleaver_depth=config.sdr.fec_interleaver_depth,
                    bit_repetitions=config.sdr.bit_repetitions,
                    rs_parity_bytes=config.sdr.rs_parity_bytes,
                    record_iq=config.recording.iq,
                    record_frames=config.recording.frames,
                    authenticated=self.signing_key is not None,
                    max_file_bytes=config.recording.max_file_bytes,
                    max_sessions=config.recording.max_sessions,
                    min_free_bytes=config.recording.min_free_bytes,
                    create_session=True,
                    on_disable=lambda _reason: self.statistics.increment("recording_failures"),
                )
                LOG.info("recording capture session in %s", self.recorder.directory)
            except (OSError, ValueError) as exc:
                LOG.error("capture recording unavailable; continuing without it: %s", exc)
                self.statistics.increment("recording_failures")
        if config.sdr.decoder_worker_enabled:
            self.decoder_worker = DecoderWorker(
                self.sdr,
                self.rx_modem,
                sample_rate=config.sdr.sample_rate,
                output_batches=config.sdr.decoder_output_batches,
                record_iq=(self.recorder.record_iq if self.recorder is not None else None),
            )
        if config.frequency.timed_tuning:
            self.tune_worker = TimedTuneWorker(self.sdr)

    def _queue(
        self,
        payload: bytes,
        destination: int,
        *,
        kind: PacketKind = PacketKind.DATA,
        priority: TrafficPriority | None = None,
        flow_key: tuple[int | None, ...] | None = None,
        reliable: bool = True,
        message_type: str | None = None,
        max_age_seconds: float | None = None,
    ) -> bool:
        if kind == PacketKind.DATA and not reliable:
            kind = PacketKind.UNRELIABLE_DATA
        packet = RadioPacket(
            self.config.node_id,
            destination,
            self.sequence,
            payload,
            kind,
            self.session_id,
        )
        if kind != PacketKind.ACK:
            self.sequence = (self.sequence + 1) & 0xFFFFFFFF
            if self.sequence == 0:
                self.session_id = _new_session_id() or 1
                self.link.session_id = self.session_id
        return self.link.enqueue(
            packet,
            priority=priority,
            flow_key=flow_key,
            message_type=message_type,
            max_age_seconds=max_age_seconds,
        )

    def _transmit_one(self) -> None:
        state = self.scheduler.state()
        scheduled = self._next_hardware_tdd_opportunity()
        scheduled_channel_allowed = True
        if scheduled is not None:
            # A look-ahead opportunity can fall just beyond a frequency boundary
            # even though the application is still on the old channel. Never queue
            # that burst into the SDR; wait for the retune and boundary guard.
            target_master_seconds = scheduled[1] / 1e9
            target_state = self.scheduler.state(
                target_master_seconds - self.scheduler.clock_offset
            )
            scheduled_channel_allowed = (
                target_state.transmit_allowed
                and target_state.frequency_hz == self.current_frequency
            )
        # In scheduled TDD mode the slot assignment is the collision-avoidance
        # mechanism.  RX power from the preceding peer burst can remain in the
        # last sample block and would otherwise defer the entire return slot.
        clear = (
            True
            if self.config.link.tdd_period_seconds > 0
            else self.channel_power < self.config.link.channel_busy_threshold
        )
        if not clear and self.link.queue:
            self.statistics.increment("channel_busy_deferrals")
            self.link.back_off()
        scheduled_cycle = (
            scheduled[3] // self.config.link.tdd_master_bursts_per_slot
            if scheduled is not None and self.config.role == "master"
            else None
        )
        reserve_orientation = (
            scheduled_cycle is not None
            and scheduled_cycle != self._last_orientation_tdd_cycle
        )
        control_interval = self.config.link.orientation_control_cycle_interval
        control_cycle = (
            scheduled_cycle is not None
            and control_interval > 0
            and scheduled_cycle % control_interval == control_interval - 1
        )
        if control_cycle:
            reserve_orientation = False
        reserve_heartbeat = (
            scheduled is not None
            and self.config.role == "master"
            and scheduled[2] >= 1
        )
        preferred_message_types = None
        if reserve_heartbeat:
            preferred_message_types = frozenset({"HEARTBEAT"})
        elif reserve_orientation:
            preferred_message_types = frozenset({"ATTITUDE", "ATTITUDE_QUATERNION"})
        tdd_allowed = (
            scheduled is not None
            if self.config.link.hardware_slot_scheduling
            else self._tdd_transmit_allowed()
        )
        queued = self.link.next_packet(
            clear,
            state.transmit_allowed
            and scheduled_channel_allowed
            and tdd_allowed
            and not self.recovering,
            preferred_message_types,
            (
                frozenset({"ATTITUDE", "ATTITUDE_QUATERNION"})
                if control_cycle
                else None
            ),
        )
        if queued is None:
            return
        try:
            target_ns: int | None = scheduled[0] if scheduled is not None else None
            target_master_ns: int | None = scheduled[1] if scheduled is not None else None
            now = self.monotonic_clock()
            orientation = any(
                message_type in {"ATTITUDE", "ATTITUDE_QUATERNION"}
                for message_type, _enqueued_at in queued.mavlink_frames
            )
            if (
                orientation
                and now < self.last_orientation_tx + self.config.link.orientation_pacing_seconds
            ):
                queued.next_attempt = (
                    self.last_orientation_tx + self.config.link.orientation_pacing_seconds
                )
                self.link.requeue(queued)
                return
            original_packet = queued.packet
            selected = self._bundle_for_transmit(queued)
            if queued.packet.kind == PacketKind.SYNC:
                if self.config.link.hardware_timed_io and target_ns is None:
                    # A sync beacon must embed its target before modulation, so
                    # give only this infrequent control packet a longer budget.
                    target_ns = self.sdr.hardware_time() + int(
                        self.config.link.hardware_sync_tx_lead_seconds * 1e9
                    )
                # Timestamp at airtime, not enqueue time; guard/backoff delays must
                # not look like clock error to recovering nodes.
                sync_time = (
                    target_ns / 1e9
                    if self.config.link.hardware_clock_sync and target_ns is not None
                    else self.wall_clock()
                    + (
                        self.config.link.hardware_timed_tx_lead_seconds
                        if self.config.link.hardware_timed_io
                        else 0.0
                    )
                )
                payload = struct.pack("!d", sync_time)
                if self.config.link.hardware_clock_sync and target_ns is not None:
                    payload += struct.pack("!Q", target_ns)
                queued.packet = RadioPacket(
                    queued.packet.source,
                    queued.packet.destination,
                    queued.packet.sequence,
                    payload,
                    PacketKind.SYNC,
                    queued.packet.session_id,
                )
            wire = queued.packet.encode(self.signing_key)
            samples = self.modem.modulate(wire)
            timed_lead = (
                0.0
                if scheduled is not None
                else (
                    self.config.link.hardware_sync_tx_lead_seconds
                    if target_ns is not None
                    else self.config.link.hardware_timed_tx_lead_seconds
                )
            )
            if not self._tdd_burst_fits(len(samples), timed_lead, target_master_ns):
                # Do not let a burst cross the slot boundary and collide with
                # the peer's receive window. Restore bundle members so newer
                # samples can coalesce them before the next slot.
                if selected:
                    self._restore_deferred_bundle(queued, original_packet, selected)
                self.link.requeue(queued)
                return
            if self.config.link.hardware_timed_io:
                if target_ns is None:
                    # Choose the normal data target after modulation and after
                    # acquiring the RX/TX I/O lock. This prevents CPU work and a
                    # pending readStream timeout from consuming the short lead.
                    target_ns = self.sdr.transmit_with_lead(
                        samples,
                        int(self.config.link.hardware_timed_tx_lead_seconds * 1e9),
                    )
                else:
                    lead_ns = target_ns - self.sdr.hardware_time()
                    self.statistics.set_gauge("scheduled_tx_lead_ms", lead_ns / 1e6)
                    self.statistics.set_gauge("scheduled_tx_target_ns", target_ns)
                    if lead_ns < int(
                        self.config.link.hardware_timed_tx_lead_seconds * 1e9
                    ):
                        counter = (
                            "orientation_deadline_misses"
                            if orientation
                            else "tdd_opportunities_missed"
                        )
                        self.statistics.increment(counter)
                        queued.next_attempt = self.monotonic_clock()
                        self.link.requeue(queued)
                        return
                    self.sdr.transmit_at(samples, target_ns)
                    self.statistics.increment("scheduled_tdd_transmissions")
            else:
                self.sdr.transmit(samples)
            self._last_tx_end = self.monotonic_clock()
            if self.config.link.tdd_period_seconds > 0:
                period = self.config.link.tdd_period_seconds
                slot = int(
                    self._tdd_time() / period
                )
                if slot == self._tdd_slot_index:
                    self._tdd_bursts_sent += 1
            if scheduled is not None:
                opportunity_id = scheduled[3]
                if (
                    self._last_tdd_opportunity_id is not None
                    and opportunity_id > self._last_tdd_opportunity_id + 1
                    and self.link.queue
                ):
                    self.statistics.increment(
                        "tdd_opportunities_missed",
                        opportunity_id - self._last_tdd_opportunity_id - 1,
                    )
                self._last_tdd_opportunity_id = opportunity_id
                if reserve_orientation and orientation:
                    self.statistics.increment("orientation_opportunities_used")
                    self._last_orientation_tdd_cycle = scheduled_cycle
            self.link.transmitted(
                queued,
                encoded_bytes=self.modem.frame_size(len(wire)),
                samples=len(samples),
                airtime_seconds=len(samples) / self.config.sdr.sample_rate,
            )
            for message_type, enqueued_at in queued.mavlink_frames:
                self.statistics.increment_mavlink(message_type, "transmitted")
                self.statistics.observe_mavlink_age(message_type, now - enqueued_at)
            if orientation:
                self.last_orientation_tx = now
        except (OSError, ValueError) as exc:
            LOG.error("radio transmission failed: %s", exc)
            self.statistics.increment("transmit_errors")
            queued.next_attempt = self.monotonic_clock() + self.config.link.backoff_max_seconds
            self.link.requeue(queued)

    def _bundle_for_transmit(self, queued) -> list:
        """Combine compatible telemetry frames under one RF/FEC envelope."""
        maximum_frames = self.config.link.mavlink_bundle_max_frames
        data_kinds = (PacketKind.DATA, PacketKind.UNRELIABLE_DATA)
        if (
            maximum_frames < 2
            or queued.packet.kind not in data_kinds
            or queued.priority < TrafficPriority.NORMAL
            and queued.packet.kind == PacketKind.DATA
            or queued.attempts
        ):
            return []
        frames = [queued.packet.payload]
        total = 4 + 2 + len(frames[0])
        selected = []
        for candidate in list(self.link.queue):
            if len(frames) >= maximum_frames:
                break
            if (
                candidate.packet.kind != queued.packet.kind
                or candidate.packet.destination != queued.packet.destination
                or candidate.priority < queued.priority
                or candidate.attempts
            ):
                continue
            added = 2 + len(candidate.packet.payload)
            if total + added > self.config.link.mavlink_bundle_max_bytes:
                continue
            frames.append(candidate.packet.payload)
            selected.append(candidate)
            total += added
        if len(frames) < 2:
            return []
        for candidate in selected:
            self.link.queue.remove(candidate)
        packet = queued.packet
        queued.packet = RadioPacket(
            packet.source,
            packet.destination,
            packet.sequence,
            encode_mavlink_bundle(frames),
            (
                PacketKind.MAVLINK_BUNDLE
                if packet.kind == PacketKind.DATA
                else PacketKind.UNRELIABLE_MAVLINK_BUNDLE
            ),
            packet.session_id,
        )
        queued.mavlink_frames = tuple(
            metadata
            for member in (queued, *selected)
            for metadata in member.mavlink_frames
        )
        self.statistics.increment("mavlink_frames_bundled", len(frames))
        for candidate in selected:
            for message_type, _enqueued_at in candidate.mavlink_frames:
                self.statistics.increment_mavlink(message_type, "bundled")
        return selected

    def _restore_deferred_bundle(self, queued, original_packet, selected: list) -> None:
        queued.packet = original_packet
        queued.mavlink_frames = queued.mavlink_frames[:1]
        for candidate in selected:
            self.link._insert(candidate)

    def _tdd_transmit_allowed(self) -> bool:
        """Give the master and GCS separate half-duplex transmit windows."""
        period = self.config.link.tdd_period_seconds
        if period <= 0:
            return True
        guard = self.config.link.tdd_guard_seconds
        inter_burst = self.config.link.tdd_inter_burst_seconds
        if inter_burst is None:
            inter_burst = guard
        if self.monotonic_clock() < self._last_tx_end + inter_burst:
            return False
        slot = int(self._tdd_time() / period)
        if slot != self._tdd_slot_index:
            self._tdd_slot_index = slot
            self._tdd_bursts_sent = 0
        # The Cube/master can generate a much larger housekeeping backlog than
        # the GCS.  Reserve one complete period for the return direction by
        # allowing only one master burst; the GCS may use two shorter bursts.
        # This prevents Pi-side retries/bulk traffic from monopolizing the
        # half-duplex channel and starving telemetry and ACKs from the peer.
        burst_limit = (
            self.config.link.tdd_master_bursts_per_slot
            if self.config.role == "master"
            else self.config.link.tdd_gcs_bursts_per_slot
        )
        if self._tdd_bursts_sent >= burst_limit:
            return False
        phase = self._tdd_time() % period
        midpoint = period / 2.0
        if self.config.link.tdd_staggered_windows:
            if self.config.role == "master":
                return 0 <= phase < midpoint - guard
            return midpoint <= phase < period - guard
        if self.config.role == "master":
            return guard <= phase < midpoint - guard
        return midpoint + guard <= phase < period - guard

    def _tdd_burst_fits(
        self,
        sample_count: int,
        timed_lead_seconds: float | None = None,
        target_master_ns: int | None = None,
    ) -> bool:
        """Return whether a complete burst fits before the current TDD boundary."""
        period = self.config.link.tdd_period_seconds
        if period <= 0:
            return True
        guard = self.config.link.tdd_guard_seconds
        phase = (
            target_master_ns / 1e9 if target_master_ns is not None else self._tdd_time()
        ) % period
        midpoint = period / 2.0
        if self.config.link.tdd_staggered_windows and self.config.role == "master":
            slot_end = midpoint - guard
        elif self.config.link.tdd_staggered_windows:
            slot_end = period - guard
        elif self.config.role == "master":
            slot_end = midpoint - guard
        else:
            slot_end = period - guard
        # The SDR keeps a short tail in its host/FPGA FIFO after writeStream
        # returns.  Include that drain margin in slot admission; otherwise a
        # packet that technically fits can still occupy the peer's guard time.
        airtime = sample_count / self.config.sdr.sample_rate + 0.001
        if self.config.link.hardware_timed_io and target_master_ns is None:
            airtime += (
                self.config.link.hardware_timed_tx_lead_seconds
                if timed_lead_seconds is None
                else timed_lead_seconds
            )
        return phase + airtime <= slot_end

    def _next_hardware_tdd_opportunity(self) -> tuple[int, int, int, int] | None:
        """Return the next exact local/master TX target within the lookahead.

        Opportunity centers are fixed in master hardware time. This removes the
        dependency on the Python loop happening to enter a short TDD window.
        """
        link = self.config.link
        if (
            not link.hardware_slot_scheduling
            or not link.hardware_timed_io
            or link.tdd_period_seconds <= 0
            or (self.config.role != "master" and not self.hardware_clock_sync.ready)
        ):
            return None
        local_now_ns = self.sdr.hardware_time()
        master_now_ns = (
            local_now_ns
            if self.config.role == "master"
            else self.hardware_clock_sync.local_to_master(local_now_ns)
        )
        period_ns = int(round(link.tdd_period_seconds * 1e9))
        midpoint_ns = period_ns // 2
        guard_ns = int(round(link.tdd_guard_seconds * 1e9))
        if self.config.role == "master":
            start_ns, end_ns = 0, midpoint_ns - guard_ns
            count = link.tdd_master_bursts_per_slot
        else:
            start_ns, end_ns = midpoint_ns, period_ns - guard_ns
            count = link.tdd_gcs_bursts_per_slot
        # Leave host/USB submission margin beyond the device's required lead.
        # Selecting a target exactly at the minimum makes any Python scheduling
        # delay turn an otherwise valid opportunity into a timed-late write.
        submission_margin_ns = int(link.hardware_slot_submission_margin_seconds * 1e9)
        minimum_ns = master_now_ns + int(link.hardware_timed_tx_lead_seconds * 1e9)
        minimum_ns += submission_margin_ns
        cycle = minimum_ns // period_ns
        candidates: list[tuple[int, int, int]] = []
        for candidate_cycle in (cycle, cycle + 1):
            for ordinal in range(count):
                offset_ns = start_ns + (2 * ordinal + 1) * (end_ns - start_ns) // (2 * count)
                target_ns = candidate_cycle * period_ns + offset_ns
                if target_ns >= minimum_ns:
                    candidates.append((target_ns, ordinal, candidate_cycle * count + ordinal))
        if not candidates:
            return None
        master_target_ns, ordinal, opportunity_id = min(candidates)
        if master_target_ns - master_now_ns > int(link.hardware_slot_lookahead_seconds * 1e9):
            return None
        local_target_ns = (
            master_target_ns
            if self.config.role == "master"
            else self.hardware_clock_sync.master_to_local(master_target_ns)
        )
        return local_target_ns, master_target_ns, ordinal, opportunity_id

    def _tdd_time(self) -> float:
        compensation = (
            self.config.link.tdd_receive_delay_compensation_seconds
            if self.config.role != "master"
            else 0.0
        )
        if self.config.link.hardware_clock_sync and self.hardware_clock_sync.ready:
            local_ns = self.sdr.hardware_time()
            return self.hardware_clock_sync.local_to_master(local_ns) / 1e9 + compensation
        if self.config.link.hardware_clock_sync and self.config.role == "master":
            return self.sdr.hardware_time() / 1e9 + compensation
        return self.wall_clock() + self.scheduler.clock_offset + compensation

    def _initialize_hardware_epoch(self) -> None:
        """Set device time to a shared future UTC whole-second epoch."""
        target_ns = int(
            math.ceil(self.wall_clock() + self.config.link.hardware_epoch_lead_seconds) * 1e9
        )
        while True:
            remaining = target_ns / 1e9 - self.wall_clock()
            if remaining <= 0:
                break
            time.sleep(min(remaining, 0.05))
        self.sdr.set_hardware_time(target_ns)
        measured = self.sdr.hardware_time()
        LOG.info(
            "hardware clock epoch set to %d ns (readback error %+d ns)",
            target_ns,
            measured - target_ns,
        )

    def _retune(self) -> None:
        if self.recovering:
            return
        state = self.scheduler.state()
        wanted = state.frequency_hz
        if wanted != self.current_frequency:
            previous = self.current_frequency
            prepared = (
                self.tune_worker is not None and self.tune_worker.completed(state.slot)
            )
            if not prepared:
                error = self.tune_worker.error(state.slot) if self.tune_worker is not None else None
                if error is not None:
                    LOG.warning("timed tune preparation failed for slot %d: %s", state.slot, error)
                self.sdr.tune(wanted)
            flush = getattr(self.sdr, "flush_receive", None)
            if flush is not None:
                flush()
            self.current_frequency = wanted
            self.statistics.set_gauge("active_frequency_hz", wanted)
            if previous is not None:
                self.statistics.increment("frequency_hops")
                self._last_tdd_opportunity_id = None
                LOG.info("frequency hop %.3f MHz -> %.3f MHz", previous / 1e6, wanted / 1e6)

    def _prepare_next_tune(self) -> None:
        if self.tune_worker is None or self.recovering:
            return
        state = self.scheduler.state()
        next_slot = state.slot + 1
        if self._scheduled_tune_slot == next_slot:
            return
        if state.seconds_to_change > self.config.frequency.tune_lead_seconds:
            return
        target_master_seconds = (
            self.wall_clock() + self.scheduler.clock_offset + state.seconds_to_change
        )
        next_state = self.scheduler.state(
            target_master_seconds + 1e-6 - self.scheduler.clock_offset
        )
        master_target_ns = int(target_master_seconds * 1e9)
        local_target_ns = (
            self.hardware_clock_sync.master_to_local(master_target_ns)
            if self.config.role != "master" and self.hardware_clock_sync.ready
            else master_target_ns
        )
        if self.tune_worker.submit(
            TuneCommand(next_slot, next_state.frequency_hz, local_target_ns)
        ):
            self._scheduled_tune_slot = next_slot

    def _on_air_size(self, packet: RadioPacket) -> int:
        framed = packet.encoded_size(authenticated=self.signing_key is not None)
        return self.modem.frame_size(framed)

    def _recover_if_needed(self) -> None:
        if self.config.role == "master":
            return
        now = self.monotonic_clock()
        if not self.recovering:
            if now - self.last_rx < self.config.frequency.sync_loss_seconds:
                return
            LOG.warning("radio synchronization lost; beginning channel scan")
            self.statistics.increment("synchronization_losses")
            self.statistics.set_gauge("synchronized", False)
            self.recovering = True
            self.scan_frequencies = self.scheduler.scan_order()
            self.scan_index = 0
            self.next_scan_at = 0.0
        if now >= self.next_scan_at:
            frequency = self.scan_frequencies[self.scan_index]
            self.scan_index = (self.scan_index + 1) % len(self.scan_frequencies)
            self.sdr.tune(frequency)
            self.current_frequency = frequency
            self.statistics.set_gauge("active_frequency_hz", frequency)
            self.next_scan_at = now + self.config.frequency.reacquire_dwell_seconds

    def _receive_radio(self) -> bool:
        received = False
        diagnostics = getattr(self.sdr, "receive_diagnostics", None)
        if diagnostics is not None:
            for name, value in diagnostics().items():
                previous = self._receive_diagnostics.get(name, 0)
                if value > previous:
                    self.statistics.increment(name, value - previous)
                self._receive_diagnostics[name] = value
        if self.decoder_worker is not None:
            worker_diagnostics = self.decoder_worker.diagnostics()
            self.channel_power = float(worker_diagnostics.pop("decoder_channel_power"))
            for name, value in worker_diagnostics.items():
                if name in {
                    "decoder_queue_depth",
                    "decoder_realtime_utilization",
                    "decoder_processing_p95_ms",
                    "decoder_processing_max_ms",
                }:
                    self.statistics.set_gauge(name, value)
                    continue
                previous = self._receive_diagnostics.get(name, 0)
                if value > previous:
                    self.statistics.increment(name, value - previous)
                self._receive_diagnostics[name] = int(value)
            batches = []
            first = self.decoder_worker.receive(timeout=0.002)
            if first is not None:
                batches.append(first)
                for _ in range(63):
                    batch = self.decoder_worker.receive()
                    if batch is None:
                        break
                    batches.append(batch)
            decoded = [frame for batch in batches for frame in batch.frames]
            corrected = sum(batch.fec_corrected_bits for batch in batches)
            uncorrectable = sum(batch.fec_uncorrectable_frames for batch in batches)
            failure_score = next(
                (
                    batch.failure_score
                    for batch in reversed(batches)
                    if batch.failure_score is not None
                ),
                None,
            )
            failure_corrected_score = next(
                (
                    batch.failure_corrected_score
                    for batch in reversed(batches)
                    if batch.failure_corrected_score is not None
                ),
                None,
            )
        else:
            timed_receive = getattr(self.sdr, "receive_timed", None)
            if timed_receive is None:
                samples = self.sdr.receive()
                sample_time_ns = None
            else:
                block = timed_receive()
                samples = block.samples
                sample_time_ns = block.time_ns
            if self.recorder is not None:
                self.recorder.record_iq(samples)
            self.channel_power = (
                float(np.mean(np.abs(samples) ** 2)) if len(samples) else 0.0
            )
            corrected_before = self.rx_modem.fec_corrected_bits
            uncorrectable_before = self.rx_modem.fec_uncorrectable_frames
            feed_timed = getattr(self.rx_modem, "feed_timed", None)
            if feed_timed is None:
                decoded = [(wire, None) for wire in self.rx_modem.feed(samples)]
            else:
                decoded = feed_timed(samples, sample_time_ns)
            corrected = self.rx_modem.fec_corrected_bits - corrected_before
            uncorrectable = self.rx_modem.fec_uncorrectable_frames - uncorrectable_before
            failure_score = self.rx_modem.last_fec_failure_score
            failure_corrected_score = self.rx_modem.last_fec_failure_corrected_score
        channel_power = getattr(self.sdr, "channel_power", None)
        if channel_power is not None:
            self.channel_power = max(self.channel_power, float(channel_power()))
        self.statistics.set_gauge("estimated_signal_power", self.channel_power)
        if corrected:
            self.statistics.increment("fec_corrected_bits", corrected)
        if uncorrectable:
            self.statistics.increment("fec_uncorrectable_frames", uncorrectable)
            self.statistics.increment("invalid_packets", uncorrectable)
            self.statistics.set_gauge(
                "last_fec_failure_score", failure_score
            )
            self.statistics.set_gauge(
                "last_fec_failure_corrected_score",
                failure_corrected_score,
            )
        for wire, arrival_time_ns in decoded:
            try:
                packet = RadioPacket.decode(wire, self.verification_keys)
            except ValueError as exc:
                if self.recorder is not None:
                    self.recorder.record_frame(wire, str(exc))
                LOG.warning("discarded invalid radio packet: %s", exc)
                self.statistics.increment("invalid_packets")
                if isinstance(exc, AuthenticationError):
                    self.statistics.increment("authentication_failures")
                elif isinstance(exc, ProtocolVersionError):
                    self.statistics.increment("protocol_version_mismatches")
                elif "CRC" in str(exc):
                    self.statistics.increment("crc_failures")
                continue
            if self.recorder is not None:
                self.recorder.record_frame(wire)
            received = True
            self.statistics.increment("packets_received")
            self.statistics.increment("bytes_received", self.modem.frame_size(len(wire)))
            if packet.source == self.config.node_id:
                continue
            if self.verification_keys is not None:
                replay_result = self.replay_protector.observe(packet)
                if replay_result is not ReplayResult.NEW:
                    if replay_result is ReplayResult.REJECTED:
                        self.statistics.increment("replay_rejections")
                    else:
                        self.statistics.increment("duplicates_suppressed")
                    acknowledgement = self.link.ack_for(packet)
                    if acknowledgement is not None:
                        self.link.enqueue(acknowledgement, priority=TrafficPriority.LINK_CONTROL)
                    continue
            if self.link.acknowledge(packet):
                self.last_rx = self.monotonic_clock()
                LOG.debug("ACK from node %d for sequence %d", packet.source, packet.sequence)
                continue
            if packet.kind == PacketKind.SYNC:
                if packet.source == self.config.master_id and len(packet.payload) in (8, 16):
                    master_time = struct.unpack("!d", packet.payload[:8])[0]
                    if (
                        len(packet.payload) == 16
                        and self.config.link.hardware_clock_sync
                        and arrival_time_ns is not None
                    ):
                        master_tx_ns = struct.unpack("!Q", packet.payload[8:])[0]
                        estimate = self.hardware_clock_sync.observe(
                            master_tx_ns, arrival_time_ns
                        )
                        self.statistics.set_gauge(
                            "hardware_clock_offset_ns", estimate.offset_ns
                        )
                        self.statistics.set_gauge(
                            "hardware_clock_drift_ppm", estimate.drift_ppm
                        )
                        self.statistics.set_gauge(
                            "hardware_clock_observations", estimate.samples
                        )
                    received_at = self.monotonic_clock()
                    # Keep the synchronization estimate in the same clock
                    # domain as the scheduler.  ``received_at`` above is
                    # monotonic and is only used for freshness/liveness;
                    # using it for the wall-clock offset would mix unrelated
                    # epochs and shift TDD slot boundaries.
                    received_wall = self.wall_clock()
                    if not math.isfinite(master_time) or not self._sync_is_fresh(
                        master_time, received_at
                    ):
                        LOG.warning("discarded stale authenticated synchronization beacon")
                        self.statistics.increment("sync_freshness_rejections")
                        continue
                    self.last_rx = received_at
                    offset = self.scheduler.synchronize(master_time, received_at=received_wall)
                    self.last_sync_master_time = master_time
                    self.last_sync_received_at = received_at
                    LOG.info("synchronized to master clock (offset %.3f seconds)", offset)
                    if self.recovering:
                        self.statistics.increment("reacquisitions")
                    self.recovering = False
                    self.statistics.set_gauge("synchronized", True)
                    # Synchronization can change the desired hopping slot, but
                    # avoid disrupting RX with a redundant retune on a fixed
                    # channel (or when the synchronized slot is unchanged).
                    if self.scheduler.state().frequency_hz != self.current_frequency:
                        self.current_frequency = None
                continue
            self.last_rx = self.monotonic_clock()
            acknowledgement = self.link.ack_for(packet)
            if acknowledgement is not None:
                if self.link.enqueue(acknowledgement, priority=TrafficPriority.LINK_CONTROL):
                    LOG.debug(
                        "queued ACK for node %d sequence %d",
                        acknowledgement.destination,
                        acknowledgement.sequence,
                    )
            if self.link.is_duplicate(packet):
                LOG.debug(
                    "suppressed duplicate from node %d sequence %d", packet.source, packet.sequence
                )
                continue
            try:
                payloads = (
                    decode_mavlink_bundle(packet.payload)
                    if packet.kind
                    in (PacketKind.MAVLINK_BUNDLE, PacketKind.UNRELIABLE_MAVLINK_BUNDLE)
                    else [packet.payload]
                )
            except ValueError as exc:
                LOG.warning("discarded invalid MAVLink bundle: %s", exc)
                self.statistics.increment("invalid_packets")
                continue
            for payload in payloads:
                data_packet = RadioPacket(
                    packet.source,
                    packet.destination,
                    packet.sequence,
                    payload,
                    PacketKind.DATA,
                    packet.session_id,
                )
                address = inspect_frame(payload)
                if self.router.deliver_locally(data_packet, address):
                    self.mavlink.send(payload)
                    self.statistics.increment("mavlink_frames_to_local")
                    self.statistics.increment_mavlink(address.message_type, "delivered")
                    if address.message_type == "HEARTBEAT":
                        component_name = (
                            f"HEARTBEAT[{address.source_system}:{address.source_component}]"
                        )
                        self.statistics.increment_mavlink(component_name, "delivered")
                        if (
                            address.source_system == self.config.mavlink.target_system
                            and address.source_component == self.config.mavlink.target_component
                        ):
                            self._last_flight_controller_heartbeat = self.monotonic_clock()
                relay_destination = self.router.relay(data_packet, address)
                if relay_destination is not None:
                    self._queue(
                        payload,
                        relay_destination,
                        priority=traffic_priority(address),
                        flow_key=(
                            address.source_system,
                            address.source_component,
                            address.message_id,
                            relay_destination,
                        ),
                        reliable=not is_replaceable_telemetry(address),
                        message_type=address.message_type,
                        max_age_seconds=(
                            self.config.link.replaceable_max_age_seconds
                            if is_replaceable_telemetry(address)
                            else None
                        ),
                    )
        return received

    def _sync_is_fresh(self, master_time: float, received_at: float) -> bool:
        if self.last_sync_master_time is None or self.last_sync_received_at is None:
            return True
        expected = self.last_sync_master_time + (received_at - self.last_sync_received_at)
        delta = master_time - expected
        return (
            -self.config.authentication.sync_max_age_seconds
            <= delta
            <= self.config.authentication.sync_max_future_seconds
        )

    def _queue_clock_sync(self) -> None:
        now = self.monotonic_clock()
        if self.config.role == "master" and now >= self.next_sync_at:
            self._queue(struct.pack("!d", self.wall_clock()), BROADCAST_ID, kind=PacketKind.SYNC)
            self.next_sync_at = now + 2.0

    def _orientation_allowed(self, address, now: float) -> bool:
        if address.message_type == "ATTITUDE_QUATERNION":
            self.last_quaternion_at = now
            return True
        if address.message_type != "ATTITUDE" or self.last_quaternion_at is None:
            return True
        return now - self.last_quaternion_at > self.config.link.quaternion_fallback_seconds

    def _queue_local_mavlink(self, frame: bytes) -> None:
        self.statistics.increment("mavlink_frames_from_local")
        address = inspect_frame(frame)
        self.statistics.increment_mavlink(address.message_type, "received")
        if address.message_type == "HEARTBEAT":
            component_name = f"HEARTBEAT[{address.source_system}:{address.source_component}]"
            self.statistics.increment_mavlink(component_name, "received")
        now = self.monotonic_clock()
        shaped_messages = {
            "PARAM_REQUEST_LIST",
            "PARAM_REQUEST_READ",
            "REQUEST_DATA_STREAM",
        }
        minimum_interval = self.config.link.gcs_request_min_interval_seconds
        if (
            self.config.role == "gcs"
            and minimum_interval > 0
            and address.message_type in shaped_messages
        ):
            previous = self._last_gcs_request_at.get(address.message_type, float("-inf"))
            if now < previous + minimum_interval:
                self.statistics.increment("gcs_requests_shaped")
                return
            self._last_gcs_request_at[address.message_type] = now
        if not self._orientation_allowed(address, now):
            self.statistics.increment("stale_packets_dropped")
            self.statistics.increment_mavlink(address.message_type, "source_suppressed")
            return
        destination = self.router.destination_for_local_mavlink(address)
        priority = traffic_priority(address)
        # Apply backpressure at the Cube boundary. Once a small live-data queue
        # is occupied, housekeeping would only create stale traffic.
        if priority >= TrafficPriority.BULK and len(self.link.queue) >= 4:
            self.statistics.increment("stale_packets_dropped")
            self.statistics.increment_mavlink(address.message_type, "age_dropped")
            return
        replaceable = is_replaceable_telemetry(address)
        repetitions = (
            self.config.link.flight_controller_heartbeat_repetitions
            if address.message_type == "HEARTBEAT"
            and address.source_system == self.config.mavlink.target_system
            and address.source_component == self.config.mavlink.target_component
            else 1
        )
        base_flow_key = (
            address.source_system,
            address.source_component,
            address.message_id,
            destination,
        )
        for repetition in range(repetitions):
            self._queue(
                frame,
                destination,
                priority=priority,
                flow_key=(
                    base_flow_key
                    if repetition == 0
                    else (*base_flow_key, repetition)
                ),
                reliable=not replaceable,
                message_type=address.message_type,
                max_age_seconds=(
                    self.config.link.replaceable_max_age_seconds if replaceable else None
                ),
            )

    def step(self) -> None:
        """Run one non-blocking application cycle (also used by simulation tests)."""
        self._prepare_next_tune()
        self._retune()
        self._request_orientation_stream()
        self._receive_radio()
        # The SDR produces one 4096-sample block every 2.048 ms at 2 Msps.
        # Waiting 10 ms for MAVLink on every cycle guarantees RX overflows and
        # drops enough IQ to make burst decoding intermittent.  MAVLink is
        # polled here; the radio receive call already provides loop pacing.
        for _ in range(self.config.link.mavlink_drain_max_frames):
            frame = self.mavlink.receive(timeout=0.0)
            if not frame:
                break
            self._queue_local_mavlink(frame)
        self._queue_clock_sync()
        self._transmit_one()
        self._recover_if_needed()
        state = self.scheduler.state()
        self.statistics.set_gauge("frequency_slot", state.slot)
        self.statistics.set_gauge("transmit_allowed", state.transmit_allowed)
        if self._last_flight_controller_heartbeat is None:
            heartbeat_age = None
        else:
            heartbeat_age = max(
                0.0, self.monotonic_clock() - self._last_flight_controller_heartbeat
            )
        self.statistics.set_gauge(
            "flight_controller_heartbeat_age_ms",
            None if heartbeat_age is None else heartbeat_age * 1000.0,
        )
        self.statistics.set_gauge(
            "flight_controller_heartbeat_stale",
            heartbeat_age is None or heartbeat_age > 1.5,
        )
        if self.status_service is not None:
            self.status_service.maybe_report()

    def _request_orientation_stream(self) -> None:
        """Keep the Cube's real ATTITUDE source rate at the configured target."""
        rate_hz = self.config.mavlink.orientation_stream_rate_hz
        now = self.monotonic_clock()
        if (
            self.config.role != "master"
            or rate_hz <= 0
            or now < self._last_orientation_stream_request + 5.0
        ):
            return
        request = getattr(self.mavlink, "request_message_interval", None)
        if request is None:
            LOG.warning("MAVLink interface cannot request an orientation stream rate")
            self._last_orientation_stream_request = now
            return
        try:
            request(
                30,
                rate_hz,
                self.config.mavlink.target_system,
                self.config.mavlink.target_component,
            )
            self._last_orientation_stream_request = now
            LOG.info("requested Cube ATTITUDE stream at %.1f Hz", rate_hz)
        except (OSError, ValueError) as exc:
            LOG.warning("could not request Cube ATTITUDE stream rate: %s", exc)

    def run(self) -> None:
        LOG.info("starting %s node %d", self.config.role, self.config.node_id)
        try:
            self.status_service = StatusService(
                self.statistics,
                self.config.status,
                self.config.role,
                self.config.node_id,
                self.monotonic_clock,
            )
            self.status_service.start()
            while self.running.is_set():
                self.step()
        finally:
            if self.status_service is not None:
                self.status_service.close()
            self.mavlink.close()
            if self.decoder_worker is not None:
                self.decoder_worker.close()
            if self.tune_worker is not None:
                self.tune_worker.close()
            self.sdr.close()
            if self.recorder is not None:
                self.recorder.close()
            LOG.info("stopped %s node %d", self.config.role, self.config.node_id)

    def stop(self, *_args: object) -> None:
        self.running.clear()


def run(config: RadioConfig) -> None:
    app = TelemetryRadioApp(config)
    signal.signal(signal.SIGINT, app.stop)
    signal.signal(signal.SIGTERM, app.stop)
    app.run()
