import pytest

from telemetry_radio.fec import UncorrectableFECError, decode, encode


def flip_bits(data: bytes, *positions: int) -> bytes:
    damaged = bytearray(data)
    for position in positions:
        damaged[position // 8] ^= 1 << (7 - position % 8)
    return bytes(damaged)


def test_fec_round_trip():
    payload = bytes(range(256))
    result = decode(encode(payload))
    assert result.payload == payload
    assert result.corrected_bits == 0


@pytest.mark.parametrize("position", range(8 * 2 * 4))
def test_fec_corrects_any_single_transmitted_bit(position):
    payload = b"test"
    result = decode(flip_bits(encode(payload), position))
    assert result.payload == payload
    assert result.corrected_bits == 1


def test_interleaving_turns_sixteen_bit_burst_into_correctable_errors():
    payload = b"partial final block"
    encoded = encode(payload)
    result = decode(flip_bits(encoded, *range(len(encoded) * 8 - 19, len(encoded) * 8 - 3)))
    assert result.payload == payload
    assert result.corrected_bits == 16


def test_fec_detects_two_errors_in_one_codeword():
    encoded = encode(b"eightbyt")
    # With depth 16, transmitted bits 0 and 16 belong to the same codeword.
    with pytest.raises(UncorrectableFECError, match="multi-bit"):
        decode(flip_bits(encoded, 0, 16))
