from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ChannelEvidence:
    visits: int = 0
    mavlink_frames: int = 0


@dataclass
class DemoEvidence:
    frequencies_hz: tuple[int, ...]
    stale_seconds: float = 5.0
    channels: dict[int, ChannelEvidence] = field(init=False)
    current_frequency_hz: int | None = None
    hops_observed: int = 0
    previous_mavlink_frames: int | None = None
    last_delivery_at: float | None = None

    def __post_init__(self) -> None:
        self.channels = {frequency: ChannelEvidence() for frequency in self.frequencies_hz}

    def observe(self, snapshot: dict[str, Any], now: float) -> None:
        value = snapshot.get("active_frequency_hz")
        frequency = int(value) if value is not None else None
        if frequency != self.current_frequency_hz and frequency is not None:
            if self.current_frequency_hz is not None:
                self.hops_observed += 1
            self.current_frequency_hz = frequency
            self.channels.setdefault(frequency, ChannelEvidence()).visits += 1

        delivered = int(snapshot.get("mavlink_frames_to_local", 0))
        if self.previous_mavlink_frames is not None:
            delta = max(0, delivered - self.previous_mavlink_frames)
            if delta and frequency is not None:
                self.channels.setdefault(frequency, ChannelEvidence()).mavlink_frames += delta
                self.last_delivery_at = now
        self.previous_mavlink_frames = delivered

    def continuity_ok(self, snapshot: dict[str, Any], now: float) -> bool:
        fresh = (
            self.last_delivery_at is not None
            and now - self.last_delivery_at <= self.stale_seconds
        )
        all_channels = all(
            self.channels[frequency].mavlink_frames > 0 for frequency in self.frequencies_hz
        )
        return bool(snapshot.get("synchronized")) and fresh and all_channels


def render(snapshot: dict[str, Any], evidence: DemoEvidence, now: float) -> str:
    synchronized = bool(snapshot.get("synchronized"))
    delivered = int(snapshot.get("mavlink_frames_to_local", 0))
    packets = int(snapshot.get("packets_received", 0))
    crc = int(snapshot.get("crc_failures", 0))
    active = evidence.current_frequency_hz
    channel_line = "  ".join(
        (
            f"[ {frequency / 1e6:.0f} MHz ]"
            if frequency == active
            else f"  {frequency / 1e6:.0f} MHz  "
        )
        for frequency in evidence.frequencies_hz
    )
    rows = []
    for frequency in evidence.frequencies_hz:
        channel = evidence.channels[frequency]
        rows.append(
            f"  {frequency / 1e6:>3.0f} MHz   visits={channel.visits:<3} "
            f"MAVLink frames={channel.mavlink_frames}"
        )
    if evidence.last_delivery_at is None:
        age = "waiting"
    else:
        age = f"{now - evidence.last_delivery_at:.1f}s ago"
    verdict = (
        "PASS - TELEMETRY CONTINUES THROUGH HOPS"
        if evidence.continuity_ok(snapshot, now)
        else "COLLECTING EVIDENCE"
    )
    return "\n".join(
        [
            "FREQUENCY-HOPPING FLIGHT-LINK DEMONSTRATION",
            "=" * 48,
            channel_line,
            "",
            f"Link synchronized : {'YES' if synchronized else 'NO'}",
            f"Hops observed     : {evidence.hops_observed}",
            f"Radio packets RX  : {packets}",
            f"MAVLink delivered : {delivered} (last delivery {age})",
            f"CRC failures      : {crc}",
            "",
            "Evidence by channel:",
            *rows,
            "",
            verdict,
            "Bench continuity evidence only; this is not flight certification.",
        ]
    )


def _frequencies(value: str) -> tuple[int, ...]:
    frequencies = tuple(int(float(item.strip()) * 1_000_000) for item in value.split(","))
    if not frequencies:
        raise argparse.ArgumentTypeError("at least one frequency is required")
    return frequencies


def main() -> None:
    parser = argparse.ArgumentParser(description="Live frequency-hop and MAVLink demo monitor")
    parser.add_argument("--url", default="http://127.0.0.1:8765/status")
    parser.add_argument("--frequencies", type=_frequencies, default=_frequencies("915"))
    parser.add_argument("--interval", type=float, default=0.25)
    parser.add_argument("--stale-seconds", type=float, default=5.0)
    args = parser.parse_args()
    evidence = DemoEvidence(args.frequencies, args.stale_seconds)
    try:
        while True:
            try:
                with urllib.request.urlopen(args.url, timeout=2) as response:
                    snapshot = json.loads(response.read())
            except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
                print(
                    "\033[2J\033[H"
                    "FREQUENCY-HOPPING FLIGHT-LINK DEMONSTRATION\n"
                    "=" * 48
                    + f"\nWaiting for status endpoint {args.url}\n{exc}",
                    flush=True,
                )
                time.sleep(args.interval)
                continue
            now = time.monotonic()
            evidence.observe(snapshot, now)
            print("\033[2J\033[H" + render(snapshot, evidence, now), flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
