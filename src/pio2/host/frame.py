# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""Link frames ([SPEC-LINK-02]): encode, decode and split transfers into frames.

Layout (little endian), at most 128 bytes per frame:

    0  op        1  chan      2  flags     3  seq
    4  len (u16, payload bytes)            6  reserved (u16, 0)
    8  addr (u32, REG_READ / REG_WRITE only)
    8 or 12  payload (len bytes)
    end      CRC32 (CRC-32/ISO-HDLC over header + addr + payload)

    f = Frame(REG_WRITE, chan=0, seq=1, addr=0x100, payload=words_to_bytes([1, 2]))
    raw = f.encode()
    decode(raw) == f
"""
# @impl [SPEC-LINK-02] docs/INTERCONNECT_SPECIFICATION.md

import struct
from dataclasses import dataclass

# Op codes ([SPEC-LINK-02], [SPEC-LINK-03])
REG_READ = 0x01
REG_WRITE = 0x02
STREAM_DATA = 0x03
STATUS = 0x04
ACK = 0x05
NAK = 0x06
OP_NAMES = {REG_READ: "REG_READ", REG_WRITE: "REG_WRITE", STREAM_DATA: "STREAM_DATA", STATUS: "STATUS",
            ACK: "ACK", NAK: "NAK"}
ADDR_OPS = (REG_READ, REG_WRITE)          # ops whose header carries the 4-byte addr
WORD_OPS = (REG_READ, REG_WRITE, STREAM_DATA)   # payload is 32-bit words: len a multiple of 4

# flags
FLAG_CONST_ADDR = 0x01                    # register ops: every word at the same address (a FIFO port)
FLAG_DUAL_REPLY = 0x02                    # the FPGA sends the reply with a dual data phase

MAX_FRAME = 128
HEADER = 8
ADDR = 4
CRC = 4
MAX_PAYLOAD = MAX_FRAME - HEADER - CRC            # 116 bytes = 29 words
MAX_PAYLOAD_ADDR = MAX_PAYLOAD - ADDR             # 112 bytes = 28 words
REGISTER_CHANNEL = 0

_HDR = struct.Struct("<BBBBHH")


class FrameError(ValueError):
    """A frame that must be dropped whole: bad CRC, bad length, oversize, unknown op."""


def _crc_table():
    table = []
    for n in range(256):
        c = n
        for _ in range(8):
            c = (c >> 1) ^ 0xEDB88320 if c & 1 else c >> 1     # 0x04C11DB7 reflected
        table.append(c)
    return table


_CRC_TABLE = _crc_table()


def crc32(data: bytes, crc: int = 0) -> int:
    """CRC-32/ISO-HDLC: poly 0x04C11DB7 reflected, init and final XOR 0xFFFFFFFF (same as zlib.crc32).

    Written out (not zlib) so the RTL framer has a readable reference; `crc` continues a previous value.
    """
    c = crc ^ 0xFFFF_FFFF
    for b in data:
        c = _CRC_TABLE[(c ^ b) & 0xFF] ^ (c >> 8)
    return c ^ 0xFFFF_FFFF


def words_to_bytes(words) -> bytes:
    return b"".join(struct.pack("<I", w & 0xFFFF_FFFF) for w in words)


def bytes_to_words(data: bytes) -> list[int]:
    if len(data) % 4:
        raise ValueError(f"{len(data)} bytes is not a whole number of 32-bit words")
    return list(struct.unpack(f"<{len(data) // 4}I", data))


def max_payload(op: int) -> int:
    return MAX_PAYLOAD_ADDR if op in ADDR_OPS else MAX_PAYLOAD


@dataclass(frozen=True)
class Frame:
    op: int
    chan: int = 0
    flags: int = 0
    seq: int = 0
    payload: bytes = b""
    addr: int | None = None      # REG_READ / REG_WRITE only

    def __post_init__(self):
        if self.op not in OP_NAMES:
            raise FrameError(f"unknown op 0x{self.op:02x}")
        for name in ("chan", "flags", "seq"):
            v = getattr(self, name)
            if not 0 <= v <= 0xFF:
                raise FrameError(f"{name} {v} does not fit a byte")
        if self.op in ADDR_OPS:
            if self.addr is None or not 0 <= self.addr <= 0xFFFF_FFFF:
                raise FrameError(f"{OP_NAMES[self.op]} needs a 32-bit addr, got {self.addr!r}")
            if self.chan != REGISTER_CHANNEL:
                raise FrameError(f"{OP_NAMES[self.op]} is a channel-0 op, got chan {self.chan}")
        elif self.addr is not None:
            raise FrameError(f"{OP_NAMES[self.op]} carries no addr")
        if self.op == STREAM_DATA and self.chan == REGISTER_CHANNEL:
            raise FrameError("STREAM_DATA on channel 0 (channel 0 is the register channel)")
        n = len(self.payload)
        if n > max_payload(self.op):
            raise FrameError(f"payload {n} bytes > {max_payload(self.op)} for {OP_NAMES[self.op]}")
        if self.op in WORD_OPS and n % 4:
            raise FrameError(f"{OP_NAMES[self.op]} payload {n} bytes is not a multiple of 4")

    @property
    def words(self) -> list[int]:
        return bytes_to_words(self.payload)

    def encode(self) -> bytes:
        body = _HDR.pack(self.op, self.chan, self.flags, self.seq, len(self.payload), 0)
        if self.op in ADDR_OPS:
            body += struct.pack("<I", self.addr)
        body += self.payload
        return body + struct.pack("<I", crc32(body))


def decode(raw: bytes) -> Frame | None:
    """One frame as read in a CS_n window. Returns None for the empty frame (0 bytes: nothing queued).

    Raises FrameError for anything the framer drops whole: shorter than a header + CRC, longer than
    128 bytes, `len` not matching the bytes received or above the op's limit, reserved not 0, unknown
    op, bad CRC.
    """
    raw = bytes(raw)
    if not raw:
        return None
    if len(raw) > MAX_FRAME:
        raise FrameError(f"oversize frame: {len(raw)} bytes > {MAX_FRAME}")
    if len(raw) < HEADER + CRC:
        raise FrameError(f"short frame: {len(raw)} bytes")
    (want,) = struct.unpack_from("<I", raw, len(raw) - CRC)
    got = crc32(raw[:-CRC])
    if got != want:
        raise FrameError(f"bad CRC: frame says 0x{want:08x}, computed 0x{got:08x}")
    op, chan, flags, seq, length, reserved = _HDR.unpack_from(raw)
    if op not in OP_NAMES:
        raise FrameError(f"unknown op 0x{op:02x}")
    if reserved:
        raise FrameError(f"reserved field 0x{reserved:04x} is not 0")
    head = HEADER + (ADDR if op in ADDR_OPS else 0)
    if length > max_payload(op):
        raise FrameError(f"len {length} > {max_payload(op)} for {OP_NAMES[op]}")
    if head + length + CRC != len(raw):
        raise FrameError(f"bad length: len {length} needs {head + length + CRC} bytes, got {len(raw)}")
    addr = struct.unpack_from("<I", raw, HEADER)[0] if op in ADDR_OPS else None
    return Frame(op, chan, flags, seq, raw[head:head + length], addr)


# ---------------------------------------------------------------- transfers as frames
def _chunks(words, n):
    words = list(words)
    for i in range(0, len(words), n):
        yield i, words[i:i + n]


def stream_frames(chan: int, words, seq: int = 0, flags: int = 0) -> list[Frame]:
    """A word list for stream channel `chan` as STREAM_DATA frames of at most 29 words; seq counts up."""
    return [Frame(STREAM_DATA, chan, flags, (seq + k) & 0xFF, words_to_bytes(part))
            for k, (_, part) in enumerate(_chunks(words, MAX_PAYLOAD // 4))]


def reg_write_frames(addr: int, words, seq: int = 0, const_addr: bool = False) -> list[Frame]:
    """A register burst as REG_WRITE frames of at most 28 words. With `const_addr` every frame writes
    `addr` (a FIFO port); otherwise each frame starts where the previous one ended."""
    flags = FLAG_CONST_ADDR if const_addr else 0
    return [Frame(REG_WRITE, REGISTER_CHANNEL, flags, (seq + k) & 0xFF, words_to_bytes(part),
                  addr if const_addr else (addr + 4 * i) & 0xFFFF_FFFF)
            for k, (i, part) in enumerate(_chunks(words, MAX_PAYLOAD_ADDR // 4))]


def reg_read_frames(addr: int, count: int, seq: int = 0, const_addr: bool = False) -> list[Frame]:
    """A register read of `count` words as REG_READ requests of at most 28 words each.

    A request's payload is one 32-bit word: the number of words to read (1..28). The reply is a
    REG_READ frame with the same seq and addr whose payload is the words read.
    """
    if count < 1:
        raise ValueError("count must be at least 1")
    flags = FLAG_CONST_ADDR if const_addr else 0
    per = MAX_PAYLOAD_ADDR // 4
    out = []
    for k, i in enumerate(range(0, count, per)):
        n = min(per, count - i)
        out.append(Frame(REG_READ, REGISTER_CHANNEL, flags, (seq + k) & 0xFF, words_to_bytes([n]),
                         addr if const_addr else (addr + 4 * i) & 0xFFFF_FFFF))
    return out
