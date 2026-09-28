from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import shutil
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .fec import INTERLEAVER_DEPTH
from .framing import PROTOCOL_VERSION, RadioPacket
from .modulation import BPSKModem, QPSKModem

CAPTURE_FORMAT_VERSION = 1
IQ_DTYPE = np.dtype("<c8")
LOG = logging.getLogger(__name__)


class CaptureRecorder:
    """Append IQ samples and demodulated radio frames to a capture directory."""

    def __init__(
        self,
        directory: str | Path,
        *,
        sample_rate: float,
        samples_per_symbol: int,
        rrc_rolloff: float = 0.0,
        rrc_span_symbols: int = 8,
        modulation: str = "bpsk",
        interleaver_depth: int = INTERLEAVER_DEPTH,
        bit_repetitions: int = 1,
        rs_parity_bytes: int = 0,
        record_iq: bool = True,
        record_frames: bool = True,
        authenticated: bool = False,
        max_file_bytes: int = 256 * 1024 * 1024,
        max_sessions: int = 10,
        min_free_bytes: int = 128 * 1024 * 1024,
        create_session: bool = False,
        on_disable: Callable[[str], None] | None = None,
    ):
        if max_file_bytes < 16 * 1024 or max_sessions < 1 or min_free_bytes < 0:
            raise ValueError("invalid capture size, retention, or free-space limit")
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        if create_session:
            self._prune_sessions(root, max_sessions - 1)
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            root = root / f"capture-{stamp}-{os.getpid()}-{secrets.token_hex(3)}"
        self.directory = root
        self.directory.mkdir(parents=True, exist_ok=True)
        self.max_file_bytes = max_file_bytes
        self.min_free_bytes = min_free_bytes
        self.on_disable = on_disable
        self.disabled_reason: str | None = None
        self.record_iq_enabled = record_iq
        self.record_frames_enabled = record_frames
        self._started_ns = time.time_ns()
        manifest = {
            "capture_format_version": CAPTURE_FORMAT_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "fec_scheme": "extended-hamming-8-4",
            "interleaver_depth": interleaver_depth,
            "bit_repetitions": bit_repetitions,
            "rs_parity_bytes": rs_parity_bytes,
            "authenticated": authenticated,
            "sample_rate": sample_rate,
            "samples_per_symbol": samples_per_symbol,
            "rrc_rolloff": rrc_rolloff,
            "rrc_span_symbols": rrc_span_symbols,
            "modulation": modulation,
            "iq_dtype": IQ_DTYPE.str,
        }
        if create_session:
            manifest["session_started_unix_ns"] = self._started_ns
        manifest_path = self.directory / "manifest.json"
        if manifest_path.exists():
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if existing != manifest:
                raise ValueError("capture directory has an incompatible manifest")
        else:
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        self._iq_index = 0
        self._frame_index = 0
        self._iq = self._open_iq() if record_iq else None
        self._frames = self._open_frames() if record_frames else None

    @staticmethod
    def _prune_sessions(root: Path, keep: int) -> None:
        sessions = sorted(
            (
                path
                for path in root.glob("capture-*")
                if path.is_dir() and (path / "manifest.json").is_file()
            ),
            key=lambda path: (path / "manifest.json").stat().st_mtime_ns,
        )
        for path in sessions[: max(0, len(sessions) - keep)]:
            shutil.rmtree(path)

    def _open_iq(self):
        path = self.directory / f"iq-{self._iq_index:05d}.c64"
        return path.open("ab")

    def _open_frames(self):
        path = self.directory / f"frames-{self._frame_index:05d}.jsonl"
        return path.open("a", encoding="utf-8")

    def _space_available(self, amount: int) -> bool:
        return shutil.disk_usage(self.directory).free - amount >= self.min_free_bytes

    def _disable(self, reason: str) -> None:
        if self.disabled_reason is not None:
            return
        self.disabled_reason = reason
        LOG.error("capture recording disabled: %s", reason)
        self.close()
        if self.on_disable is not None:
            self.on_disable(reason)

    def record_iq(self, samples: np.ndarray) -> None:
        if self._iq is None or len(samples) == 0:
            return
        data = memoryview(np.asarray(samples, dtype=IQ_DTYPE).tobytes())
        try:
            if not self._space_available(len(data)):
                self._disable("minimum free-space threshold reached")
                return
            while data:
                available = self.max_file_bytes - self._iq.tell()
                if available < IQ_DTYPE.itemsize:
                    self._iq.close()
                    self._iq_index += 1
                    self._iq = self._open_iq()
                    available = self.max_file_bytes
                chunk_size = min(len(data), available - available % IQ_DTYPE.itemsize)
                self._iq.write(data[:chunk_size])
                data = data[chunk_size:]
            self._iq.flush()
        except OSError as exc:
            self._disable(str(exc))

    def record_frame(self, wire: bytes, error: str | None = None) -> None:
        if self._frames is None:
            return
        entry = {
            "elapsed_ns": time.time_ns() - self._started_ns,
            "wire_base64": base64.b64encode(wire).decode("ascii"),
            "valid": error is None,
        }
        if error is not None:
            entry["error"] = error
        line = json.dumps(entry, separators=(",", ":")) + "\n"
        try:
            encoded_size = len(line.encode("utf-8"))
            if not self._space_available(encoded_size):
                self._disable("minimum free-space threshold reached")
                return
            if self._frames.tell() and self._frames.tell() + encoded_size > self.max_file_bytes:
                self._frames.close()
                self._frame_index += 1
                self._frames = self._open_frames()
            self._frames.write(line)
            self._frames.flush()
        except OSError as exc:
            self._disable(str(exc))

    def close(self) -> None:
        if self._iq is not None:
            self._iq.close()
            self._iq = None
        if self._frames is not None:
            self._frames.close()
            self._frames = None


def load_manifest(directory: str | Path) -> dict[str, Any]:
    path = Path(directory) / "manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read capture manifest: {exc}") from exc
    if manifest.get("capture_format_version") != CAPTURE_FORMAT_VERSION:
        raise ValueError("unsupported capture format version")
    return manifest


def replay_iq(directory: str | Path, chunk_samples: int = 4096) -> Iterator[bytes]:
    manifest = load_manifest(directory)
    modem_class = QPSKModem if manifest.get("modulation", "bpsk") == "qpsk" else BPSKModem
    modem = modem_class(
        int(manifest["samples_per_symbol"]),
        sample_rate=float(manifest["sample_rate"]),
        rrc_rolloff=float(manifest.get("rrc_rolloff", 0.0)),
        rrc_span_symbols=int(manifest.get("rrc_span_symbols", 8)),
        interleaver_depth=int(manifest.get("interleaver_depth", INTERLEAVER_DEPTH)),
        bit_repetitions=int(manifest.get("bit_repetitions", 1)),
        rs_parity_bytes=int(manifest.get("rs_parity_bytes", 0)),
    )
    try:
        paths = sorted(Path(directory).glob("iq-*.c64"))
        legacy = Path(directory) / "iq.c64"
        if legacy.exists():
            paths.insert(0, legacy)
        for path in paths:
            with path.open("rb") as stream:
                while True:
                    samples = np.fromfile(stream, dtype=IQ_DTYPE, count=chunk_samples)
                    if len(samples) == 0:
                        break
                    yield from modem.feed(samples)
    except OSError as exc:
        raise ValueError(f"cannot replay IQ capture: {exc}") from exc


def replay_frames(directory: str | Path) -> Iterator[bytes]:
    load_manifest(directory)
    try:
        paths = sorted(Path(directory).glob("frames-*.jsonl"))
        legacy = Path(directory) / "frames.jsonl"
        if legacy.exists():
            paths.insert(0, legacy)
        for path in paths:
            with path.open("r", encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, 1):
                    try:
                        entry = json.loads(line)
                        yield base64.b64decode(entry["wire_base64"], validate=True)
                    except (json.JSONDecodeError, KeyError, ValueError) as exc:
                        raise ValueError(
                            f"invalid frame record in {path.name} line {line_number}"
                        ) from exc
    except OSError as exc:
        raise ValueError(f"cannot replay frame capture: {exc}") from exc


def describe_frames(
    wires: Iterable[bytes], verification_keys: dict[int, Any] | None = None
) -> Iterator[dict[str, Any]]:
    for index, wire in enumerate(wires):
        try:
            packet = RadioPacket.decode(wire, verification_keys)
        except ValueError as exc:
            yield {"index": index, "valid": False, "error": str(exc)}
            continue
        yield {
            "index": index,
            "valid": True,
            "kind": packet.kind.name,
            "source": packet.source,
            "destination": packet.destination,
            "sequence": packet.sequence,
            "session_id": packet.session_id,
            "payload_base64": base64.b64encode(packet.payload).decode("ascii"),
        }
