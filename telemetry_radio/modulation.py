from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

import numpy as np
from reedsolo import ReedSolomonError, RSCodec

from .fec import (
    UncorrectableFECError,
)
from .fec import (
    decode as fec_decode,
)
from .fec import (
    encode as fec_encode,
)
from .fec import (
    encoded_size as fec_encoded_size,
)

SYNC = b"\x55\x55\x55\x55\xd3\x91"
# The B2xx TX/filter chain needs a brief settling interval after a burst starts.
# These alternating symbols are acquisition training only; framing begins at SYNC.
# Thirty-two alternating bytes provide the settling and carrier-acquisition
# margin required by the current B205 conducted-link setup.
TRAINING = b"\x55" * 32
# A repeated 0x55 is alternating in BPSK but maps to one constant QPSK point
# (01, 01, ...). Use a deterministic noise-like training word for QPSK so burst
# acquisition has a sharp correlation peak instead of hundreds of aliases.
QPSK_TRAINING = bytes.fromhex(
    "d37a49c5a612f08e5b31e4972cd068bf"
    "91e6235dac7840f7b2c9540e6fa1833d"
)
MAX_SYNC_BIT_ERRORS = 4
# Score the complete training + sync sequence when choosing burst alignment.  The
# leading 0x55 bytes in SYNC are deliberately repetitive and are not distinctive
# enough on their own to choose between nearby timing candidates.
# Training is long enough to retain a distinctive score through a short RF fade.
# Permit up to 48 bad bits across its 304 bits while still requiring substantially
# better than random correlation.
MAX_PREAMBLE_BIT_ERRORS = 48


def _root_raised_cosine(sps: int, rolloff: float, span_symbols: int) -> np.ndarray:
    """Return unit-energy odd-length RRC taps sampled at ``sps``."""
    count = span_symbols * sps
    t = np.arange(-count // 2, count // 2 + 1, dtype=np.float64) / sps
    taps = np.empty_like(t)
    for index, value in enumerate(t):
        if abs(value) < 1e-12:
            taps[index] = 1.0 + rolloff * (4.0 / np.pi - 1.0)
        elif abs(abs(value) - 1.0 / (4.0 * rolloff)) < 1e-12:
            taps[index] = (rolloff / np.sqrt(2.0)) * (
                (1.0 + 2.0 / np.pi) * np.sin(np.pi / (4.0 * rolloff))
                + (1.0 - 2.0 / np.pi) * np.cos(np.pi / (4.0 * rolloff))
            )
        else:
            numerator = (
                np.sin(np.pi * value * (1.0 - rolloff))
                + 4.0
                * rolloff
                * value
                * np.cos(np.pi * value * (1.0 + rolloff))
            )
            denominator = np.pi * value * (1.0 - (4.0 * rolloff * value) ** 2)
            taps[index] = numerator / denominator
    taps /= np.sqrt(np.sum(taps * taps))
    return taps.astype(np.float32)


class Modem(ABC):
    @abstractmethod
    def modulate(self, payload: bytes) -> np.ndarray: ...

    @abstractmethod
    def feed(self, samples: np.ndarray) -> Iterable[bytes]: ...


class BPSKModem(Modem):
    """Simple coherent BPSK reference modem; replaceable through the Modem interface."""

    def __init__(
        self,
        samples_per_symbol: int = 8,
        max_frame: int = 8192,
        sample_rate: float = 1_000_000,
        carrier_offset_hz: float = 0.0,
        bit_repetitions: int = 1,
        interleaver_depth: int = 16,
        rs_parity_bytes: int = 0,
        rrc_rolloff: float = 0.0,
        rrc_span_symbols: int = 8,
        timing_subsamples: int = 1,
    ):
        if samples_per_symbol < 2:
            raise ValueError("samples_per_symbol must be at least 2")
        if bit_repetitions < 1:
            raise ValueError("bit_repetitions must be at least 1")
        if interleaver_depth < 1:
            raise ValueError("interleaver_depth must be positive")
        self.sps = samples_per_symbol
        self.max_frame = max_frame
        self.sample_rate = sample_rate
        self.carrier_offset_hz = carrier_offset_hz
        self.bit_repetitions = bit_repetitions
        self.interleaver_depth = interleaver_depth
        if rs_parity_bytes < 0 or rs_parity_bytes % 2:
            raise ValueError("rs_parity_bytes must be a non-negative even value")
        self.rs_parity_bytes = rs_parity_bytes
        if not 0.0 <= rrc_rolloff <= 1.0:
            raise ValueError("rrc_rolloff must be between 0 and 1")
        if rrc_rolloff and (rrc_span_symbols < 2 or rrc_span_symbols % 2):
            raise ValueError("rrc_span_symbols must be a positive even value")
        self.rrc_rolloff = rrc_rolloff
        self.rrc_span_symbols = rrc_span_symbols
        if timing_subsamples < 1:
            raise ValueError("timing_subsamples must be positive")
        self.timing_subsamples = timing_subsamples
        self._rrc_taps = (
            _root_raised_cosine(self.sps, rrc_rolloff, rrc_span_symbols)
            if rrc_rolloff
            else None
        )
        self._rs = RSCodec(rs_parity_bytes) if rs_parity_bytes else None
        self._samples = np.empty(0, dtype=np.complex64)
        self._sync_bits = np.unpackbits(np.frombuffer(SYNC, dtype=np.uint8))
        self._training_bits = np.unpackbits(np.frombuffer(TRAINING, dtype=np.uint8))
        self._preamble_bits = np.concatenate((self._training_bits, self._sync_bits))
        self.fec_corrected_bits = 0
        self.fec_uncorrectable_frames = 0
        self._noise_power: float | None = None
        self._burst_active = False
        self._live_block_size = 0
        self._buffer_time_ns: int | None = None
        self._last_frame_start_sample: float | None = None
        self._decoded_arrival_times: list[int | None] = []
        self.last_fec_failure_score: float | None = None
        self.last_fec_failure_corrected_score: float | None = None

    def frame_size(self, payload_size: int) -> int:
        """Return on-air envelope bytes before conversion to BPSK samples."""
        protected_size = fec_encoded_size(payload_size, self.interleaver_depth) + self.rs_parity_bytes
        return (
            len(TRAINING) + len(SYNC) + 2 + protected_size
        ) * self.bit_repetitions

    def sample_count(self, payload_size: int) -> int:
        base = self.frame_size(payload_size) * 8 * self.sps
        return base + (len(self._rrc_taps) - 1 if self._rrc_taps is not None else 0)

    def modulate(self, payload: bytes) -> np.ndarray:
        if len(payload) > self.max_frame:
            raise ValueError("frame exceeds modem limit")
        encoded = fec_encode(payload, self.interleaver_depth)
        if self._rs is not None:
            encoded = bytes(self._rs.encode(encoded))
        wire = TRAINING + SYNC + len(encoded).to_bytes(2, "big") + encoded
        bits = np.unpackbits(np.frombuffer(wire, dtype=np.uint8))
        if self.bit_repetitions > 1:
            bits = np.repeat(bits, self.bit_repetitions)
        symbols = bits.astype(np.float32) * 2.0 - 1.0
        if self._rrc_taps is None:
            samples = np.repeat(symbols, self.sps).astype(np.complex64)
        else:
            impulses = np.zeros(len(symbols) * self.sps, dtype=np.float32)
            impulses[:: self.sps] = symbols
            samples = np.convolve(impulses, self._rrc_taps, mode="full").astype(np.complex64)
            peak = float(np.max(np.abs(samples)))
            if peak:
                samples /= peak
        if self.carrier_offset_hz:
            phase = 2 * np.pi * self.carrier_offset_hz * np.arange(len(samples)) / self.sample_rate
            samples *= np.exp(1j * phase).astype(np.complex64)
        return samples

    def feed(self, samples: np.ndarray) -> Iterable[bytes]:
        return [payload for payload, _arrival in self.feed_timed(samples, None)]

    def feed_timed(
        self, samples: np.ndarray, time_ns: int | None
    ) -> list[tuple[bytes, int | None]]:
        incoming = np.asarray(samples, dtype=np.complex64)
        if not len(incoming):
            return []
        if time_ns is not None:
            if self._buffer_time_ns is None or not len(self._samples):
                self._buffer_time_ns = int(time_ns)
            else:
                expected = self._buffer_time_ns + int(len(self._samples) / self.sample_rate * 1e9)
                tolerance = max(1, int(2 / self.sample_rate * 1e9))
                if abs(int(time_ns) - expected) > tolerance:
                    # Never correlate across a timed RX discontinuity.
                    self._samples = np.empty(0, dtype=np.complex64)
                    self._buffer_time_ns = int(time_ns)
                    self._burst_active = False
        self._decoded_arrival_times = []
        power = float(np.mean(np.abs(incoming) ** 2))
        minimum_samples = (len(SYNC) + 2) * 8 * self.sps
        idle = (
            self._noise_power is not None
            # Unit-amplitude simulation/replay streams contain no idle gap;
            # energy gating is only needed for low-level live SDR samples.
            and self._noise_power < 1e-4
            and power <= max(self._noise_power * 3.0, self._noise_power + 1e-12)
        )
        if idle and not self._burst_active and len(self._samples) >= minimum_samples:
            # Searching an ever-growing idle-noise buffer on every SDR read is
            # quadratic and eventually causes hardware RX overflows. Keep two
            # read-sized blocks so a burst beginning at a block edge retains
            # its complete preamble, but defer correlation until energy rises.
            keep = max(minimum_samples, len(incoming) * 2)
            self._samples = np.concatenate((self._samples, incoming))
            self._keep_tail(keep)
            self._noise_power = 0.98 * self._noise_power + 0.02 * power
            self._live_block_size = max(self._live_block_size, len(incoming))
            return []
        if idle and not self._burst_active:
            # There is not enough history to contain a frame yet. Avoid an
            # allocation and frame-search pass for every idle SDR block.
            self._noise_power = 0.98 * self._noise_power + 0.02 * power
            self._live_block_size = max(self._live_block_size, len(incoming))
            if len(self._samples) > minimum_samples:
                self._keep_tail(minimum_samples)
            self._samples = np.concatenate((self._samples, incoming))
            self._keep_tail(minimum_samples)
            return []
        self._samples = np.concatenate((self._samples, incoming))
        if self._noise_power is None:
            self._noise_power = power
        elif idle:
            self._noise_power = 0.98 * self._noise_power + 0.02 * power
        else:
            self._burst_active = True
        # Live SDR streams have a low idle-noise floor and explicit gaps between
        # TDD bursts. Searching every growing partial burst repeats the expensive
        # timing/correlation work and can starve the SDR receive stream. Accumulate
        # the burst and search once when the following idle block arrives. Unit-
        # amplitude simulation/replay streams retain immediate streaming decode.
        live_burst = (
            self._noise_power is not None
            and self._noise_power < 1e-4
            and self._burst_active
            and not idle
            and len(incoming) >= self._live_block_size
        )
        if live_burst:
            maximum = self.sample_count(self.max_frame)
            if len(self._samples) > maximum:
                self._keep_tail(maximum)
            return []
        frames: list[bytes] = []
        while True:
            found = self._find_frame()
            if found is None:
                # Bound memory while retaining enough history for the largest frame.
                maximum = self.sample_count(self.max_frame)
                if len(self._samples) > maximum:
                    self._keep_tail(maximum)
                break
            payload, consumed = found
            if payload is not None:
                frames.append(payload)
                arrival = None
                if self._buffer_time_ns is not None and self._last_frame_start_sample is not None:
                    arrival = self._buffer_time_ns + int(
                        self._last_frame_start_sample / self.sample_rate * 1e9
                    )
                self._decoded_arrival_times.append(arrival)
            self._consume(consumed)
        if idle and self._burst_active:
            self._burst_active = False
            keep = max(minimum_samples, len(incoming) * 2)
            self._keep_tail(keep)
        return list(zip(frames, self._decoded_arrival_times, strict=True))

    def _consume(self, count: int) -> None:
        self._samples = self._samples[count:]
        if self._buffer_time_ns is not None:
            self._buffer_time_ns += int(count / self.sample_rate * 1e9)

    def _keep_tail(self, count: int) -> None:
        removed = max(0, len(self._samples) - count)
        if removed:
            self._consume(removed)

    def _find_frame(self) -> tuple[bytes | None, int] | None:
        minimum_symbols = (len(SYNC) + 2) * 8
        if len(self._samples) < minimum_symbols * self.sps:
            return None

        # Squaring removes BPSK data modulation. The phase progression of x^2
        # estimates twice the carrier offset, allowing coherent hard decisions.
        squared = self._samples * self._samples
        correlation = np.sum(squared[1:] * np.conj(squared[:-1]))
        phase_step = np.angle(correlation) / 2.0 if correlation else 0.0
        n = np.arange(len(self._samples), dtype=np.float64)
        corrected = self._samples * np.exp(-1j * phase_step * n)
        matched = (
            np.convolve(corrected, self._rrc_taps, mode="same")
            if self._rrc_taps is not None
            else corrected
        )

        failed_consumed: int | None = None
        failed_score = float("-inf")
        incomplete_score = float("-inf")
        # Rectangular BPSK is optimally detected by integrating each symbol. Test
        # all integer sample phases using vectorized reshape/mean operations. This
        # is both more robust than choosing a single sample and substantially less
        # expensive than interpolating four fractional offsets per sample phase.
        for timing_index in range(self.sps * self.timing_subsamples):
            timing = timing_index / self.timing_subsamples
            usable = int((len(matched) - timing) // self.sps)
            if usable < minimum_symbols:
                continue
            if self.timing_subsamples == 1 and self._rrc_taps is None:
                timing_sample = int(timing)
                sampled = matched[timing_sample : timing_sample + usable * self.sps]
                symbols = sampled.reshape(-1, self.sps).mean(axis=1).astype(np.complex64)
            elif self.timing_subsamples == 1:
                timing_sample = int(timing)
                symbols = matched[
                    timing_sample : timing_sample + usable * self.sps : self.sps
                ].astype(np.complex64)
            else:
                if self._rrc_taps is None:
                    positions = timing + np.arange(usable * self.sps, dtype=np.float64)
                    axis = np.arange(len(matched), dtype=np.float64)
                    sampled = np.interp(positions, axis, matched.real) + 1j * np.interp(
                        positions, axis, matched.imag
                    )
                    symbols = sampled.reshape(-1, self.sps).mean(axis=1).astype(
                        np.complex64
                    )
                else:
                    positions = timing + self.sps * np.arange(usable, dtype=np.float64)
                    axis = np.arange(len(matched), dtype=np.float64)
                    symbols = (
                        np.interp(positions, axis, matched.real)
                        + 1j * np.interp(positions, axis, matched.imag)
                    ).astype(np.complex64)
            # Estimate residual carrier drift on this symbol-aligned candidate.
            # The block-wide estimate above can be biased by idle samples and
            # adjacent bursts; per-frame tracking prevents phase from rotating
            # across long MAVLink frames.
            if len(symbols) > 2:
                symbol_squared = symbols * symbols
                residual = np.sum(symbol_squared[1:] * np.conj(symbol_squared[:-1]))
                if residual:
                    residual_step = np.angle(residual) / 2.0
                    symbols = symbols * np.exp(
                        -1j * residual_step * np.arange(len(symbols))
                    ).astype(np.complex64)
            symbol_count = len(symbols)
            if self.bit_repetitions > 1:
                usable_bits = symbol_count // self.bit_repetitions
                if usable_bits < minimum_symbols // 1:
                    continue
                symbols = symbols[: usable_bits * self.bit_repetitions].reshape(
                    usable_bits, self.bit_repetitions
                ).mean(axis=1)
            # The squaring estimator leaves a constant phase and a 180-degree
            # ambiguity. Rotate to the dominant BPSK axis and test both polarities.
            axis = np.angle(np.sum(symbols * symbols)) / 2.0
            rotated = np.real(symbols * np.exp(-1j * axis))
            decisions = rotated >= 0
            # A conducted SDR path can introduce a small residual DC offset or
            # asymmetric gain. Use the observed symbol midpoint as an
            # additional soft-decision candidate, while retaining zero-threshold
            # decisions for the normal balanced case.
            midpoint = float(np.median(rotated))
            spread = float(np.std(rotated))
            # A burst can contain a residual DC step after the B205 AGC settles.
            # Try a small set of thresholds around the observed midpoint so a
            # handful of biased symbols do not turn into an entire uncorrectable
            # FEC frame.  These are cheap hard-decision candidates; the normal
            # zero-threshold path remains first for maximum throughput.
            threshold_delta = min(0.25, max(0.02, spread * 0.08))
            adaptive = rotated >= midpoint
            adaptive_low = rotated >= midpoint - threshold_delta
            adaptive_high = rotated >= midpoint + threshold_delta
            variants: list[np.ndarray] = []
            for candidate in (
                decisions,
                ~decisions,
                adaptive,
                ~adaptive,
                adaptive_low,
                ~adaptive_low,
                adaptive_high,
                ~adaptive_high,
            ):
                candidate = candidate.astype(np.uint8)
                if not any(np.array_equal(candidate, prior) for prior in variants):
                    variants.append(candidate)
            for bits in variants:
                bipolar = bits.astype(np.int16) * 2 - 1
                # Use the short distinctive sync as an inexpensive shortlist,
                # then score the complete preamble only at those positions. A
                # full 304-bit convolution for every phase/threshold candidate
                # overwhelmed the Pi on live receive buffers.
                sync_bipolar = self._sync_bits.astype(np.int16) * 2 - 1
                sync_correlation = np.convolve(
                    bipolar, sync_bipolar[::-1], mode="valid"
                )
                sync_starts = np.flatnonzero(
                    sync_correlation
                    >= len(self._sync_bits) - 2 * MAX_SYNC_BIT_ERRORS
                )
                preamble_bipolar = self._preamble_bits.astype(np.int16) * 2 - 1
                scored: list[tuple[int, int]] = []
                for sync_start in sync_starts:
                    preamble_start = int(sync_start) - len(self._training_bits)
                    if preamble_start < 0:
                        continue
                    observed = bipolar[
                        preamble_start : int(sync_start) + len(self._sync_bits)
                    ]
                    if len(observed) == len(preamble_bipolar):
                        scored.append(
                            (int(sync_start), int(np.dot(observed, preamble_bipolar)))
                        )
                scored.sort(key=lambda item: item[1], reverse=True)
                full = [
                    item
                    for item in scored
                    if item[1]
                    >= len(self._preamble_bits) - 2 * MAX_PREAMBLE_BIT_ERRORS
                ]
                used_full_preamble = bool(full)
                if used_full_preamble:
                    starts = np.asarray([item[0] for item in full], dtype=np.int64)
                    start_scores = np.asarray([item[1] for item in full], dtype=np.int64)
                else:
                    # A fade can damage training while leaving the distinctive
                    # sync suffix usable. Retain a bounded compatibility fallback
                    # so such a detected-but-bad burst is counted for diagnostics.
                    starts = sync_starts
                    starts = starts[
                        np.argsort(sync_correlation[starts])[::-1]
                    ]
                    start_scores = sync_correlation[starts]
                for start, candidate_score_raw in zip(starts, start_scores, strict=True):
                    candidate_score = float(candidate_score_raw)
                    candidates = [bits]
                    last_consumed: int | None = None
                    # If the global phase estimate was biased by idle samples,
                    # refine it from the known alternating training immediately
                    # before this sync. Keep the original decisions first: a bad
                    # training alignment must never hide a decodable frame.
                    training_bits = self._training_bits
                    training_start = start - len(training_bits)
                    if training_start >= 0:
                        observed = symbols[training_start:start]
                        known = training_bits.astype(np.float32) * 2.0 - 1.0
                        # A threshold-derived bit variant can contain a candidate
                        # at the final correlation position while the symbol view
                        # is shorter after repetition grouping. The original hard
                        # decisions remain valid; skip only training refinement.
                        gain = (
                            np.vdot(known, observed) / len(known)
                            if len(observed) == len(known)
                            else 0.0
                        )
                        if abs(gain) > 1e-6:
                            refined = np.real(
                                symbols * (np.conj(gain) / abs(gain))
                            ) >= 0
                            refined_bits = refined.astype(np.uint8)
                            if not np.array_equal(refined_bits, bits):
                                candidates.append(refined_bits)
                            # Track residual carrier drift from the known
                            # training symbols. Removing the alternating BPSK
                            # pattern first makes adjacent-sample phase change
                            # a direct estimate of frequency error.
                            training_aligned = observed * known
                            drift = 0.0
                            if len(training_aligned) > 1:
                                drift_corr = np.sum(
                                    training_aligned[1:]
                                    * np.conj(training_aligned[:-1])
                                )
                                if drift_corr:
                                    drift = float(np.angle(drift_corr))
                            phase0 = float(np.angle(np.mean(training_aligned)))
                            phase_index = np.arange(len(symbols), dtype=np.float64)
                            phase = phase0 + drift * (phase_index - training_start)
                            tracked = symbols * np.exp(-1j * phase).astype(np.complex64)
                            tracked_bits = np.real(tracked) >= 0
                            tracked_bits = tracked_bits.astype(np.uint8)
                            if not np.array_equal(tracked_bits, bits):
                                candidates.append(tracked_bits)
                    for candidate_bits in candidates:
                        length_start = start + len(self._sync_bits)
                        if len(candidate_bits) < length_start + 16:
                            if used_full_preamble:
                                incomplete_score = max(incomplete_score, candidate_score)
                            continue
                        length = int.from_bytes(
                            np.packbits(
                                candidate_bits[length_start : length_start + 16]
                            ).tobytes(),
                            "big",
                        )
                        if length > fec_encoded_size(self.max_frame, self.interleaver_depth) + self.rs_parity_bytes:
                            continue
                        end = length_start + 16 + length * 8
                        if len(candidate_bits) < end:
                            if used_full_preamble:
                                incomplete_score = max(incomplete_score, candidate_score)
                            continue
                        consumed = int(np.ceil(timing + end * self.bit_repetitions * self.sps))
                        last_consumed = consumed
                        encoded = np.packbits(candidate_bits[length_start + 16 : end]).tobytes()
                        try:
                            if self._rs is not None:
                                encoded = bytes(self._rs.decode(encoded)[0])
                            result = fec_decode(encoded, self.interleaver_depth)
                        except (UncorrectableFECError, ReedSolomonError):
                            continue
                        self.fec_corrected_bits += result.corrected_bits
                        self._last_frame_start_sample = (
                            timing
                            + (int(start) - len(self._training_bits))
                            * self.bit_repetitions
                            * self.sps
                        )
                        return result.payload, consumed
                    # A complete candidate was found but neither phase decision
                    # decoded. Remember its boundary, but test every remaining
                    # timing/alignment candidate before declaring the burst bad.
                    if last_consumed is not None:
                        if candidate_score > failed_score:
                            failed_score = candidate_score
                            failed_consumed = last_consumed
        if failed_consumed is not None and failed_score >= incomplete_score:
            self.fec_uncorrectable_frames += 1
            return None, failed_consumed
        if incomplete_score > float("-inf"):
            return None
        return None


class QPSKModem(BPSKModem):
    """Gray-coded coherent QPSK modem using the proven BPSK burst envelope."""

    # A coarse fourth-power estimate can be biased by the B205's analog-shaped
    # rectangular transitions. The full-preamble phase fit below can recover a
    # rotating candidate, so admit lower correlations while the noise-like
    # training word and candidate cap prevent an unbounded false search.
    _PREAMBLE_THRESHOLD = 0.35
    _MAX_ACQUISITION_CANDIDATES = 8

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._training_bits = np.unpackbits(np.frombuffer(QPSK_TRAINING, dtype=np.uint8))
        self._preamble_bits = np.concatenate((self._training_bits, self._sync_bits))

    @staticmethod
    def _map_bits(bits: np.ndarray) -> np.ndarray:
        pairs = bits.astype(np.uint8).reshape(-1, 2)
        # Gray order around the constellation: 00, 01, 11, 10.
        real = 1.0 - 2.0 * pairs[:, 1].astype(np.float32)
        imag = 1.0 - 2.0 * pairs[:, 0].astype(np.float32)
        return ((real + 1j * imag) / np.sqrt(2.0)).astype(np.complex64)

    def sample_count(self, payload_size: int) -> int:
        base = self.frame_size(payload_size) * 4 * self.sps
        return base + (len(self._rrc_taps) - 1 if self._rrc_taps is not None else 0)

    def modulate(self, payload: bytes) -> np.ndarray:
        if len(payload) > self.max_frame:
            raise ValueError("frame exceeds modem limit")
        encoded = fec_encode(payload, self.interleaver_depth)
        if self._rs is not None:
            encoded = bytes(self._rs.encode(encoded))
        wire = QPSK_TRAINING + SYNC + len(encoded).to_bytes(2, "big") + encoded
        bits = np.unpackbits(np.frombuffer(wire, dtype=np.uint8))
        if self.bit_repetitions > 1:
            bits = np.repeat(bits, self.bit_repetitions)
        symbols = self._map_bits(bits)
        if self._rrc_taps is None:
            samples = np.repeat(symbols, self.sps).astype(np.complex64)
        else:
            impulses = np.zeros(len(symbols) * self.sps, dtype=np.complex64)
            impulses[:: self.sps] = symbols
            samples = np.convolve(impulses, self._rrc_taps, mode="full").astype(np.complex64)
            peak = float(np.max(np.abs(samples)))
            if peak:
                samples /= peak
        if self.carrier_offset_hz:
            phase = 2 * np.pi * self.carrier_offset_hz * np.arange(len(samples)) / self.sample_rate
            samples *= np.exp(1j * phase).astype(np.complex64)
        return samples

    @staticmethod
    def _decision_directed_track(symbols: np.ndarray, start: int) -> np.ndarray:
        """Track residual payload phase/frequency after open-loop acquisition."""
        tracked = symbols.copy()
        phase = 0.0
        frequency = 0.0
        for index in range(max(0, start), len(tracked)):
            value = tracked[index] * np.exp(-1j * phase)
            reference = complex(
                1.0 if value.real >= 0 else -1.0,
                1.0 if value.imag >= 0 else -1.0,
            ) / np.sqrt(2.0)
            error = float(np.angle(value * np.conj(reference)))
            frequency += 0.002 * error
            phase += frequency + 0.08 * error
            tracked[index] = value
        return tracked

    def _find_frame(self) -> tuple[bytes | None, int] | None:
        preamble_symbols = self._map_bits(self._preamble_bits)
        minimum_symbols = len(preamble_symbols) + 8
        if len(self._samples) < minimum_symbols * self.sps:
            return None

        # Fourth power removes QPSK data and resolves coarse carrier frequency.
        fourth = self._samples**4
        # Compare points one symbol apart. Adjacent shaped samples include the
        # deterministic RRC transition trajectory, which biases the fourth-power
        # estimate even when carrier offset is zero.
        frequency_correlation = np.sum(
            fourth[self.sps :] * np.conj(fourth[: -self.sps])
        )
        phase_step = (
            np.angle(frequency_correlation) / (4.0 * self.sps)
            if frequency_correlation
            else 0.0
        )
        sample_index = np.arange(len(self._samples), dtype=np.float64)
        corrected = self._samples * np.exp(-1j * phase_step * sample_index)
        matched = (
            np.convolve(corrected, self._rrc_taps, mode="same")
            if self._rrc_taps is not None
            else corrected
        )

        failed: tuple[float, float, int] | None = None
        incomplete_score = float("-inf")
        for timing in range(self.sps):
            usable = (len(matched) - timing) // self.sps
            if usable < minimum_symbols:
                continue
            if self._rrc_taps is None:
                block = matched[timing : timing + usable * self.sps]
                symbols = block.reshape(-1, self.sps).mean(axis=1).astype(np.complex64)
            else:
                symbols = matched[timing : timing + usable * self.sps : self.sps].astype(
                    np.complex64
                )
            correlation = np.correlate(symbols, preamble_symbols, mode="valid")
            energy = np.convolve(
                np.abs(symbols) ** 2,
                np.ones(len(preamble_symbols), dtype=np.float32),
                mode="valid",
            )
            denominator = np.sqrt(energy * float(np.vdot(preamble_symbols, preamble_symbols).real))
            scores = np.divide(
                np.abs(correlation),
                denominator,
                out=np.zeros_like(denominator, dtype=np.float32),
                where=denominator > 1e-9,
            )
            starts = np.flatnonzero(scores >= self._PREAMBLE_THRESHOLD)
            starts = starts[np.argsort(scores[starts])[::-1]]
            starts = starts[: self._MAX_ACQUISITION_CANDIDATES]
            for start_raw in starts:
                start = int(start_raw)
                score = float(scores[start])
                observed = symbols[start : start + len(preamble_symbols)]
                aligned = observed * np.conj(preamble_symbols)
                aligned_index = np.arange(len(aligned), dtype=np.float64)
                aligned_phase = np.unwrap(np.angle(aligned)).astype(np.float64)
                weights = np.abs(aligned).astype(np.float64) ** 2
                weight_sum = float(np.sum(weights))
                drift = 0.0
                phase0 = float(np.angle(np.mean(aligned)))
                if weight_sum > 1e-12 and len(aligned) > 1:
                    index_mean = float(np.sum(weights * aligned_index) / weight_sum)
                    phase_mean = float(np.sum(weights * aligned_phase) / weight_sum)
                    centered = aligned_index - index_mean
                    denominator = float(np.sum(weights * centered * centered))
                    if denominator > 1e-12:
                        drift = float(
                            np.sum(weights * centered * (aligned_phase - phase_mean))
                            / denominator
                        )
                        phase0 = phase_mean - drift * index_mean
                symbol_index = np.arange(len(symbols), dtype=np.float64)
                phase = phase0 + drift * (symbol_index - start)
                decided = symbols * np.exp(-1j * phase).astype(np.complex64)
                corrected_preamble = decided[start : start + len(preamble_symbols)]
                corrected_energy = float(np.sum(np.abs(corrected_preamble) ** 2))
                corrected_score = (
                    abs(np.vdot(preamble_symbols, corrected_preamble))
                    / np.sqrt(corrected_energy * len(preamble_symbols))
                    if corrected_energy > 1e-12
                    else 0.0
                )
                # Reject startup impulses and other false coarse candidates
                # before treating their random length/data decisions as an FEC
                # failure. Real B205 bursts from the permissive coarse search
                # become strongly coherent after the full-preamble phase fit.
                if corrected_score < 0.8:
                    continue
                data_symbol_start = start + len(preamble_symbols)
                candidates = [
                    decided,
                    self._decision_directed_track(decided, data_symbol_start),
                ]
                for candidate in candidates:
                    bits = np.empty(len(candidate) * 2, dtype=np.uint8)
                    bits[0::2] = np.imag(candidate) < 0
                    bits[1::2] = np.real(candidate) < 0
                    data_bit_start = data_symbol_start * 2
                    if len(bits) < data_bit_start + 16:
                        incomplete_score = max(incomplete_score, score)
                        continue
                    length = int.from_bytes(
                        np.packbits(bits[data_bit_start : data_bit_start + 16]).tobytes(),
                        "big",
                    )
                    maximum = (
                        fec_encoded_size(self.max_frame, self.interleaver_depth)
                        + self.rs_parity_bytes
                    )
                    if length > maximum:
                        continue
                    end_bit = data_bit_start + 16 + length * 8
                    if len(bits) < end_bit:
                        incomplete_score = max(incomplete_score, score)
                        continue
                    end_symbol = (end_bit + 1) // 2
                    consumed = int(np.ceil(timing + end_symbol * self.sps))
                    encoded = np.packbits(bits[data_bit_start + 16 : end_bit]).tobytes()
                    try:
                        if self._rs is not None:
                            encoded = bytes(self._rs.decode(encoded)[0])
                        result = fec_decode(encoded, self.interleaver_depth)
                    except (UncorrectableFECError, ReedSolomonError):
                        if failed is None or corrected_score > failed[0]:
                            failed = (float(corrected_score), score, consumed)
                        continue
                    self.fec_corrected_bits += result.corrected_bits
                    self._last_frame_start_sample = timing + start * self.sps
                    return result.payload, consumed
        if failed is not None and failed[1] >= incomplete_score:
            self.fec_uncorrectable_frames += 1
            self.last_fec_failure_corrected_score = failed[0]
            self.last_fec_failure_score = failed[1]
            return None, failed[2]
        return None
