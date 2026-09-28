from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class ClockEstimate:
    offset_ns: float
    drift_ppm: float
    samples: int


class HardwareClockSynchronizer:
    """Robust affine mapping from master hardware time to local hardware time."""

    def __init__(self, window: int = 32, smoothing: float = 0.25):
        if window < 2 or not 0 < smoothing <= 1:
            raise ValueError("invalid hardware clock estimator parameters")
        self._observations: deque[tuple[int, int]] = deque(maxlen=window)
        self._smoothing = smoothing
        self._offset_ns: float | None = None
        self._slope = 1.0
        self._origin_master_ns = 0

    @property
    def ready(self) -> bool:
        return len(self._observations) >= 2

    def observe(self, master_tx_ns: int, local_arrival_ns: int) -> ClockEstimate:
        self._observations.append((int(master_tx_ns), int(local_arrival_ns)))
        master_values = [item[0] for item in self._observations]
        local_values = [item[1] for item in self._observations]
        self._origin_master_ns = master_values[-1]
        origin_local_ns = local_values[-1]
        centered = np.asarray(
            [value - self._origin_master_ns for value in master_values], dtype=np.float64
        )
        local_centered = np.asarray(
            [value - origin_local_ns for value in local_values], dtype=np.float64
        )
        if len(master_values) >= 2 and float(np.ptp(centered)) > 0:
            slope, intercept_delta = np.polyfit(centered, local_centered, 1)
            offset = float(origin_local_ns - self._origin_master_ns) + float(
                intercept_delta
            )
        else:
            slope = 1.0
            offset = float(origin_local_ns - self._origin_master_ns)
        slope = float(np.clip(slope, 1.0 - 200e-6, 1.0 + 200e-6))
        if self._offset_ns is None:
            self._offset_ns = offset
            self._slope = slope
        else:
            alpha = self._smoothing
            self._offset_ns += alpha * (offset - self._offset_ns)
            self._slope += alpha * (slope - self._slope)
        return self.estimate()

    def master_to_local(self, master_time_ns: int) -> int:
        if self._offset_ns is None:
            raise RuntimeError("hardware clock synchronizer has no observations")
        delta = int(master_time_ns) - self._origin_master_ns
        return int(round(self._origin_master_ns + self._offset_ns + self._slope * delta))

    def local_to_master(self, local_time_ns: int) -> int:
        if self._offset_ns is None:
            raise RuntimeError("hardware clock synchronizer has no observations")
        delta = int(local_time_ns) - (self._origin_master_ns + self._offset_ns)
        return int(round(self._origin_master_ns + delta / self._slope))

    def estimate(self) -> ClockEstimate:
        return ClockEstimate(
            offset_ns=0.0 if self._offset_ns is None else self._offset_ns,
            drift_ppm=(self._slope - 1.0) * 1e6,
            samples=len(self._observations),
        )
