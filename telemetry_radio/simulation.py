from __future__ import annotations

import random
import threading
from collections import deque
from dataclasses import dataclass

import numpy as np

from .app import TelemetryRadioApp
from .config import RadioConfig


class SimulatedClock:
    def __init__(self, start_time: float = 1_800_000_000.0):
        self.wall = start_time
        self.monotonic = 0.0

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("simulation time cannot move backward")
        self.wall += seconds
        self.monotonic += seconds


class OffsetClock:
    def __init__(self, clock: SimulatedClock, offset: float = 0.0):
        self.clock = clock
        self.offset = offset

    def wall_time(self) -> float:
        return self.clock.wall + self.offset

    def monotonic_time(self) -> float:
        return self.clock.monotonic


class SimulatedChannel:
    """In-memory RF channel with impairments, collisions, capture, and delay."""

    def __init__(
        self,
        *,
        packet_loss: float = 0.0,
        noise_stddev: float = 0.0,
        frequency_offset_hz: float = 0.0,
        sample_rate: float = 1_000_000,
        random_delay_cycles: tuple[int, int] = (0, 0),
        capture_ratio_db: float = 10.0,
        carrier_sense_threshold: float = 0.02,
        max_transmissions_per_cycle: int = 1024,
        corruption_rate: float = 0.0,
        corruption_burst_samples: int = 16,
        slotted: bool = False,
        seed: int | None = None,
    ):
        self.packet_loss = packet_loss
        self.noise_stddev = noise_stddev
        self.frequency_offset_hz = frequency_offset_hz
        self.sample_rate = sample_rate
        if random_delay_cycles[0] < 0 or random_delay_cycles[1] < random_delay_cycles[0]:
            raise ValueError("invalid random_delay_cycles")
        self.random_delay_cycles = random_delay_cycles
        self.capture_ratio = 10 ** (capture_ratio_db / 10.0)
        self.carrier_sense_threshold = carrier_sense_threshold
        if max_transmissions_per_cycle < 1:
            raise ValueError("max_transmissions_per_cycle must be positive")
        self.max_transmissions_per_cycle = max_transmissions_per_cycle
        if not 0 <= corruption_rate <= 1:
            raise ValueError("corruption_rate must be between zero and one")
        if corruption_burst_samples < 1:
            raise ValueError("corruption_burst_samples must be positive")
        self.corruption_rate = corruption_rate
        self.corruption_burst_samples = corruption_burst_samples
        self.slotted = slotted
        self.random = random.Random(seed)
        self.radios: list[SimulatedRadio] = []
        self.lock = threading.Lock()
        self.pending: list[tuple[SimulatedRadio, np.ndarray]] = []
        self.delayed: list[tuple[int, SimulatedRadio, np.ndarray]] = []
        self.path_gains: dict[tuple[int, int], float] = {}
        self.cycle = 0
        self.stats = {
            "transmissions": 0,
            "deliveries": 0,
            "collisions": 0,
            "captures": 0,
            "dropped": 0,
            "delayed": 0,
            "congestion_drops": 0,
            "corrupted": 0,
        }

    def radio(self) -> "SimulatedRadio":
        with self.lock:
            radio = SimulatedRadio(self, len(self.radios))
            self.radios.append(radio)
        return radio

    def set_path_gain(
        self, sender: "SimulatedRadio", receiver: "SimulatedRadio", gain: float
    ) -> None:
        """Set linear received-power gain; zero makes a transmitter inaudible."""
        if gain < 0:
            raise ValueError("path gain cannot be negative")
        self.path_gains[(sender.index, receiver.index)] = gain

    def path_gain(self, sender: "SimulatedRadio", receiver: "SimulatedRadio") -> float:
        return self.path_gains.get((sender.index, receiver.index), 1.0)

    def send(self, sender: "SimulatedRadio", samples: np.ndarray) -> None:
        samples = np.asarray(samples, dtype=np.complex64).copy()
        with self.lock:
            self.stats["transmissions"] += 1
            if self.slotted:
                if len(self.pending) >= self.max_transmissions_per_cycle:
                    self.stats["congestion_drops"] += 1
                    self.stats["dropped"] += 1
                    return
                self.pending.append((sender, samples))
                return
        self._resolve([(sender, samples)])

    def sensed_power(self, receiver: "SimulatedRadio") -> float:
        """Power from current-cycle transmissions audible to this receiver."""
        power = 0.0
        with self.lock:
            pending = list(self.pending)
        for sender, samples in pending:
            if (
                sender is receiver
                or not sender.connected
                or not receiver.connected
                or sender.frequency_hz != receiver.frequency_hz
                or not len(samples)
            ):
                continue
            power += float(np.mean(np.abs(samples) ** 2)) * self.path_gain(sender, receiver)
        return power

    def advance(self) -> None:
        """Resolve one RF cycle, then release deliveries whose delay has elapsed."""
        with self.lock:
            pending, self.pending = self.pending, []
        self._resolve(pending)
        with self.lock:
            due = [item for item in self.delayed if item[0] <= self.cycle]
            self.delayed = [item for item in self.delayed if item[0] > self.cycle]
            for _, receiver, samples in due:
                if receiver.connected:
                    receiver.incoming.append(samples)
                    self.stats["deliveries"] += 1
                else:
                    self.stats["dropped"] += 1
            self.cycle += 1

    def _impaired(self, samples: np.ndarray) -> np.ndarray:
        if self.random.random() < self.packet_loss:
            self.stats["dropped"] += 1
            return np.empty(0, dtype=np.complex64)
        output = samples.copy()
        if len(output) and self.random.random() < self.corruption_rate:
            start = len(output) // 2
            width = min(self.corruption_burst_samples, len(output) - start)
            output[start : start + width] *= -1
            self.stats["corrupted"] += 1
        if self.frequency_offset_hz:
            phase = 2 * np.pi * self.frequency_offset_hz * np.arange(len(output)) / self.sample_rate
            output *= np.exp(1j * phase).astype(np.complex64)
        if self.noise_stddev:
            generator = np.random.default_rng(self.random.randrange(2**32))
            noise = generator.normal(0, self.noise_stddev, len(output)) + 1j * generator.normal(
                0, self.noise_stddev, len(output)
            )
            output += noise.astype(np.complex64)
        return output

    def _deliver(self, receiver: "SimulatedRadio", samples: np.ndarray) -> None:
        output = self._impaired(samples)
        if not len(output):
            return
        delay = self.random.randint(*self.random_delay_cycles)
        with self.lock:
            if delay:
                self.stats["delayed"] += 1
                self.delayed.append((self.cycle + delay, receiver, output))
            else:
                receiver.incoming.append(output)
                self.stats["deliveries"] += 1

    def _resolve(self, transmissions: list[tuple["SimulatedRadio", np.ndarray]]) -> None:
        if not transmissions:
            return
        for receiver in self.radios:
            audible: list[tuple[float, np.ndarray]] = []
            for sender, samples in transmissions:
                gain = self.path_gain(sender, receiver)
                if (
                    sender is receiver
                    or not sender.connected
                    or not receiver.connected
                    or sender.frequency_hz != receiver.frequency_hz
                    or gain <= 0
                    or not len(samples)
                ):
                    continue
                power = float(np.mean(np.abs(samples) ** 2)) * gain
                if power >= self.carrier_sense_threshold:
                    audible.append((power, samples * np.sqrt(gain)))
            if not audible:
                continue
            audible.sort(key=lambda item: item[0], reverse=True)
            if len(audible) == 1:
                self._deliver(receiver, audible[0][1])
                continue
            strongest, second = audible[0][0], audible[1][0]
            if second == 0 or strongest / second >= self.capture_ratio:
                self.stats["captures"] += 1
                self._deliver(receiver, audible[0][1])
            else:
                # Overlap the bursts in sample time. The resulting waveform may
                # demodulate but should fail its packet CRC, as on a real collision.
                self.stats["collisions"] += 1
                length = max(len(item[1]) for item in audible)
                collision = np.zeros(length, dtype=np.complex64)
                for _, signal in audible:
                    collision[: len(signal)] += signal
                self._deliver(receiver, collision)


class SimulatedRadio:
    def __init__(self, channel: SimulatedChannel, index: int):
        self.channel = channel
        self.index = index
        self.frequency_hz: float | None = None
        self.incoming: deque[np.ndarray] = deque()
        self.connected = True
        self.drop_next_transmissions = 0

    def tune(self, frequency_hz: float) -> None:
        self.frequency_hz = frequency_hz

    def transmit(self, samples: np.ndarray) -> None:
        if not self.connected:
            return
        if self.drop_next_transmissions:
            self.drop_next_transmissions -= 1
            self.channel.stats["dropped"] += 1
            return
        self.channel.send(self, samples)

    def receive(self, count: int = 4096, timeout_us: int = 50_000) -> np.ndarray:
        if not self.connected:
            return np.empty(0, dtype=np.complex64)
        if not self.incoming:
            return np.empty(0, dtype=np.complex64)
        samples = self.incoming.popleft()
        if len(samples) > count:
            self.incoming.appendleft(samples[count:])
            return samples[:count]
        return samples

    def channel_power(self) -> float:
        if not self.connected:
            return 0.0
        return self.channel.sensed_power(self)

    def disconnect(self) -> None:
        self.connected = False
        self.incoming.clear()

    def reconnect(self) -> None:
        self.connected = True

    def drop_next(self, count: int = 1) -> None:
        self.drop_next_transmissions += count

    def close(self) -> None:
        self.incoming.clear()


class VirtualMavlink:
    """Queue-backed MAVLink endpoint preserving each injected wire frame."""

    def __init__(self):
        self.outbound: deque[bytes] = deque()
        self.inbound: deque[bytes] = deque()
        self.closed = False

    def inject(self, frame: bytes) -> None:
        """Inject a frame as though it came from QGC or an attached autopilot."""
        self.outbound.append(bytes(frame))

    def receive(self, timeout: float = 0.05) -> bytes | None:
        return self.outbound.popleft() if self.outbound else None

    def send(self, frame: bytes) -> None:
        """Receive a frame delivered by the radio application."""
        self.inbound.append(bytes(frame))

    def close(self) -> None:
        self.closed = True


@dataclass
class SimulatedNode:
    name: str
    app: TelemetryRadioApp
    mavlink: VirtualMavlink
    radio: SimulatedRadio
    clock: OffsetClock


class SimulatedNetwork:
    """End-to-end network running real node logic over an in-memory RF channel."""

    def __init__(
        self,
        configs: dict[str, RadioConfig],
        *,
        channel: SimulatedChannel | None = None,
        clock: SimulatedClock | None = None,
        clock_offsets: dict[str, float] | None = None,
        cycle_seconds: float = 0.01,
    ):
        self.channel = channel or SimulatedChannel(slotted=True)
        self.channel.slotted = True
        self.clock = clock or SimulatedClock()
        self.clock_offsets = clock_offsets or {}
        self.cycle_seconds = cycle_seconds
        self.nodes: dict[str, SimulatedNode] = {}
        node_ids: set[int] = set()
        for name, config in configs.items():
            if config.node_id in node_ids:
                raise ValueError(f"duplicate simulated node ID: {config.node_id}")
            node_ids.add(config.node_id)
            radio = self.channel.radio()
            mavlink = VirtualMavlink()
            node_clock = OffsetClock(self.clock, self.clock_offsets.get(name, 0.0))
            app = TelemetryRadioApp(
                config,
                sdr=radio,
                mavlink=mavlink,
                wall_clock=node_clock.wall_time,
                monotonic_clock=node_clock.monotonic_time,
            )
            self.nodes[name] = SimulatedNode(name, app, mavlink, radio, node_clock)

    def step(self, cycles: int = 1) -> None:
        """Advance all nodes and resolve transmissions that overlap each cycle."""
        for _ in range(cycles):
            values = list(self.nodes.values())
            # Rotate first access to avoid permanently favoring one node's CCA.
            offset = self.channel.cycle % len(values) if values else 0
            for node in values[offset:] + values[:offset]:
                node.app.step()
            self.channel.advance()
            self.clock.advance(self.cycle_seconds)

    def advance_time(self, seconds: float) -> None:
        self.clock.advance(seconds)

    def disconnect(self, node: str) -> None:
        self.nodes[node].radio.disconnect()

    def reconnect(self, node: str) -> None:
        self.nodes[node].radio.reconnect()

    def inject(self, node: str, frame: bytes) -> None:
        self.nodes[node].mavlink.inject(frame)

    def received(self, node: str) -> list[bytes]:
        return list(self.nodes[node].mavlink.inbound)

    def close(self) -> None:
        for node in self.nodes.values():
            node.mavlink.close()
            node.radio.close()
