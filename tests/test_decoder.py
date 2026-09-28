import time
from collections import deque
import numpy as np

from telemetry_radio.decoder import DecoderWorker
from telemetry_radio.sdr import SampleBlock


class Radio:
    def __init__(self, blocks):
        self.blocks = deque(blocks)

    def receive_timed(self, timeout_us=5_000):
        if self.blocks:
            return self.blocks.popleft()
        time.sleep(timeout_us / 1_000_000)
        return SampleBlock(np.empty(0, dtype=np.complex64))


class Modem:
    fec_corrected_bits = 0
    fec_uncorrectable_frames = 0
    last_fec_failure_score = None
    last_fec_failure_corrected_score = None

    def feed_timed(self, samples, time_ns):
        return [(bytes([len(samples)]), time_ns)]


def wait_for_batch(worker):
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        batch = worker.receive(timeout=0.01)
        if batch is not None:
            return batch
    raise AssertionError("decoder worker did not produce a batch")


def test_decoder_worker_preserves_timestamps_and_reports_work():
    worker = DecoderWorker(
        Radio([SampleBlock(np.ones(4, np.complex64), 1_000)]),
        Modem(),
        sample_rate=1_000_000,
    )
    try:
        batch = wait_for_batch(worker)
        diagnostics = worker.diagnostics()
    finally:
        worker.close()

    assert batch.frames == [(b"\x04", 1_000)]
    assert diagnostics["decoder_iq_blocks"] == 1
    assert diagnostics["decoder_iq_samples"] == 4
    assert diagnostics["decoder_frames"] == 1


def test_decoder_worker_counts_hardware_timestamp_discontinuity():
    worker = DecoderWorker(
        Radio(
            [
                SampleBlock(np.ones(4, np.complex64), 1_000),
                SampleBlock(np.ones(4, np.complex64), 20_000),
            ]
        ),
        Modem(),
        sample_rate=1_000_000,
    )
    try:
        wait_for_batch(worker)
        wait_for_batch(worker)
        diagnostics = worker.diagnostics()
    finally:
        worker.close()

    assert diagnostics["decoder_timestamp_discontinuities"] == 1
