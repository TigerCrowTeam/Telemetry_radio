from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .authentication import load_private_key, load_public_keys, public_key_fingerprint


def generate_key_pair(
    node_id: int, directory: str | Path, label: str | None = None
) -> tuple[Path, Path]:
    if not 1 <= node_id <= 255:
        raise ValueError("node ID must be between 1 and 255")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    label = label or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = f"node{node_id}-{label}"
    private_path = directory / f"{stem}-private.pem"
    public_path = directory / f"{stem}-public.pem"
    if private_path.exists() or public_path.exists():
        raise ValueError(f"key output already exists for {stem}")
    key = Ed25519PrivateKey.generate()
    private_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    os.chmod(private_path, 0o600)
    public_path.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    os.chmod(public_path, 0o644)
    return private_path, public_path


def verify_key_pair(private_path: str | Path, public_path: str | Path) -> str:
    private = load_private_key(private_path)
    public = load_public_keys({1: str(public_path)})[1][0]
    private_public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    supplied_public = public.public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    if private_public != supplied_public:
        raise ValueError("private and public keys do not form a pair")
    return public_key_fingerprint(public)


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage telemetry-radio node identities")
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate", help="generate a new Ed25519 node identity")
    generate.add_argument("node_id", type=int)
    generate.add_argument("output_directory")
    generate.add_argument("--label")
    verify = commands.add_parser("verify", help="verify a private/public key pair")
    verify.add_argument("private_key")
    verify.add_argument("public_key")
    inspect = commands.add_parser("inspect", help="print public-key fingerprints")
    inspect.add_argument("public_keys", nargs="+")
    args = parser.parse_args()
    try:
        if args.command == "generate":
            private, public = generate_key_pair(args.node_id, args.output_directory, args.label)
            fingerprint = verify_key_pair(private, public)
            print(
                json.dumps(
                    {
                        "private_key": str(private),
                        "public_key": str(public),
                        "fingerprint": fingerprint,
                    }
                )
            )
        elif args.command == "verify":
            print(json.dumps({"fingerprint": verify_key_pair(args.private_key, args.public_key)}))
        else:
            for path in args.public_keys:
                key = load_public_keys({1: path})[1][0]
                print(json.dumps({"public_key": path, "fingerprint": public_key_fingerprint(key)}))
    except ValueError as exc:
        parser.exit(1, f"telemetry-radio-keys: {exc}\n")


if __name__ == "__main__":
    main()
