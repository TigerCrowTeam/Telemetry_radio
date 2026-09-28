from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .framing import RadioPacket


class ReplayResult(Enum):
    NEW = "new"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"


@dataclass
class _SourceWindow:
    session_id: int
    highest_sequence: int
    bitmap: int = 1
    retired_sessions: set[int] = field(default_factory=set)


class ReplayProtector:
    """Per-source sliding replay windows with explicit boot-session rollover."""

    def __init__(self, window_size: int = 128, state_file: str | None = None):
        if window_size < 1:
            raise ValueError("replay window size must be positive")
        self.window_size = window_size
        self.state_file = Path(state_file) if state_file is not None else None
        self.sources: dict[int, _SourceWindow] = {}
        if self.state_file is not None and self.state_file.exists():
            self._load()

    def observe(self, packet: RadioPacket) -> ReplayResult:
        if packet.session_id == 0:
            return ReplayResult.REJECTED
        state = self.sources.get(packet.source)
        if state is None:
            self.sources[packet.source] = _SourceWindow(packet.session_id, packet.sequence)
            self._save()
            return ReplayResult.NEW
        if packet.session_id != state.session_id:
            if packet.session_id in state.retired_sessions or packet.session_id < state.session_id:
                return ReplayResult.REJECTED
            retired = set(state.retired_sessions)
            retired.add(state.session_id)
            self.sources[packet.source] = _SourceWindow(
                packet.session_id,
                packet.sequence,
                retired_sessions=retired,
            )
            self._save()
            return ReplayResult.NEW
        if packet.sequence > state.highest_sequence:
            shift = packet.sequence - state.highest_sequence
            state.bitmap = (
                1
                if shift >= self.window_size
                else ((state.bitmap << shift) | 1) & ((1 << self.window_size) - 1)
            )
            state.highest_sequence = packet.sequence
            self._save()
            return ReplayResult.NEW
        distance = state.highest_sequence - packet.sequence
        if distance >= self.window_size:
            return ReplayResult.REJECTED
        mask = 1 << distance
        if state.bitmap & mask:
            return ReplayResult.DUPLICATE
        state.bitmap |= mask
        self._save()
        return ReplayResult.NEW

    def _load(self) -> None:
        if self.state_file is None:
            return
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
            if raw.get("version") != 1 or raw.get("window_size") != self.window_size:
                raise ValueError("incompatible replay-state format or window size")
            for source, value in raw.get("sources", {}).items():
                self.sources[int(source)] = _SourceWindow(
                    int(value["session_id"]),
                    int(value["highest_sequence"]),
                    int(value["bitmap"]),
                    {int(item) for item in value.get("retired_sessions", [])},
                )
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot load replay state {self.state_file}: {exc}") from exc

    def _save(self) -> None:
        if self.state_file is None:
            return
        data = {
            "version": 1,
            "window_size": self.window_size,
            "sources": {
                str(source): {
                    "session_id": state.session_id,
                    "highest_sequence": state.highest_sequence,
                    "bitmap": str(state.bitmap),
                    "retired_sessions": sorted(state.retired_sessions),
                }
                for source, state in self.sources.items()
            },
        }
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
            temporary.write_text(json.dumps(data, separators=(",", ":")) + "\n", encoding="utf-8")
            temporary.replace(self.state_file)
        except OSError as exc:
            raise ValueError(f"cannot persist replay state {self.state_file}: {exc}") from exc
