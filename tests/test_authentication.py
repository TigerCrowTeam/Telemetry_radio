import struct
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from telemetry_radio.authentication import load_private_key, load_public_keys
from telemetry_radio.config import AuthenticationConfig
from telemetry_radio.framing import BROADCAST_ID, PacketKind, RadioPacket
from telemetry_radio.key_cli import generate_key_pair, verify_key_pair
from telemetry_radio.simulation import SimulatedNetwork
from tests.test_end_to_end import node


def identities(tmp_path, node_ids):
    private_paths = {}
    public_paths = {}
    private_keys = {}
    for node_id in node_ids:
        key = Ed25519PrivateKey.generate()
        private = tmp_path / f"{node_id}-private.pem"
        public = tmp_path / f"{node_id}-public.pem"
        private.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        private.chmod(0o600)
        public.write_bytes(
            key.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
        private_paths[node_id] = str(private)
        public_paths[node_id] = str(public)
        private_keys[node_id] = key
    return private_paths, public_paths, private_keys


def test_key_files_must_contain_ed25519_keys(tmp_path):
    path = tmp_path / "bad.pem"
    path.write_bytes(b"not a key")
    path.chmod(0o600)
    with pytest.raises(ValueError, match="invalid Ed25519 private key"):
        load_private_key(path)
    with pytest.raises(ValueError, match="invalid Ed25519 public key"):
        load_public_keys({1: str(path)})


def test_key_cli_generates_restricted_matching_identity(tmp_path):
    private, public = generate_key_pair(2, tmp_path, "rotation1")
    assert private.stat().st_mode & 0o777 == 0o600
    assert public.stat().st_mode & 0o777 == 0o644
    assert len(verify_key_pair(private, public).split(":")) == 32


def test_world_accessible_private_key_is_rejected(tmp_path):
    private, _ = generate_key_pair(2, tmp_path, "permissions")
    private.chmod(0o604)
    with pytest.raises(ValueError, match="must not be accessible by other users"):
        load_private_key(private)


def test_overlapping_public_keys_allow_rotation_then_revocation():
    old = Ed25519PrivateKey.generate()
    new = Ed25519PrivateKey.generate()
    packet = RadioPacket(2, 1, 8, b"rotation", session_id=100)
    trusted_during_rotation = {2: (old.public_key(), new.public_key())}
    assert RadioPacket.decode(packet.encode(old), trusted_during_rotation) == packet
    assert RadioPacket.decode(packet.encode(new), trusted_during_rotation) == packet
    with pytest.raises(ValueError, match="signature verification"):
        RadioPacket.decode(packet.encode(old), {2: (new.public_key(),)})


def test_authenticated_nodes_exchange_traffic(tmp_path):
    private, public, _ = identities(tmp_path, (1, 2, 3, 255))
    configs = {
        "gcs": replace(node("gcs", 255), authentication=AuthenticationConfig(private[255], public)),
        "master": replace(
            node("master", 1, relay=True), authentication=AuthenticationConfig(private[1], public)
        ),
        "slave2": replace(
            node("slave", 2), authentication=AuthenticationConfig(private[2], public)
        ),
        "slave3": replace(
            node("slave", 3), authentication=AuthenticationConfig(private[3], public)
        ),
    }
    radio_network = SimulatedNetwork(configs)
    radio_network.step(3)
    try:
        frame = b"\xfdauthenticated telemetry"
        radio_network.inject("slave2", frame)
        radio_network.step(30)
        assert frame in radio_network.received("gcs")
    finally:
        radio_network.close()


def test_receiver_counts_and_discards_packet_signed_by_wrong_key(tmp_path):
    private, public, keys = identities(tmp_path, (1, 255))
    radio_network = SimulatedNetwork(
        {
            "master": replace(
                node("master", 1, relay=True),
                authentication=AuthenticationConfig(private[1], public),
            ),
            "gcs": replace(
                node("gcs", 255), authentication=AuthenticationConfig(private[255], public)
            ),
        }
    )
    radio_network.step(3)
    try:
        master = radio_network.nodes["master"]
        hostile = RadioPacket(255, 1, 99, b"forged command", session_id=8).encode(
            Ed25519PrivateKey.generate()
        )
        master.radio.incoming.append(master.app.modem.modulate(hostile))
        for _ in range(5):
            master.app.step()
        snapshot = master.app.statistics.snapshot()
        assert snapshot["authentication_failures"] == 1
        assert snapshot["invalid_packets"] == 1
        assert b"forged command" not in radio_network.received("master")
    finally:
        radio_network.close()


def test_sync_freshness_rejects_backward_and_large_forward_jumps(tmp_path):
    private, public, _ = identities(tmp_path, (1, 255))
    radio_network = SimulatedNetwork(
        {
            "master": replace(
                node("master", 1, relay=True),
                authentication=AuthenticationConfig(private[1], public),
            ),
            "gcs": replace(
                node("gcs", 255), authentication=AuthenticationConfig(private[255], public)
            ),
        }
    )
    try:
        app = radio_network.nodes["gcs"].app
        app.last_sync_master_time = 1_000.0
        app.last_sync_received_at = 50.0
        assert app._sync_is_fresh(1_005.0, 55.0)
        assert not app._sync_is_fresh(900.0, 55.0)
        assert not app._sync_is_fresh(1_100.0, 55.0)
    finally:
        radio_network.close()


def test_stale_signed_sync_does_not_refresh_link_liveness(tmp_path):
    private, public, _ = identities(tmp_path, (1, 255))
    radio_network = SimulatedNetwork(
        {
            "master": replace(
                node("master", 1, relay=True),
                authentication=AuthenticationConfig(private[1], public),
            ),
            "gcs": replace(
                node("gcs", 255), authentication=AuthenticationConfig(private[255], public)
            ),
        }
    )
    radio_network.step(3)
    try:
        master = radio_network.nodes["master"].app
        gcs_node = radio_network.nodes["gcs"]
        gcs = gcs_node.app
        gcs.last_sync_master_time = 1_000.0
        gcs.last_sync_received_at = gcs.monotonic_clock()
        last_rx = gcs.last_rx
        stale = RadioPacket(
            1,
            BROADCAST_ID,
            100_000,
            struct.pack("!d", 900.0),
            PacketKind.SYNC,
            master.session_id,
        ).encode(master.signing_key)
        gcs_node.radio.incoming.append(gcs.modem.modulate(stale))
        for _ in range(5):
            gcs.step()
        assert gcs.statistics.snapshot()["sync_freshness_rejections"] == 1
        assert gcs.last_rx == last_rx
    finally:
        radio_network.close()
