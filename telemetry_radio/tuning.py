from __future__ import annotations

import queue
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class TuneCommand:
    slot: int
    frequency_hz: float
    time_ns: int


class TimedTuneWorker:
    """Prepare future hardware-timed LO changes without blocking the app loop."""

    def __init__(self, radio):
        self.radio = radio
        self._commands: queue.Queue[TuneCommand | None] = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._completed: set[int] = set()
        self._errors: dict[int, BaseException] = {}
        self._thread = threading.Thread(target=self._run, name="timed-tune", daemon=True)
        self._thread.start()

    def submit(self, command: TuneCommand) -> bool:
        try:
            self._commands.put_nowait(command)
            return True
        except queue.Full:
            return False

    def completed(self, slot: int) -> bool:
        with self._lock:
            return slot in self._completed

    def error(self, slot: int) -> BaseException | None:
        with self._lock:
            return self._errors.get(slot)

    def _run(self) -> None:
        while True:
            command = self._commands.get()
            if command is None:
                return
            try:
                self.radio.tune_at(command.frequency_hz, command.time_ns)
                with self._lock:
                    self._completed.add(command.slot)
            except BaseException as exc:
                with self._lock:
                    self._errors[command.slot] = exc

    def close(self) -> None:
        try:
            self._commands.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(timeout=1.0)
