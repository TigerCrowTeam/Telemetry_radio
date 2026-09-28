from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

import numpy as np

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class DecodeBatch:
    frames: list[tuple[bytes, int | None]]
    fec_corrected_bits: int
    fec_uncorrectable_frames: int
    failure_score: float | None
    failure_corrected_score: float | None


class DecoderWorker:
    """Continuously drain timestamped SDR blocks through one RX-only modem."""

    def __init__(
        self,
        radio,
        modem,
        *,
        sample_rate: float,
        output_batches: int = 1024,
        record_iq: Callable[[np.ndarray], None] | None = None,
    ):
        self.radio = radio
        self.modem = modem
        self.sample_rate = sample_rate
        self.record_iq = record_iq
        self._output: queue.Queue[DecodeBatch] = queue.Queue(output_batches)
        self._running = threading.Event()
        self._running.set()
        self._lock = threading.Lock()
        self._processing_ms: deque[float] = deque(maxlen=2048)
        self._blocks = 0
        self._samples = 0
        self._frames = 0
        self._discontinuities = 0
        self._output_drops = 0
        self._processing_seconds = 0.0
        self._last_time_ns: int | None = None
        self._last_sample_count = 0
        self._channel_power = 0.0
        self._error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="telemetry-radio-decoder",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        try:
            while self._running.is_set():
                block = self.radio.receive_timed(timeout_us=5_000)
                samples = block.samples
                if not len(samples):
                    continue
                started = time.perf_counter()
                if self.record_iq is not None:
                    self.record_iq(samples)
                discontinuity = False
                if block.time_ns is not None and self._last_time_ns is not None:
                    expected = self._last_time_ns + int(
                        self._last_sample_count / self.sample_rate * 1e9
                    )
                    tolerance = max(1, int(2 / self.sample_rate * 1e9))
                    discontinuity = abs(block.time_ns - expected) > tolerance
                corrected_before = self.modem.fec_corrected_bits
                uncorrectable_before = self.modem.fec_uncorrectable_frames
                frames = self.modem.feed_timed(samples, block.time_ns)
                elapsed = time.perf_counter() - started
                corrected = self.modem.fec_corrected_bits - corrected_before
                uncorrectable = self.modem.fec_uncorrectable_frames - uncorrectable_before
                batch = DecodeBatch(
                    frames,
                    corrected,
                    uncorrectable,
                    self.modem.last_fec_failure_score,
                    self.modem.last_fec_failure_corrected_score,
                )
                if frames or corrected or uncorrectable:
                    try:
                        self._output.put_nowait(batch)
                    except queue.Full:
                        with self._lock:
                            self._output_drops += len(frames)
                with self._lock:
                    self._blocks += 1
                    self._samples += len(samples)
                    self._frames += len(frames)
                    self._processing_seconds += elapsed
                    self._processing_ms.append(elapsed * 1000.0)
                    self._channel_power = float(np.mean(np.abs(samples) ** 2))
                    if discontinuity:
                        self._discontinuities += 1
                self._last_time_ns = block.time_ns
                self._last_sample_count = len(samples)
        except BaseException as exc:
            LOG.exception("decoder worker stopped unexpectedly")
            self._error = exc
            self._running.clear()

    def receive(self, timeout: float = 0.0) -> DecodeBatch | None:
        if self._error is not None:
            raise RuntimeError(f"decoder worker failed: {self._error!r}") from self._error
        try:
            return self._output.get(timeout=timeout) if timeout else self._output.get_nowait()
        except queue.Empty:
            return None

    def diagnostics(self) -> dict[str, int | float]:
        with self._lock:
            processing = sorted(self._processing_ms)
            sample_seconds = self._samples / self.sample_rate
            return {
                "decoder_iq_blocks": self._blocks,
                "decoder_iq_samples": self._samples,
                "decoder_frames": self._frames,
                "decoder_timestamp_discontinuities": self._discontinuities,
                "decoder_output_drops": self._output_drops,
                "decoder_queue_depth": self._output.qsize(),
                "decoder_realtime_utilization": (
                    self._processing_seconds / sample_seconds if sample_seconds else 0.0
                ),
                "decoder_processing_p95_ms": (
                    processing[int(0.95 * (len(processing) - 1))] if processing else 0.0
                ),
                "decoder_processing_max_ms": processing[-1] if processing else 0.0,
                "decoder_channel_power": self._channel_power,
            }

    def close(self) -> None:
        self._running.clear()
        self._thread.join(timeout=1.0)
