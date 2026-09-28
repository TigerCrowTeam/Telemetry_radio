from __future__ import annotations

from dataclasses import dataclass

import numpy as np

INTERLEAVER_DEPTH = 16


class UncorrectableFECError(ValueError):
    """Raised when extended Hamming detects a multi-bit codeword error."""


@dataclass(frozen=True)
class FECDecodeResult:
    payload: bytes
    corrected_bits: int


def _encode_nibble(nibble: int) -> int:
    # Hamming positions 1..7 use parity at 1, 2, and 4; position 8 is
    # overall parity, producing an extended Hamming SECDED codeword.
    bits = [0] * 8
    bits[2] = (nibble >> 3) & 1
    bits[4] = (nibble >> 2) & 1
    bits[5] = (nibble >> 1) & 1
    bits[6] = nibble & 1
    bits[0] = bits[2] ^ bits[4] ^ bits[6]
    bits[1] = bits[2] ^ bits[5] ^ bits[6]
    bits[3] = bits[4] ^ bits[5] ^ bits[6]
    bits[7] = bits[0] ^ bits[1] ^ bits[2] ^ bits[3] ^ bits[4] ^ bits[5] ^ bits[6]
    return sum(bit << (7 - index) for index, bit in enumerate(bits))


def _decode_codeword(codeword: int) -> tuple[int, bool]:
    bits = [(codeword >> (7 - index)) & 1 for index in range(8)]
    syndrome = (
        (bits[0] ^ bits[2] ^ bits[4] ^ bits[6])
        | ((bits[1] ^ bits[2] ^ bits[5] ^ bits[6]) << 1)
        | ((bits[3] ^ bits[4] ^ bits[5] ^ bits[6]) << 2)
    )
    overall = 0
    for bit in bits:
        overall ^= bit
    corrected = False
    if syndrome and overall:
        bits[syndrome - 1] ^= 1
        corrected = True
    elif not syndrome and overall:
        bits[7] ^= 1
        corrected = True
    elif syndrome and not overall:
        raise UncorrectableFECError("uncorrectable multi-bit FEC error")
    nibble = (bits[2] << 3) | (bits[4] << 2) | (bits[5] << 1) | bits[6]
    return nibble, corrected


def interleave(codewords: bytes, depth: int = INTERLEAVER_DEPTH) -> bytes:
    if depth < 1:
        raise ValueError("interleaver depth must be positive")
    output = bytearray()
    for start in range(0, len(codewords), depth):
        block = np.frombuffer(codewords[start : start + depth], dtype=np.uint8)
        bits = np.unpackbits(block).reshape(len(block), 8)
        output.extend(np.packbits(bits.T.reshape(-1)).tobytes())
    return bytes(output)


def deinterleave(encoded: bytes, depth: int = INTERLEAVER_DEPTH) -> bytes:
    if depth < 1:
        raise ValueError("interleaver depth must be positive")
    output = bytearray()
    for start in range(0, len(encoded), depth):
        block = np.frombuffer(encoded[start : start + depth], dtype=np.uint8)
        bits = np.unpackbits(block).reshape(8, len(block)).T
        output.extend(np.packbits(bits.reshape(-1)).tobytes())
    return bytes(output)


def encode(payload: bytes, depth: int = INTERLEAVER_DEPTH) -> bytes:
    if len(payload) > 0xFFFF:
        raise ValueError("FEC payload is too large")
    protected = len(payload).to_bytes(2, "big") + payload
    codewords = bytearray()
    for value in protected:
        codewords.append(_encode_nibble(value >> 4))
        codewords.append(_encode_nibble(value & 0x0F))
    while len(codewords) % depth:
        codewords.append(_encode_nibble(0))
    return interleave(bytes(codewords), depth)


def decode(encoded: bytes, depth: int = INTERLEAVER_DEPTH) -> FECDecodeResult:
    if len(encoded) % 2:
        raise UncorrectableFECError("FEC payload has an odd codeword count")
    codewords = deinterleave(encoded, depth)
    nibbles: list[int] = []
    corrected_bits = 0
    for codeword in codewords:
        nibble, corrected = _decode_codeword(codeword)
        nibbles.append(nibble)
        corrected_bits += int(corrected)
    decoded = bytes(
        (nibbles[index] << 4) | nibbles[index + 1] for index in range(0, len(nibbles), 2)
    )
    if len(decoded) < 2:
        raise UncorrectableFECError("FEC payload is missing its length")
    length = int.from_bytes(decoded[:2], "big")
    if length > len(decoded) - 2:
        raise UncorrectableFECError("FEC payload length exceeds decoded data")
    payload = decoded[2 : 2 + length]
    return FECDecodeResult(payload, corrected_bits)


def encoded_size(payload_size: int, depth: int = INTERLEAVER_DEPTH) -> int:
    if payload_size < 0 or payload_size > 0xFFFF or depth < 1:
        raise ValueError("invalid FEC payload size or interleaver depth")
    codewords = (payload_size + 2) * 2
    return ((codewords + depth - 1) // depth) * depth
