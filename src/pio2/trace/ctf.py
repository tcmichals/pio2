# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trace records ([SPEC-TRC-01]) to CTF 1.8 ([SPEC-TRC-02]): a `metadata` file and one binary stream.

A record is 64 bits: [15:0] data, [23:16] src, [31:24] type, [63:32] timestamp (system clocks,
wraps at 32 bits). On the link a record is two 32-bit words, bits [31:0] first.

The host extends the 32-bit timestamps to 64 bits (records arrive in the event FIFO's priority order,
so a record may be older than the one before it: each timestamp is taken as the one nearest the
previous record's, within +-2^31 clocks), sorts by the extended timestamp and writes CTF packets.
The event record types follow `src/pio2/trace/barectf_config.yaml` (the same names and fields, in
the same order; `tests/test_trace_ctf.py` checks they agree). DROPPED records become `pio_dropped`
events and also raise the packet context's `events_discarded` counter, so babeltrace2 reports the loss.

    uv run pio2-ctf records.bin out_dir            # raw link words (little endian u32, low word first)
    babeltrace2 out_dir
"""
# @impl [SPEC-TRC-02] docs/INTERCONNECT_SPECIFICATION.md

import argparse
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

DROPPED = 0x1F
DEFAULT_FREQ_HZ = 125_000_000      # RP2040 nominal system clock; pass the real one per board / session
CTF_MAGIC = 0xC1FC1FC1

# Event record types: (name, PIO type code or None for the generic one, (src field, data field)).
# Ids are the index in this list. Type codes: [SPEC-TRC-01]; field names follow AbstractX's `pio_event`
# (tile_id, sm_id, event_type, pc).
EVENT_TYPES = [
    ("pio_event", None, ("sm_id", "data")),              # a type code with no class of its own
    ("pio_stall_begin", 0x01, ("sm_id", "pc")),
    ("pio_stall_end", 0x02, ("sm_id", "pc")),
    ("pio_irq_set", 0x03, ("src", "irq")),
    ("pio_irq_clear", 0x04, ("src", "irq")),
    ("pio_fifo_full", 0x05, ("sm_id", "data")),
    ("pio_fifo_empty", 0x06, ("sm_id", "data")),
    ("dma_burst_begin", 0x08, ("channel", "data")),
    ("dma_burst_done", 0x09, ("channel", "data")),
    ("dma_underrun", 0x0A, ("channel", "data")),
    ("dma_overrun", 0x0B, ("channel", "data")),
    ("dma_trigger", 0x0C, ("channel", "data")),
    ("link_frame_error", 0x10, ("src", "data")),
    ("pio_dropped", DROPPED, ("src", "count")),
]
_ID_BY_CODE = {code: i for i, (_, code, _) in enumerate(EVENT_TYPES) if code is not None}


def event_fields(name_or_id):
    """Payload members of an event record type, in order: [(name, bits)]."""
    i = name_or_id if isinstance(name_or_id, int) else [n for n, _, _ in EVENT_TYPES].index(name_or_id)
    src, data = EVENT_TYPES[i][2]
    return [("tile_id", 8), (src, 8), ("event_type", 8), (data, 16)]


@dataclass(frozen=True)
class Record:
    ts: int          # 32-bit timestamp as recorded
    type: int
    src: int
    data: int

    @classmethod
    def from_u64(cls, r: int) -> "Record":
        return cls(r >> 32 & 0xFFFF_FFFF, r >> 24 & 0xFF, r >> 16 & 0xFF, r & 0xFFFF)

    def to_u64(self) -> int:
        return (self.ts & 0xFFFF_FFFF) << 32 | (self.type & 0xFF) << 24 | (self.src & 0xFF) << 16 | self.data & 0xFFFF


def records_from_words(words) -> list[Record]:
    """Link words (two per record, bits [31:0] first) to records."""
    words = list(words)
    if len(words) % 2:
        raise ValueError(f"{len(words)} words: a record is two words")
    return [Record.from_u64(words[i] | words[i + 1] << 32) for i in range(0, len(words), 2)]


def records_from_bytes(data: bytes) -> list[Record]:
    if len(data) % 8:
        raise ValueError(f"{len(data)} bytes: a record is 8 bytes")
    return records_from_words(struct.unpack(f"<{len(data) // 4}I", data))


def extend_timestamps(ts32, start: int = 0) -> list[int]:
    """32-bit timestamps in arrival order -> 64-bit. Each is placed nearest the previous one (within
    +-2^31 clocks), so wraps and the event FIFO's reordering are both handled. `start` is the
    extended value the first timestamp is placed near (default 0: the counter's first epoch). A
    record that would land before clock 0 raises ValueError; pass a larger `start` for such a capture."""
    out = []
    last = None
    for t in ts32:
        t &= 0xFFFF_FFFF
        if last is None:
            base = start & ~0xFFFF_FFFF
            ext = base | t
            if ext - start > 1 << 31:
                ext -= 1 << 32
            elif start - ext > 1 << 31:
                ext += 1 << 32
        else:
            delta = (t - last) & 0xFFFF_FFFF
            if delta >= 1 << 31:
                delta -= 1 << 32
            ext = last + delta
        out.append(ext)
        last = ext
    if out and min(out) < 0:
        raise ValueError("a record is older than clock 0 after extension: pass `start`")
    return out


def ordered(records, start: int = 0) -> list[tuple[int, Record]]:
    """(extended timestamp, record), sorted by time; records with the same time keep arrival order."""
    records = list(records)
    ext = extend_timestamps([r.ts for r in records], start)
    return sorted(zip(ext, records), key=lambda p: p[0])


# ---------------------------------------------------------------- CTF writer
def metadata(freq_hz: int = DEFAULT_FREQ_HZ, tile_count: int = 1) -> str:
    lines = [
        "/* CTF 1.8 */",
        "/* pio2 trace ([SPEC-TRC-02]); event record types from src/pio2/trace/barectf_config.yaml */",
        "",
        "typealias integer { size = 8; align = 8; signed = false; } := uint8_t;",
        "typealias integer { size = 16; align = 8; signed = false; } := uint16_t;",
        "typealias integer { size = 32; align = 8; signed = false; } := uint32_t;",
        "typealias integer { size = 64; align = 8; signed = false; } := uint64_t;",
        "",
        "trace {",
        "\tmajor = 1;",
        "\tminor = 8;",
        "\tbyte_order = le;",
        "\tpacket.header := struct {",
        "\t\tuint32_t magic;",
        "\t\tuint32_t stream_id;",
        "\t};",
        "};",
        "",
        "env {",
        '\tdomain = "pio2";',
        '\ttracer_name = "pio2";',
        f"\ttile_count = {tile_count};",
        "};",
        "",
        "clock {",
        "\tname = sys_clk;",
        f"\tfreq = {freq_hz};",
        '\tdescription = "PIO system clock (the event FIFO timestamp, extended to 64 bits)";',
        "\toffset = 0;",
        "\tabsolute = false;",
        "};",
        "",
        "typealias integer { size = 64; align = 8; signed = false; map = clock.sys_clk.value; } := sys_clk_t;",
        "",
        "stream {",
        "\tid = 0;",
        "\tpacket.context := struct {",
        "\t\tuint64_t packet_size;",
        "\t\tuint64_t content_size;",
        "\t\tsys_clk_t timestamp_begin;",
        "\t\tsys_clk_t timestamp_end;",
        "\t\tuint64_t events_discarded;",
        "\t\tuint64_t packet_seq_num;",
        "\t};",
        "\tevent.header := struct {",
        "\t\tuint16_t id;",
        "\t\tsys_clk_t timestamp;",
        "\t};",
        "};",
        "",
    ]
    ctype = {8: "uint8_t", 16: "uint16_t"}
    for i, (name, _, _) in enumerate(EVENT_TYPES):
        members = " ".join(f"{ctype[bits]} {f};" for f, bits in event_fields(i))
        lines += ["event {", f'\tname = "{name}";', f"\tid = {i};", "\tstream_id = 0;",
                  f"\tfields := struct {{ {members} }};", "};", ""]
    return "\n".join(lines)


_PKT_HEADER = struct.Struct("<II")
_PKT_CONTEXT = struct.Struct("<QQQQQQ")
_EVENT = struct.Struct("<HQBBBH")


def stream_bytes(records, start: int = 0, tile_id: int = 0, packet_events: int = 1024) -> bytes:
    """The binary data stream: packets of at most `packet_events` events, in timestamp order."""
    evs = ordered(records, start)
    out = bytearray()
    dropped = 0
    # Packet 0 is empty with events_discarded = 0: babeltrace2 reports a count only as the difference
    # to an earlier packet (for the first packet it says "may have discarded"), so a DROPPED record in
    # the first events still shows as an exact count.
    parts = [[]] + [evs[i:i + packet_events] for i in range(0, len(evs), packet_events)]
    first = evs[0][0] if evs else 0
    for seq, part in enumerate(parts):
        body = bytearray()
        for ts, r in part:
            body += _EVENT.pack(_ID_BY_CODE.get(r.type, 0), ts, tile_id, r.src, r.type, r.data)
            if r.type == DROPPED:
                dropped += r.data
        begin = part[0][0] if part else first
        end = part[-1][0] if part else first
        size = (_PKT_HEADER.size + _PKT_CONTEXT.size + len(body)) * 8
        out += _PKT_HEADER.pack(CTF_MAGIC, 0)
        out += _PKT_CONTEXT.pack(size, size, begin, end, dropped, seq)
        out += body
    return bytes(out)


def write_ctf(records, out_dir, freq_hz: int = DEFAULT_FREQ_HZ, start: int = 0, tile_id: int = 0,
              packet_events: int = 1024) -> Path:
    """Write `out_dir/metadata` and `out_dir/stream_0` (a CTF 1.8 trace babeltrace2 opens)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "metadata").write_text(metadata(freq_hz, max(tile_id + 1, 1)))
    (out / "stream_0").write_bytes(stream_bytes(records, start, tile_id, packet_events))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="pio2 trace records (raw link words) -> CTF 1.8 directory")
    ap.add_argument("records", help="binary file: 32-bit little-endian words, two per record, low word first")
    ap.add_argument("out_dir")
    ap.add_argument("--freq", type=int, default=DEFAULT_FREQ_HZ, help="system clock in Hz")
    ap.add_argument("--tile", type=int, default=0, help="tile_id written into every event")
    a = ap.parse_args(argv)
    recs = records_from_bytes(Path(a.records).read_bytes())
    write_ctf(recs, a.out_dir, a.freq, tile_id=a.tile)
    print(f"{len(recs)} records -> {a.out_dir} (babeltrace2 {a.out_dir})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
