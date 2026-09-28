from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

from .config import SDRConfig

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class SampleBlock:
    samples: np.ndarray
    time_ns: int | None = None


def make_soapy_device(
    api: Any,
    arguments: dict[str, str],
    *,
    attempts: int = 1,
    retry_seconds: float = 0.0,
) -> Any:
    """Create a device with a selector accepted by old and new SWIG bindings."""
    selector = api.SoapySDRKwargs(arguments) if hasattr(api, "SoapySDRKwargs") else arguments
    for attempt in range(1, attempts + 1):
        try:
            return api.Device(selector)
        except (IndexError, RuntimeError) as exc:
            if attempt >= attempts:
                raise
            LOG.warning(
                "SDR open attempt %d/%d failed (%s); retrying in %.1f seconds",
                attempt,
                attempts,
                exc,
                retry_seconds,
            )
            time.sleep(retry_seconds)
    raise RuntimeError("unreachable SDR open retry state")


def _supported(value: float, ranges: Iterable[Any]) -> bool:
    ranges = list(ranges)
    if not ranges:
        return True
    return any(float(item.minimum()) <= value <= float(item.maximum()) for item in ranges)


def validate_device_capabilities(
    device: Any,
    config: SDRConfig,
    frequencies_hz: Sequence[float],
    rx: int,
    tx: int,
) -> None:
    """Check configured settings against ranges reported by a SoapySDR device."""
    for direction, label, gain in ((rx, "RX", config.rx_gain), (tx, "TX", config.tx_gain)):
        channels = int(device.getNumChannels(direction))
        if config.channel >= channels:
            raise ValueError(
                f"SDR {label} channel {config.channel} is unavailable; device reports {channels} channels"
            )
        checks = (
            (
                "sample rate",
                config.sample_rate,
                device.getSampleRateRange(direction, config.channel),
            ),
            ("bandwidth", config.bandwidth, device.getBandwidthRange(direction, config.channel)),
            ("frequency", None, device.getFrequencyRange(direction, config.channel)),
        )
        for name, value, ranges in checks:
            if name == "frequency":
                values = frequencies_hz
            else:
                assert value is not None
                values = (value,)
            for candidate in values:
                if not _supported(float(candidate), ranges):
                    raise ValueError(f"SDR {label} does not support configured {name} {candidate}")
        gain_range = device.getGainRange(direction, config.channel)
        if not float(gain_range.minimum()) <= gain <= float(gain_range.maximum()):
            raise ValueError(f"SDR {label} gain {gain} is outside the reported range")
        antenna = config.rx_antenna if direction == rx else config.tx_antenna
        if antenna is not None and antenna not in device.listAntennas(direction, config.channel):
            raise ValueError(f"SDR {label} does not provide configured antenna {antenna}")


class Radio(Protocol):
    def tune(self, frequency_hz: float) -> None: ...
    def tune_at(self, frequency_hz: float, time_ns: int) -> None: ...
    def transmit(self, samples: np.ndarray) -> None: ...
    def transmit_at(self, samples: np.ndarray, time_ns: int) -> None: ...
    def transmit_with_lead(self, samples: np.ndarray, lead_ns: int) -> int: ...
    def hardware_time(self) -> int: ...
    def set_hardware_time(self, time_ns: int) -> None: ...
    def receive_timed(self, count: int = 4096, timeout_us: int = 50_000) -> SampleBlock: ...
    def receive(self, count: int = 4096, timeout_us: int = 50_000) -> np.ndarray: ...
    def close(self) -> None: ...


class SoapyRadio:
    def __init__(self, config: SDRConfig, frequencies_hz: Sequence[float] = ()):
        try:
            import SoapySDR
            from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX, SOAPY_SDR_TX
        except ImportError as exc:
            raise RuntimeError("SoapySDR Python bindings are required for radio operation") from exc
        self.api = SoapySDR
        self.RX, self.TX, self.CF32 = SOAPY_SDR_RX, SOAPY_SDR_TX, SOAPY_SDR_CF32
        self.END_BURST = getattr(SoapySDR, "SOAPY_SDR_END_BURST", 0)
        self.HAS_TIME = getattr(SoapySDR, "SOAPY_SDR_HAS_TIME", 0)
        self.TIME_ERROR = getattr(SoapySDR, "SOAPY_SDR_TIME_ERROR", None)
        self.STREAM_ERROR = getattr(SoapySDR, "SOAPY_SDR_STREAM_ERROR", None)
        self.CORRUPTION = getattr(SoapySDR, "SOAPY_SDR_CORRUPTION", None)
        self.config = config
        self.device = make_soapy_device(
            SoapySDR,
            config.device,
            attempts=config.open_retry_attempts,
            retry_seconds=config.open_retry_seconds,
        )
        if frequencies_hz:
            validate_device_capabilities(self.device, config, frequencies_hz, self.RX, self.TX)
        for direction, gain, antenna in (
            (self.RX, config.rx_gain, config.rx_antenna),
            (self.TX, config.tx_gain, config.tx_antenna),
        ):
            if antenna is not None:
                self.device.setAntenna(direction, config.channel, antenna)
            self.device.setSampleRate(direction, config.channel, config.sample_rate)
            self.device.setBandwidth(direction, config.channel, config.bandwidth)
            self.device.setGain(direction, config.channel, gain)
        self.rx_stream = self.device.setupStream(self.RX, self.CF32, [config.channel])
        self.tx_stream = self.device.setupStream(self.TX, self.CF32, [config.channel])
        self.tx_mtu = (
            int(self.device.getStreamMTU(self.tx_stream))
            if hasattr(self.device, "getStreamMTU")
            else 4096
        )
        self.device.activateStream(self.rx_stream)
        self.rx_active = True
        self.tx_active = False
        self.rx_overflows = 0
        self.rx_timeouts = 0
        self.rx_stream_errors = 0
        self.timed_transmissions = 0
        self.timed_transmit_late = 0
        self.timed_receive_windows = 0

    def supports_hardware_time(self) -> bool:
        return bool(self.HAS_TIME and self.device.hasHardwareTime(""))

    def hardware_time(self) -> int:
        if not self.supports_hardware_time():
            raise RuntimeError("SDR does not support hardware time")
        return int(self.device.getHardwareTime(""))

    def set_hardware_time(self, time_ns: int) -> None:
        if not self.supports_hardware_time():
            raise RuntimeError("SDR does not support hardware time")
        self.device.setHardwareTime(int(time_ns), "")

    def tune(self, frequency_hz: float) -> None:
        for direction in (self.RX, self.TX):
            self.device.setFrequency(direction, self.config.channel, frequency_hz)
        LOG.info("tuned to %.3f MHz", frequency_hz / 1e6)

    def tune_at(self, frequency_hz: float, time_ns: int) -> None:
        """Queue an RX+TX LO change against the device hardware clock."""
        if not self.supports_hardware_time() or not hasattr(self.device, "setCommandTime"):
            raise RuntimeError("timed tuning is not supported by this SDR backend")
        if time_ns <= self.hardware_time():
            raise IOError("timed tune target is not in the future")
        self.device.setCommandTime(int(time_ns), "")
        try:
            for direction in (self.RX, self.TX):
                self.device.setFrequency(direction, self.config.channel, frequency_hz)
        finally:
            self.device.setCommandTime(0, "")
        LOG.info("queued tune to %.3f MHz at %d ns", frequency_hz / 1e6, time_ns)

    def transmit(self, samples: np.ndarray) -> None:
        self._transmit(samples, None)

    def transmit_at(self, samples: np.ndarray, time_ns: int) -> None:
        if not self.supports_hardware_time():
            raise RuntimeError("timed transmit requested without hardware-time support")
        now = self.hardware_time()
        if time_ns <= now:
            self.timed_transmit_late += 1
            raise IOError(f"timed transmit is late by {now - time_ns} ns")
        self._transmit(samples, int(time_ns))

    def transmit_with_lead(self, samples: np.ndarray, lead_ns: int) -> int:
        target_ns = self.hardware_time() + int(lead_ns)
        self.transmit_at(samples, target_ns)
        return target_ns

    def _transmit(self, samples: np.ndarray, time_ns: int | None) -> None:
        # Explicit TDD switching works with SDRs that cannot keep RX and TX active
        # together and ensures this node never receives while it transmits.
        if self.rx_active:
            self.device.deactivateStream(self.rx_stream)
            self.rx_active = False
        self.device.activateStream(self.tx_stream)
        self.tx_active = True
        try:
            position = 0
            while position < len(samples):
                remaining = len(samples) - position
                requested = min(remaining, self.tx_mtu)
                flags = self.END_BURST if requested == remaining else 0
                if time_ns is not None and position == 0:
                    flags |= self.HAS_TIME
                result = self.device.writeStream(
                    self.tx_stream,
                    [samples[position:]],
                    requested,
                    flags=flags,
                    timeNs=0 if time_ns is None else time_ns,
                )
                if result.ret <= 0:
                    if self.TIME_ERROR is not None and result.ret == self.TIME_ERROR:
                        self.timed_transmit_late += 1
                    raise IOError(f"SoapySDR TX failed: {result.ret}")
                if flags and result.ret != requested:
                    raise IOError(
                        "SoapySDR returned a partial final TX burst write: "
                        f"{result.ret}/{requested}"
                    )
                position += result.ret
            if time_ns is not None:
                self.timed_transmissions += 1
                rx_time_ns = time_ns + int(len(samples) / self.config.sample_rate * 1e9)
                self.device.activateStream(
                    self.rx_stream,
                    flags=self.HAS_TIME,
                    timeNs=rx_time_ns,
                    numElems=0,
                )
                self.rx_active = True
                self.timed_receive_windows += 1
            # writeStream reports samples accepted by the host-side FIFO, not
            # necessarily samples already emitted by the radio.  Deactivating
            # immediately can discard the queued tail on UHD/B2xx devices.
            # Allow one burst airtime plus a small USB/FPGA drain margin. At
            # 2 Msps the previous 5 ms fixed margin dominated short packets
            # and delayed the peer's receive window.
            delay = 0.0
            if time_ns is not None:
                delay = max(0.0, (time_ns - self.hardware_time()) / 1e9)
            time.sleep(delay + len(samples) / self.config.sample_rate + 0.001)
        finally:
            self.device.deactivateStream(self.tx_stream)
            self.tx_active = False
            if not self.rx_active:
                self.device.activateStream(self.rx_stream)
                self.rx_active = True

    def receive(self, count: int = 4096, timeout_us: int = 50_000) -> np.ndarray:
        return self.receive_timed(count, timeout_us).samples

    def receive_timed(self, count: int = 4096, timeout_us: int = 50_000) -> SampleBlock:
        buffer = np.empty(count, np.complex64)
        result = self.device.readStream(self.rx_stream, [buffer], count, timeoutUs=timeout_us)
        if result.ret < 0:
            # Timeouts and overflows are recoverable receive conditions. UHD can
            # report an overflow while its USB buffers settle after activation.
            recoverable = {self.api.SOAPY_SDR_TIMEOUT}
            overflow = getattr(self.api, "SOAPY_SDR_OVERFLOW", None)
            if overflow is not None:
                recoverable.add(overflow)
            if result.ret in recoverable:
                if overflow is not None and result.ret == overflow:
                    self.rx_overflows += 1
                else:
                    self.rx_timeouts += 1
                return SampleBlock(buffer[:0])
            # UHD can report a transient stream/corruption/time error while RX
            # is reactivated after a half-duplex TX burst. The next read is the
            # recovery operation; terminating the continuous decoder here would
            # silently remove the MAVLink UDP feed from QGroundControl.
            transition_errors = {
                value
                for value in (self.STREAM_ERROR, self.CORRUPTION, self.TIME_ERROR)
                if value is not None
            }
            if result.ret in transition_errors:
                self.rx_stream_errors += 1
                LOG.warning("recovering from transient SoapySDR RX result %d", result.ret)
                return SampleBlock(buffer[:0])
            raise IOError(f"SoapySDR RX failed: {result.ret}")
        flags = int(getattr(result, "flags", 0))
        time_ns = int(result.timeNs) if flags & self.HAS_TIME else None
        return SampleBlock(buffer[: result.ret], time_ns)

    def schedule_receive(self, time_ns: int, count: int) -> None:
        if not self.supports_hardware_time():
            raise RuntimeError("timed receive requested without hardware-time support")
        if time_ns <= self.hardware_time():
            raise IOError("timed receive activation is late")
        if self.rx_active:
            self.device.deactivateStream(self.rx_stream)
        self.device.activateStream(
            self.rx_stream,
            flags=self.HAS_TIME,
            timeNs=int(time_ns),
            numElems=int(count),
        )
        self.rx_active = True
        self.timed_receive_windows += 1

    def close(self) -> None:
        if self.rx_active:
            self.device.deactivateStream(self.rx_stream)
        if self.tx_active:
            self.device.deactivateStream(self.tx_stream)
        self.device.closeStream(self.rx_stream)
        self.device.closeStream(self.tx_stream)

    def receive_diagnostics(self) -> dict[str, int]:
        return {
            "rx_overflows": self.rx_overflows,
            "rx_timeouts": self.rx_timeouts,
            "rx_stream_errors": self.rx_stream_errors,
            "timed_transmissions": self.timed_transmissions,
            "timed_transmit_late": self.timed_transmit_late,
            "timed_receive_windows": self.timed_receive_windows,
        }


class BufferedRadio:
    """Continuously drain a radio into a bounded queue on a dedicated thread."""

    def __init__(
        self,
        radio: Radio,
        *,
        block_size: int = 4096,
        queue_blocks: int = 256,
        read_timeout_us: int = 5_000,
    ):
        if block_size <= 0 or queue_blocks <= 0 or read_timeout_us <= 0:
            raise ValueError("buffer dimensions and read timeout must be positive")
        self.radio = radio
        self.block_size = block_size
        self.read_timeout_us = read_timeout_us
        self._queue: queue.Queue[SampleBlock] = queue.Queue(queue_blocks)
        self._io_lock = threading.Lock()
        self._paused = threading.Event()
        self._running = threading.Event()
        self._running.set()
        self._error: BaseException | None = None
        self.rx_buffer_drops = 0
        self.rx_buffer_flushes = 0
        self._thread = threading.Thread(
            target=self._drain,
            name="telemetry-radio-rx",
            daemon=True,
        )
        self._thread.start()

    def _drain(self) -> None:
        try:
            while self._running.is_set():
                if self._paused.is_set():
                    time.sleep(0.0005)
                    continue
                with self._io_lock:
                    timed_receive = getattr(self.radio, "receive_timed", None)
                    if timed_receive is None:
                        samples = self.radio.receive(self.block_size, self.read_timeout_us)
                        received = SampleBlock(samples)
                    else:
                        received = timed_receive(self.block_size, self.read_timeout_us)
                if not len(received.samples):
                    continue
                block = SampleBlock(
                    np.asarray(received.samples, dtype=np.complex64).copy(), received.time_ns
                )
                try:
                    self._queue.put_nowait(block)
                except queue.Full:
                    try:
                        self._queue.get_nowait()
                    except queue.Empty:
                        pass
                    self.rx_buffer_drops += 1
                    self._queue.put_nowait(block)
        except BaseException as exc:  # surfaced to the application thread
            self._error = exc
            self._running.clear()

    def _discard_buffered(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
                self.rx_buffer_flushes += 1
            except queue.Empty:
                return

    def tune(self, frequency_hz: float) -> None:
        self._paused.set()
        try:
            with self._io_lock:
                self._discard_buffered()
                self.radio.tune(frequency_hz)
        finally:
            self._paused.clear()

    def tune_at(self, frequency_hz: float, time_ns: int) -> None:
        # UHD accepts control commands concurrently with streaming. Do not take
        # the RX I/O lock here: preparing a calibrated future tune can block for
        # 150 ms, and stopping the drain thread would discard the current dwell.
        self.radio.tune_at(frequency_hz, time_ns)

    def flush_receive(self) -> None:
        with self._io_lock:
            self._discard_buffered()

    def transmit(self, samples: np.ndarray) -> None:
        self._paused.set()
        try:
            with self._io_lock:
                self._discard_buffered()
                self.radio.transmit(samples)
                self._discard_buffered()
        finally:
            self._paused.clear()

    def hardware_time(self) -> int:
        with self._io_lock:
            return self.radio.hardware_time()

    def set_hardware_time(self, time_ns: int) -> None:
        self._paused.set()
        try:
            with self._io_lock:
                self._discard_buffered()
                self.radio.set_hardware_time(time_ns)
        finally:
            self._paused.clear()

    def transmit_at(self, samples: np.ndarray, time_ns: int) -> None:
        self._paused.set()
        try:
            with self._io_lock:
                self._discard_buffered()
                self.radio.transmit_at(samples, time_ns)
                self._discard_buffered()
        finally:
            self._paused.clear()

    def transmit_with_lead(self, samples: np.ndarray, lead_ns: int) -> int:
        """Choose the target only after gaining exclusive SDR access."""
        self._paused.set()
        try:
            with self._io_lock:
                self._discard_buffered()
                target_ns = self.radio.hardware_time() + int(lead_ns)
                self.radio.transmit_at(samples, target_ns)
                self._discard_buffered()
                return target_ns
        finally:
            self._paused.clear()

    def schedule_receive(self, time_ns: int, count: int) -> None:
        self._paused.set()
        try:
            with self._io_lock:
                self._discard_buffered()
                self.radio.schedule_receive(time_ns, count)
        finally:
            self._paused.clear()

    def receive(self, count: int = 4096, timeout_us: int = 50_000) -> np.ndarray:
        return self.receive_timed(count, timeout_us).samples

    def receive_timed(self, count: int = 4096, timeout_us: int = 50_000) -> SampleBlock:
        if self._error is not None:
            raise RuntimeError("SDR receive thread failed") from self._error
        try:
            return self._queue.get(timeout=min(timeout_us / 1_000_000, 0.01))
        except queue.Empty:
            return SampleBlock(np.empty(0, dtype=np.complex64))

    def receive_diagnostics(self) -> dict[str, int]:
        diagnostics = {
            "rx_buffer_drops": self.rx_buffer_drops,
            "rx_buffer_flushes": self.rx_buffer_flushes,
        }
        source = getattr(self.radio, "receive_diagnostics", None)
        if source is not None:
            diagnostics.update(source())
        return diagnostics

    def close(self) -> None:
        self._running.clear()
        self._thread.join(timeout=max(1.0, self.read_timeout_us / 500_000))
        self.radio.close()
