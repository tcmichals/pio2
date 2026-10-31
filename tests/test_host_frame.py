# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""Link frames on the host: layout, CRC32, rejects, transfers split into frames ([SPEC-LINK-02])."""
# @verifies [SPEC-LINK-02] docs/INTERCONNECT_SPECIFICATION.md

import random
import struct
import zlib

import pytest

from pio2.host import frame as F
from pio2.host.frame import (ACK, NAK, REG_READ, REG_WRITE, STATUS, STREAM_DATA, Frame, FrameError, crc32,
                             decode, reg_read_frames, reg_write_frames, stream_frames, words_to_bytes)


def resign(raw: bytes) -> bytes:
    """Replace the CRC so only the defect under test makes the frame bad."""
    body = raw[:-4]
    return body + struct.pack("<I", zlib.crc32(body))


# ---------------------------------------------------------------- CRC32
@pytest.mark.parametrize("data,crc", [
    (b"123456789", 0xCBF43926),                 # CRC-32/ISO-HDLC check value
    (b"", 0x00000000),
    (b"a", 0xE8B7BE43),
    (b"The quick brown fox jumps over the lazy dog", 0x414FA339),
])
def test_crc32_known_vectors(data, crc):
    assert crc32(data) == crc


def test_crc32_matches_zlib_on_random_data():
    rng = random.Random(1)
    for n in list(range(0, 130)) + [1000]:
        data = bytes(rng.randrange(256) for _ in range(n))
        assert crc32(data) == zlib.crc32(data), n
        cut = n // 3
        assert crc32(data[cut:], crc32(data[:cut])) == zlib.crc32(data), "continuation"


# ---------------------------------------------------------------- layout
def test_op_codes():
    assert (REG_READ, REG_WRITE, STREAM_DATA, STATUS, ACK, NAK) == (1, 2, 3, 4, 5, 6)
    assert F.MAX_PAYLOAD == 116 and F.MAX_PAYLOAD_ADDR == 112


def test_register_frame_layout_byte_for_byte():
    f = Frame(REG_WRITE, chan=0, flags=F.FLAG_CONST_ADDR, seq=0x5A, addr=0x0000_2010,
              payload=words_to_bytes([0x11223344, 0xAABBCCDD]))
    raw = f.encode()
    assert raw[:12] == bytes([0x02, 0x00, 0x01, 0x5A, 8, 0, 0, 0, 0x10, 0x20, 0x00, 0x00])
    assert raw[12:20] == bytes([0x44, 0x33, 0x22, 0x11, 0xDD, 0xCC, 0xBB, 0xAA])
    assert raw[20:] == struct.pack("<I", zlib.crc32(raw[:20]))
    assert len(raw) == 24


def test_stream_frame_has_no_addr():
    raw = Frame(STREAM_DATA, chan=3, seq=7, payload=words_to_bytes([1])).encode()
    assert raw[:8] == bytes([0x03, 3, 0, 7, 4, 0, 0, 0]) and raw[8:12] == b"\x01\0\0\0" and len(raw) == 16


def test_status_frame_without_payload():
    raw = Frame(STATUS, chan=0, seq=9).encode()
    assert len(raw) == 12 and decode(raw) == Frame(STATUS, 0, 0, 9)


@pytest.mark.parametrize("op,addr,n", [(REG_WRITE, 0x3000, 112), (REG_READ, 0, 112), (STREAM_DATA, None, 116),
                                       (STATUS, None, 116), (ACK, None, 4), (NAK, None, 4)])
def test_roundtrip_at_the_limit_is_128_bytes(op, addr, n):
    chan = 0 if op in F.ADDR_OPS or op != STREAM_DATA else 1
    f = Frame(op, chan, 0, 0xFF, bytes(range(n)), addr)
    raw = f.encode()
    assert decode(raw) == f
    if n >= 112:
        assert len(raw) == 128


def test_roundtrip_random_frames():
    rng = random.Random(2)
    for _ in range(2000):
        op = rng.choice(list(F.OP_NAMES))
        addr = rng.randrange(1 << 32) if op in F.ADDR_OPS else None
        chan = 0 if op in F.ADDR_OPS else rng.randrange(1 if op == STREAM_DATA else 0, 256)
        n = rng.randrange(F.max_payload(op) // 4 + 1) * 4
        f = Frame(op, chan, rng.randrange(256), rng.randrange(256), bytes(rng.randrange(256) for _ in range(n)), addr)
        assert decode(f.encode()) == f


# ---------------------------------------------------------------- encode refuses what the link cannot carry
@pytest.mark.parametrize("kw", [
    dict(op=0x07), dict(op=0),
    dict(op=REG_WRITE, addr=None), dict(op=REG_WRITE, addr=0, chan=1),
    dict(op=STREAM_DATA, chan=0), dict(op=STREAM_DATA, chan=1, addr=4),
    dict(op=STREAM_DATA, chan=1, payload=bytes(120)), dict(op=REG_WRITE, addr=0, payload=bytes(116)),
    dict(op=STREAM_DATA, chan=1, payload=bytes(6)), dict(op=STATUS, seq=256),
])
def test_encode_refuses(kw):
    with pytest.raises(FrameError):
        Frame(**kw)


# ---------------------------------------------------------------- decode drops bad frames whole
GOOD = Frame(REG_WRITE, 0, 0, 1, words_to_bytes([1, 2, 3]), 0x100).encode()


def test_empty_window_is_no_frame():
    assert decode(b"") is None


def test_every_single_bit_error_is_caught():
    for bit in range(8 * len(GOOD)):
        bad = bytearray(GOOD)
        bad[bit // 8] ^= 1 << (bit % 8)
        with pytest.raises(FrameError):
            decode(bytes(bad))


def test_bad_crc():
    with pytest.raises(FrameError, match="bad CRC"):
        decode(GOOD[:-1] + bytes([GOOD[-1] ^ 0x80]))


def test_bad_length_field():
    for length in (8, 16, 0):                               # len says 2, 4, 0 words; 3 are there
        raw = bytearray(GOOD)
        raw[4:6] = struct.pack("<H", length)
        with pytest.raises(FrameError, match="bad length"):
            decode(resign(bytes(raw)))


def test_truncated_and_padded():
    with pytest.raises(FrameError):
        decode(resign(GOOD[:-8]))                          # one word lost, CRC recomputed
    with pytest.raises(FrameError, match="bad length"):
        decode(resign(GOOD[:-4] + b"\0\0\0\0" + GOOD[-4:]))
    with pytest.raises(FrameError, match="short"):
        decode(GOOD[:11])


def test_oversize():
    big = Frame(STREAM_DATA, 1, 0, 0, bytes(116)).encode()
    raw = bytearray(big[:-4] + bytes(4) + bytes(4))          # 4 more payload bytes + room for the CRC
    raw[4:6] = struct.pack("<H", 120)
    with pytest.raises(FrameError, match="oversize"):
        decode(resign(bytes(raw)))                         # 132 bytes
    raw = bytearray(Frame(REG_WRITE, 0, 0, 0, bytes(112), 0).encode())
    raw[0] = STREAM_DATA                                    # 128 bytes but len 112 + addr: len ok, layout not
    with pytest.raises(FrameError):
        decode(resign(bytes(raw)))


def test_len_above_the_op_limit():
    raw = bytearray(Frame(STREAM_DATA, 1, 0, 0, bytes(112)).encode())
    raw[0] = REG_WRITE                                      # 112 + 12 + 4 = 128 bytes, but REG_WRITE max is
    raw[4:6] = struct.pack("<H", 116)                       # 112: len 116 must be refused
    with pytest.raises(FrameError, match="len 116"):
        decode(resign(bytes(raw[:8]) + bytes(4) + bytes(116)))


def test_unknown_op_and_reserved():
    for patch in ({0: 0x00}, {0: 0x07}, {0: 0xFF}, {6: 1}, {7: 0x80}):
        raw = bytearray(GOOD)
        for k, v in patch.items():
            raw[k] = v
        with pytest.raises(FrameError):
            decode(resign(bytes(raw)))


def test_word_ops_need_whole_words():
    raw = Frame(STATUS, 0, 0, 0, b"\1\2\3").encode()
    raw = bytearray(raw)
    raw[0] = STREAM_DATA
    raw[1] = 1
    with pytest.raises(FrameError, match="multiple of 4"):
        decode(resign(bytes(raw)))


# ---------------------------------------------------------------- transfers split into frames
def test_stream_frames_split_and_join():
    words = list(range(1000))
    frames = stream_frames(5, words, seq=250)
    assert [len(f.words) for f in frames] == [29] * 34 + [14]
    assert [f.seq for f in frames[:8]] == [250, 251, 252, 253, 254, 255, 0, 1]
    assert all(f.op == STREAM_DATA and f.chan == 5 and len(f.encode()) <= 128 for f in frames)
    assert [w for f in frames for w in decode(f.encode()).words] == words
    assert stream_frames(5, []) == []


def test_reg_write_frames_incrementing_and_constant():
    words = [0x1000 + i for i in range(60)]
    inc = reg_write_frames(0x2000, words, seq=3)
    assert [(f.addr, len(f.words), f.seq, f.flags) for f in inc] == [(0x2000, 28, 3, 0), (0x2070, 28, 4, 0),
                                                                     (0x20E0, 4, 5, 0)]
    assert [w for f in inc for w in f.words] == words
    const = reg_write_frames(0x0110, words, const_addr=True)
    assert {f.addr for f in const} == {0x0110} and all(f.flags == F.FLAG_CONST_ADDR for f in const)
    assert all(decode(f.encode()) == f for f in inc + const)


def test_reg_read_frames():
    reqs = reg_read_frames(0x100, 30, seq=0)
    assert [(f.op, f.addr, f.words) for f in reqs] == [(REG_READ, 0x100, [28]), (REG_READ, 0x170, [2])]
    assert [f.words for f in reg_read_frames(0x10, 1)] == [[1]]
    with pytest.raises(ValueError):
        reg_read_frames(0, 0)
