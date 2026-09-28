from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .fec import decode as fec_decode
from .fec import encode as fec_encode
from .framing import PROTOCOL_VERSION, PacketKind, RadioPacket
from .modulation import SYNC


def _default_fixture() -> Path:
    checkout = Path(__file__).parents[1] / "fixtures/protocol_v4.json"
    installed = Path(sys.prefix) / "share/telemetry-radio/fixtures/protocol_v4.json"
    return checkout if checkout.exists() else installed


DEFAULT_FIXTURE = _default_fixture()


def validate_fixture(path: str | Path = DEFAULT_FIXTURE) -> list[str]:
    try:
        fixture: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read conformance fixture {path}: {exc}") from exc
    if fixture.get("fixture_format_version") != 1:
        raise ValueError("unsupported conformance fixture format")
    if fixture.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("fixture protocol version does not match this implementation")
    private = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(fixture["test_private_seed_hex"]))
    public_raw = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    if public_raw.hex() != fixture["test_public_key_raw_hex"]:
        raise ValueError("fixture public key does not match its deterministic test seed")
    validated = []
    for vector in fixture.get("vectors", []):
        name = vector["name"]
        fields = vector["packet"]
        packet = RadioPacket(
            int(fields["source"]),
            int(fields["destination"]),
            int(fields["sequence"]),
            bytes.fromhex(fields["payload_hex"]),
            PacketKind[fields["kind"]],
            int(fields["session_id"]),
        )
        wire = packet.encode(private)
        if wire.hex() != vector["wire_hex"]:
            raise ValueError(f"{name}: signed wire bytes changed")
        if wire[-4:].hex() != vector["crc32_hex"]:
            raise ValueError(f"{name}: CRC bytes changed")
        if RadioPacket.decode(wire, {packet.source: (private.public_key(),)}) != packet:
            raise ValueError(f"{name}: signed frame does not decode to its packet")
        fec = fec_encode(wire)
        if fec.hex() != vector["fec_interleaved_hex"]:
            raise ValueError(f"{name}: FEC/interleaved bytes changed")
        if fec_decode(fec).payload != wire:
            raise ValueError(f"{name}: FEC fixture does not decode to its wire frame")
        envelope = SYNC + len(fec).to_bytes(2, "big") + fec
        if envelope.hex() != vector["modem_envelope_hex"]:
            raise ValueError(f"{name}: modem envelope bytes changed")
        validated.append(name)
    if set(validated) != {"data", "ack", "sync"}:
        raise ValueError("fixture must contain DATA, ACK, and SYNC vectors")
    return validated


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate protocol conformance vectors")
    parser.add_argument("fixture", nargs="?", default=str(DEFAULT_FIXTURE))
    args = parser.parse_args()
    try:
        names = validate_fixture(args.fixture)
        print(f"valid protocol v{PROTOCOL_VERSION} vectors: {', '.join(names)}")
    except (KeyError, TypeError, ValueError) as exc:
        parser.exit(1, f"telemetry-radio-conformance: {exc}\n")


if __name__ == "__main__":
    main()
