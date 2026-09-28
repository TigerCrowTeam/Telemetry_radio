from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Any


class LinkStatistics:
    """Thread-safe link counters and gauges with JSON-friendly snapshots."""

    COUNTERS = (
        "packets_enqueued",
        "packets_transmitted",
        "packets_received",
        "bytes_transmitted",
        "bytes_received",
        "samples_transmitted",
        "airtime_seconds_transmitted",
        "invalid_packets",
        "protocol_version_mismatches",
        "authentication_failures",
        "replay_rejections",
        "sync_freshness_rejections",
        "crc_failures",
        "fec_corrected_bits",
        "fec_uncorrectable_frames",
        "retries",
        "acknowledgements_sent",
        "acknowledgements_received",
        "duplicates_suppressed",
        "queue_drops",
        "queue_evictions",
        "telemetry_coalesced",
        "stale_packets_dropped",
        "rate_limit_deferrals",
        "channel_busy_deferrals",
        "transmit_errors",
        "rx_overflows",
        "rx_timeouts",
        "rx_stream_errors",
        "rx_buffer_drops",
        "rx_buffer_flushes",
        "timed_transmissions",
        "timed_transmit_late",
        "timed_receive_windows",
        "scheduled_tdd_transmissions",
        "frequency_hops",
        "tdd_opportunities_missed",
        "orientation_opportunities_used",
        "orientation_deadline_misses",
        "recording_failures",
        "synchronization_losses",
        "reacquisitions",
        "mavlink_frames_from_local",
        "mavlink_frames_to_local",
        "mavlink_frames_bundled",
        "decoder_iq_blocks",
        "decoder_iq_samples",
        "decoder_frames",
        "decoder_timestamp_discontinuities",
        "decoder_output_drops",
        "gcs_requests_shaped",
    )

    def __init__(self, monotonic_clock=time.monotonic):
        self._clock = monotonic_clock
        self._started_at = monotonic_clock()
        self._lock = threading.Lock()
        self._counters: dict[str, int | float] = {name: 0 for name in self.COUNTERS}
        self._gauges: dict[str, int | float | bool | None] = {
            "queue_depth": 0,
            "queue_capacity": 0,
            "awaiting_acknowledgements": 0,
            "estimated_signal_power": 0.0,
            "active_frequency_hz": None,
            "frequency_slot": None,
            "transmit_allowed": False,
            "synchronized": True,
            "hardware_clock_offset_ns": None,
            "hardware_clock_drift_ppm": None,
            "hardware_clock_observations": 0,
            "last_fec_failure_score": None,
            "last_fec_failure_corrected_score": None,
            "scheduled_tx_lead_ms": None,
            "scheduled_tx_target_ns": None,
            "decoder_queue_depth": 0,
            "decoder_realtime_utilization": 0.0,
            "decoder_processing_p95_ms": 0.0,
            "decoder_processing_max_ms": 0.0,
            "flight_controller_heartbeat_age_ms": None,
            "flight_controller_heartbeat_stale": True,
        }
        self._mavlink: dict[str, dict[str, int | float]] = defaultdict(dict)
        self._mavlink_ages: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=2048))
        self._mavlink_intervals: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=2048)
        )
        self._mavlink_last_delivery: dict[str, float] = {}

    def increment(self, name: str, amount: int | float = 1) -> None:
        with self._lock:
            if name not in self._counters:
                raise KeyError(f"unknown statistics counter: {name}")
            self._counters[name] += amount

    def set_gauge(self, name: str, value: int | float | bool | None) -> None:
        with self._lock:
            if name not in self._gauges:
                raise KeyError(f"unknown statistics gauge: {name}")
            self._gauges[name] = value

    def increment_mavlink(self, message_type: str | None, event: str, amount: int = 1) -> None:
        name = message_type or "UNKNOWN"
        with self._lock:
            values = self._mavlink[name]
            values[event] = int(values.get(event, 0)) + amount
            if event == "delivered":
                now = self._clock()
                previous = self._mavlink_last_delivery.get(name)
                if previous is not None:
                    self._mavlink_intervals[name].append(max(0.0, now - previous))
                self._mavlink_last_delivery[name] = now

    def observe_mavlink_age(self, message_type: str | None, age_seconds: float) -> None:
        name = message_type or "UNKNOWN"
        with self._lock:
            self._mavlink_ages[name].append(max(0.0, age_seconds))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            mavlink = {name: dict(values) for name, values in self._mavlink.items()}
            for name, samples in self._mavlink_ages.items():
                ordered = sorted(samples)
                if not ordered:
                    continue
                values = mavlink.setdefault(name, {})
                values["age_samples"] = len(ordered)
                values["age_average_ms"] = 1000.0 * sum(ordered) / len(ordered)
                values["age_maximum_ms"] = 1000.0 * ordered[-1]
                values["age_p95_ms"] = 1000.0 * ordered[int(0.95 * (len(ordered) - 1))]
            for name, samples in self._mavlink_intervals.items():
                ordered = sorted(samples)
                if not ordered:
                    continue
                values = mavlink.setdefault(name, {})
                values["interval_samples"] = len(ordered)
                values["interval_average_ms"] = 1000.0 * sum(ordered) / len(ordered)
                values["interval_maximum_ms"] = 1000.0 * ordered[-1]
                values["interval_p95_ms"] = 1000.0 * ordered[
                    int(0.95 * (len(ordered) - 1))
                ]
            return {
                **self._counters,
                **self._gauges,
                "uptime_seconds": max(0.0, self._clock() - self._started_at),
                "mavlink_by_type": mavlink,
            }
