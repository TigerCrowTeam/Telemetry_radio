from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def _read(path: str | Path, label: str) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read {label} file {path}: {exc}") from exc


def load_private_key(path: str | Path) -> Ed25519PrivateKey:
    path = Path(path)
    try:
        if os.name == "posix" and path.stat().st_mode & 0o007:
            raise ValueError(f"private key file {path} must not be accessible by other users")
    except OSError as exc:
        raise ValueError(f"cannot inspect private key file {path}: {exc}") from exc
    try:
        key = serialization.load_pem_private_key(_read(path, "private key"), password=None)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid Ed25519 private key file {path}") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(f"private key file {path} is not an Ed25519 key")
    return key


def load_public_keys(
    paths: Mapping[int, str | Sequence[str]],
) -> dict[int, tuple[Ed25519PublicKey, ...]]:
    keys: dict[int, tuple[Ed25519PublicKey, ...]] = {}
    for node_id, configured in paths.items():
        node_keys = []
        for path in (configured,) if isinstance(configured, str) else configured:
            try:
                key = serialization.load_pem_public_key(_read(path, f"node {node_id} public key"))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid Ed25519 public key file {path}") from exc
            if not isinstance(key, Ed25519PublicKey):
                raise ValueError(f"public key file {path} is not an Ed25519 key")
            node_keys.append(key)
        keys[node_id] = tuple(node_keys)
    return keys


def public_key_fingerprint(key: Ed25519PublicKey) -> str:
    encoded = key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    digest = hashlib.sha256(encoded).hexdigest()
    return ":".join(digest[index : index + 2] for index in range(0, len(digest), 2))
