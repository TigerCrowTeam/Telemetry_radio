import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from telemetry_radio.framing import (
    PROTOCOL_VERSION,
    AuthenticationError,
    ProtocolVersionError,
    RadioPacket,
    decode_mavlink_bundle,
    encode_mavlink_bundle,
)


def test_packet_round_trip_preserves_payload():
    payload = b"\xfd\x03\x00\x00 mavlink bytes"
    packet = RadioPacket(2, 255, 17, payload)
    assert RadioPacket.decode(packet.encode()) == packet


def test_encoded_packet_declares_current_protocol_version():
    wire = RadioPacket(2, 255, 17, b"payload").encode()
    assert wire[2] == PROTOCOL_VERSION


def test_incompatible_protocol_version_is_rejected_explicitly():
    wire = bytearray(RadioPacket(2, 255, 17, b"payload").encode())
    wire[2] = PROTOCOL_VERSION + 1
    with pytest.raises(
        ProtocolVersionError,
        match=f"incompatible protocol version {PROTOCOL_VERSION + 1}",
    ) as error:
        RadioPacket.decode(bytes(wire))
    assert error.value.received == PROTOCOL_VERSION + 1
    assert error.value.supported == PROTOCOL_VERSION


def test_crc_rejects_corruption():
    wire = bytearray(RadioPacket(1, 0, 0, b"hello").encode())
    wire[-5] ^= 1
    with pytest.raises(ValueError, match="CRC"):
        RadioPacket.decode(bytes(wire))


def test_32_bit_sequence_round_trip():
    packet = RadioPacket(2, 1, 0xFEDCBA98, b"high rate telemetry")
    assert RadioPacket.decode(packet.encode()) == packet


def test_sequence_rejects_values_outside_uint32():
    with pytest.raises(ValueError, match="32-bit"):
        RadioPacket(2, 1, 0x1_0000_0000, b"invalid").encode()


def test_authenticated_packet_round_trip():
    key = Ed25519PrivateKey.generate()
    packet = RadioPacket(2, 1, 9, b"authenticated command")
    assert RadioPacket.decode(packet.encode(key), {2: key.public_key()}) == packet
    assert packet.encoded_size(authenticated=True) == len(packet.encode(key))
    assert packet.encoded_size(authenticated=False) == len(packet.encode())


def test_wrong_key_and_missing_tag_are_rejected():
    packet = RadioPacket(2, 1, 9, b"command")
    signing_key = Ed25519PrivateKey.generate()
    wrong_key = Ed25519PrivateKey.generate()
    with pytest.raises(AuthenticationError, match="signature verification failed"):
        RadioPacket.decode(packet.encode(signing_key), {2: wrong_key.public_key()})
    with pytest.raises(AuthenticationError, match="missing"):
        RadioPacket.decode(packet.encode(), {2: signing_key.public_key()})


def test_mavlink_bundle_round_trip_and_strict_validation():
    frames = [b"\xfdfirst", b"\xfesecond", b"\xfdthird"]
    payload = encode_mavlink_bundle(frames)
    assert decode_mavlink_bundle(payload) == frames

    with pytest.raises(ValueError, match="2 to 255"):
        encode_mavlink_bundle([b"one"])
    with pytest.raises(ValueError, match="truncated"):
        decode_mavlink_bundle(payload[:-1])
    with pytest.raises(ValueError, match="trailing"):
        decode_mavlink_bundle(payload + b"x")
