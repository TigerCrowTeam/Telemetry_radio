import time

from telemetry_radio.tuning import TimedTuneWorker, TuneCommand


class Radio:
    def __init__(self):
        self.calls = []

    def tune_at(self, frequency_hz, time_ns):
        self.calls.append((frequency_hz, time_ns))


def test_timed_tune_worker_prepares_command_off_thread():
    radio = Radio()
    worker = TimedTuneWorker(radio)
    try:
        assert worker.submit(TuneCommand(7, 940e6, 123456))
        deadline = time.monotonic() + 1
        while not worker.completed(7) and time.monotonic() < deadline:
            time.sleep(0.001)
        assert worker.completed(7)
        assert worker.error(7) is None
        assert radio.calls == [(940e6, 123456)]
    finally:
        worker.close()
