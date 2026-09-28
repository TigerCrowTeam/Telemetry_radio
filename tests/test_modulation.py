import numpy as np

from telemetry_radio.fec import encode as fec_encode
from telemetry_radio.modulation import SYNC, TRAINING, BPSKModem, QPSKModem


def test_bpsk_loopback():
    tx = BPSKModem(samples_per_symbol=4)
    rx = BPSKModem(samples_per_symbol=4)
    payload = b"radio frame"
    assert list(rx.feed(tx.modulate(payload))) == [payload]


def test_qpsk_reports_preamble_hardware_arrival_time():
    modem = QPSKModem(8, sample_rate=2_000_000)
    leading = np.zeros(13, dtype=np.complex64)
    start_ns = 9_000_000_000
    decoded = modem.feed_timed(
        np.concatenate((leading, modem.modulate(b"timestamped"))), start_ns
    )
    assert decoded[0][0] == b"timestamped"
    assert abs(decoded[0][1] - (start_ns + int(13 / 2_000_000 * 1e9))) <= int(
        modem.sps / modem.sample_rate * 1e9
    )


def test_qpsk_decision_directed_tracking_recovers_payload_phase_curvature():
    payload = b"curved carrier" * 16
    transmitter = QPSKModem(8, sample_rate=2_000_000)
    samples = transmitter.modulate(payload)
    index = np.arange(len(samples), dtype=np.float64)
    impaired = samples * np.exp(1j * 1e-7 * index * index)

    assert list(QPSKModem(8, sample_rate=2_000_000).feed(impaired)) == [payload]


def test_bpsk_accepts_arbitrarily_split_sample_reads():
    samples = BPSKModem(4).modulate(b"split")
    rx = BPSKModem(4)
    assert list(rx.feed(samples[:7])) == []
    assert list(rx.feed(samples[7:])) == [b"split"]


def test_bpsk_searches_all_integer_sample_phases():
    samples = BPSKModem(8).modulate(b"sample phase")
    leading = np.zeros(5, dtype=np.complex64)
    assert list(BPSKModem(8).feed(np.concatenate((leading, samples)))) == [
        b"sample phase"
    ]


def test_bpsk_does_not_discard_valid_burst_after_bad_candidate():
    modem = BPSKModem(8)
    bad = modem.modulate(b"bad candidate")
    encoded_start = (len(TRAINING) + len(SYNC) + 2) * 8
    # Destroy enough encoded decisions to exceed the interleaved SECDED budget.
    for bit in range(encoded_start, encoded_start + 256):
        bad[bit * modem.sps : (bit + 1) * modem.sps] *= -1
    good = modem.modulate(b"valid candidate")

    receiver = BPSKModem(8)
    assert list(receiver.feed(np.concatenate((bad, good)))) == [b"valid candidate"]


def test_bpsk_recovers_carrier_offset_and_noise():
    samples = BPSKModem(8).modulate(b"impaired link")
    sample_rate = 1_000_000
    offset_hz = 750
    phase = 2 * np.pi * offset_hz * np.arange(len(samples)) / sample_rate
    rng = np.random.default_rng(4)
    noise = rng.normal(0, 0.08, len(samples)) + 1j * rng.normal(0, 0.08, len(samples))
    impaired = samples * np.exp(1j * phase) + noise
    assert list(BPSKModem(8).feed(impaired.astype(np.complex64))) == [b"impaired link"]


def test_rrc_shaped_bpsk_round_trip_with_timing_phase_and_noise():
    tx = BPSKModem(8, rrc_rolloff=0.35, rrc_span_symbols=8)
    samples = tx.modulate(b"matched filter")
    rng = np.random.default_rng(21)
    noise = rng.normal(0, 0.02, len(samples)) + 1j * rng.normal(0, 0.02, len(samples))
    leading = np.zeros(3, dtype=np.complex64)
    impaired = np.concatenate((leading, samples + noise.astype(np.complex64)))
    rx = BPSKModem(8, rrc_rolloff=0.35, rrc_span_symbols=8)
    assert list(rx.feed(impaired)) == [b"matched filter"]
    assert len(samples) == tx.sample_count(len(b"matched filter"))


def test_bpsk_discards_idle_history_and_decodes_later_burst():
    rng = np.random.default_rng(12)
    receiver = BPSKModem(8)
    for _ in range(100):
        noise = (
            rng.normal(0, 0.001, 4096) + 1j * rng.normal(0, 0.001, 4096)
        ).astype(np.complex64)
        assert list(receiver.feed(noise)) == []

    payload = b"hardware burst"
    burst = BPSKModem(8).modulate(payload) * 0.03
    frames = []
    for start in range(0, len(burst), 4096):
        frames.extend(receiver.feed(burst[start : start + 4096]))

    assert frames == [payload]
    assert len(receiver._samples) < 20_000


def test_bpsk_fec_corrects_payload_bit_error():
    modem = BPSKModem(4)
    samples = modem.modulate(b"correct me")
    # Skip the unencoded sync and length, then invert one encoded payload bit.
    bit = (len(TRAINING) + len(SYNC) + 2) * 8 + 3
    samples[bit * 4 : (bit + 1) * 4] *= -1
    receiver = BPSKModem(4)
    assert list(receiver.feed(samples)) == [b"correct me"]
    assert receiver.fec_corrected_bits == 1


def test_reed_solomon_parity_corrects_multiple_encoded_byte_errors():
    modem = BPSKModem(8, rs_parity_bytes=16)
    samples = modem.modulate(b"outer code repairs byte errors")
    encoded_start = (len(TRAINING) + len(SYNC) + 2) * 8
    for byte_offset in (2, 9, 17, 25):
        bit = encoded_start + byte_offset * 8
        samples[bit * modem.sps : (bit + 1) * modem.sps] *= -1
    receiver = BPSKModem(8, rs_parity_bytes=16)
    assert list(receiver.feed(samples)) == [b"outer code repairs byte errors"]


def test_bpsk_bit_repetition_round_trip():
    tx = BPSKModem(samples_per_symbol=4, bit_repetitions=3)
    rx = BPSKModem(samples_per_symbol=4, bit_repetitions=3)
    samples = tx.modulate(b"repeated modem frame")
    assert list(rx.feed(samples)) == [b"repeated modem frame"]


def test_bpsk_deep_interleaver_round_trip_and_burst_correction():
    tx = BPSKModem(samples_per_symbol=4, interleaver_depth=64)
    samples = tx.modulate(b"deep interleaver")
    # A short contiguous symbol fade becomes a burst of hard decisions. The
    # deeper interleaver distributes it over separate SECDED codewords.
    start = (len(TRAINING) + len(SYNC) + 2) * 8 * 4 + 8 * 4
    samples[start : start + 64 * 4] *= -1
    rx = BPSKModem(samples_per_symbol=4, interleaver_depth=64)
    assert list(rx.feed(samples)) == [b"deep interleaver"]


def test_bpsk_tolerates_sync_bit_errors():
    modem = BPSKModem(4)
    samples = modem.modulate(b"sync survives")
    sync_start = len(TRAINING) * 8
    for offset in (2, 11, 27, 35):
        bit = sync_start + offset
        samples[bit * 4 : (bit + 1) * 4] *= -1

    assert list(BPSKModem(4).feed(samples)) == [b"sync survives"]


def test_modem_size_helpers_include_fec_padding_preamble_and_length():
    modem = BPSKModem(4)
    payload_size = 37
    expected_bytes = len(TRAINING) + len(SYNC) + 2 + len(fec_encode(bytes(payload_size)))
    assert modem.frame_size(payload_size) == expected_bytes
    assert modem.sample_count(payload_size) == expected_bytes * 8 * 4
    assert len(modem.modulate(bytes(payload_size))) == modem.sample_count(payload_size)


def test_qpsk_loopback_uses_half_the_bpsk_samples():
    payload = b"two bits per symbol"
    tx = QPSKModem(8)
    samples = tx.modulate(payload)

    assert list(QPSKModem(8).feed(samples)) == [payload]
    assert len(samples) == tx.sample_count(len(payload))
    assert len(samples) * 2 == BPSKModem(8).sample_count(len(payload))


def test_qpsk_accepts_split_reads_and_sample_phase():
    samples = QPSKModem(8).modulate(b"split qpsk")
    receiver = QPSKModem(8)

    assert list(receiver.feed(np.concatenate((np.zeros(5, np.complex64), samples[:97])))) == []
    assert list(receiver.feed(samples[97:])) == [b"split qpsk"]


def test_qpsk_resolves_phase_ambiguity_carrier_offset_and_noise():
    payload = b"impaired qpsk link"
    samples = QPSKModem(8).modulate(payload)
    sample_rate = 1_000_000
    phase = 1.31 + 2 * np.pi * 750 * np.arange(len(samples)) / sample_rate
    rng = np.random.default_rng(44)
    noise = rng.normal(0, 0.06, len(samples)) + 1j * rng.normal(0, 0.06, len(samples))
    impaired = samples * np.exp(1j * phase) + noise

    assert list(QPSKModem(8).feed(impaired.astype(np.complex64))) == [payload]


def test_qpsk_reed_solomon_repairs_multiple_encoded_byte_errors():
    payload = b"qpsk outer code repairs byte errors"
    modem = QPSKModem(8, rs_parity_bytes=16)
    samples = modem.modulate(payload)
    encoded_symbol_start = (len(TRAINING) + len(SYNC) + 2) * 4
    for byte_offset in (2, 9, 17, 25):
        symbol = encoded_symbol_start + byte_offset * 4
        samples[symbol * modem.sps : (symbol + 1) * modem.sps] *= -1

    receiver = QPSKModem(8, rs_parity_bytes=16)
    assert list(receiver.feed(samples)) == [payload]


def test_rrc_shaped_qpsk_round_trip_with_timing_phase_and_noise():
    payload = b"shaped qpsk"
    tx = QPSKModem(8, rrc_rolloff=0.35, rrc_span_symbols=8)
    samples = tx.modulate(payload)
    rng = np.random.default_rng(45)
    noise = rng.normal(0, 0.015, len(samples)) + 1j * rng.normal(0, 0.015, len(samples))
    impaired = np.concatenate((np.zeros(3, np.complex64), samples + noise))

    receiver = QPSKModem(8, rrc_rolloff=0.35, rrc_span_symbols=8)
    assert list(receiver.feed(impaired.astype(np.complex64))) == [payload]
