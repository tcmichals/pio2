# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trace records -> CTF 1.8, read back with babeltrace2 ([SPEC-TRC-02])."""
# @verifies [SPEC-TRC-02] docs/INTERCONNECT_SPECIFICATION.md

import random
import re
import shutil
import struct
import subprocess
from pathlib import Path

import pytest
import yaml

from pio2.trace import ctf
from pio2.trace.ctf import DROPPED, Record, extend_timestamps, records_from_bytes, records_from_words, write_ctf

BT = shutil.which("babeltrace2")
needs_bt = pytest.mark.skipif(BT is None, reason="babeltrace2 not installed")
CONFIG = Path(ctf.__file__).with_name("barectf_config.yaml")
LINE = re.compile(r"^\[(\d+)\] \S+ (\w+): \{ (.*) \}$")


def read_back(trace_dir):
    """babeltrace2 --clock-cycles: [(cycle, event name, {field: value})] and its other lines (the
    discarded-events warnings go to stderr)."""
    proc = subprocess.run([BT, "--clock-cycles", str(trace_dir)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    events, other = [], proc.stderr.splitlines()
    for line in proc.stdout.splitlines():
        m = LINE.match(line)
        if m:
            fields = dict(kv.split(" = ") for kv in m.group(3).split(", "))
            events.append((int(m.group(1)), m.group(2), {k: int(v) for k, v in fields.items()}))
        else:
            other.append(line)
    return events, other


# ---------------------------------------------------------------- records and timestamps
def test_record_layout_and_link_words():
    r = Record(ts=0x8765_4321, type=0x03, src=0xFF, data=0x0005)
    assert r.to_u64() == 0x8765_4321_03FF_0005
    assert Record.from_u64(r.to_u64()) == r
    assert records_from_words([0x03FF_0005, 0x8765_4321]) == [r]          # bits [31:0] first
    assert records_from_bytes(struct.pack("<II", 0x03FF_0005, 0x8765_4321)) == [r]
    with pytest.raises(ValueError):
        records_from_words([1, 2, 3])


def test_extend_timestamps_wraps_and_reordering():
    assert extend_timestamps([10, 20, 30]) == [10, 20, 30]
    m = 1 << 32
    # forward across the wrap; an older record (priority order) just before it; then forward again
    ts = [m - 100, m - 10, 5, m - 50, 40, 0x8000_0000 + 30]
    assert extend_timestamps(ts, start=m - 100) == [m - 100, m - 10, m + 5, m - 50, m + 40, m + 0x8000_0000 + 30]
    # many wraps: every step < 2^31 forward
    true = list(range(0, 10 * m, 0x7000_0000))
    assert extend_timestamps([t & (m - 1) for t in true]) == true
    with pytest.raises(ValueError):
        extend_timestamps([5, m - 5])                                       # 10 clocks before clock 0


def test_ordered_sorts_by_extended_time_stable():
    rs = [Record(30, 1, 0, 0), Record(10, 0x1F, 0, 2), Record(30, 2, 0, 0), Record(20, 3, 0xFF, 1)]
    assert [r for _, r in ctf.ordered(rs)] == [rs[1], rs[3], rs[0], rs[2]]


def test_barectf_config_matches_the_writer():
    """The barectf 3 YAML and ctf.EVENT_TYPES name the same event record types, in the same order,
    with the same members; the PIO type code in each YAML comment matches too."""
    loader = type("L", (yaml.SafeLoader,), {})
    loader.add_constructor("tag:barectf.org,2020/3/config", lambda ld, node: ld.construct_mapping(node, deep=True))
    cfg = yaml.load(CONFIG.read_text(), Loader=loader)
    ds = cfg["trace"]["type"]["data-stream-types"]["pio2"]
    assert ds["$is-default"] and ds["$default-clock-type-name"] in cfg["trace"]["type"]["clock-types"]
    ert = ds["event-record-types"]
    assert list(ert) == [n for n, _, _ in ctf.EVENT_TYPES]
    aliases = cfg["trace"]["type"]["$field-type-aliases"]
    for i, (name, _, _) in enumerate(ctf.EVENT_TYPES):
        members = [next(iter(m.items())) for m in ert[name]["payload-field-type"]["members"]]
        assert [(f, aliases[t]["size"]) for f, t in members] == ctf.event_fields(i), name
    codes = dict(re.findall(r"^\s{10}(\w+):\s+# (0x[0-9A-F]{2})", CONFIG.read_text(), re.M))
    assert {n: int(c, 16) for n, c in codes.items()} == {n: c for n, c, _ in ctf.EVENT_TYPES if c is not None}
    # AbstractX pio_event names
    assert ctf.event_fields("pio_stall_begin")[:3] == [("tile_id", 8), ("sm_id", 8), ("event_type", 8)]
    assert ctf.event_fields("pio_stall_begin")[3][0] == "pc"


# ---------------------------------------------------------------- CTF read back with babeltrace2
@needs_bt
def test_every_type_read_back(tmp_path):
    codes = [c for _, c, _ in ctf.EVENT_TYPES if c is not None] + [0x07, 0x1E]
    recs = [Record(100 + 7 * i, c, i, 0x1000 + i) for i, c in enumerate(codes)]
    write_ctf(recs, tmp_path)
    events, _ = read_back(tmp_path)
    assert len(events) == len(recs)
    for (cyc, name, f), r in zip(events, recs):
        i = ctf._ID_BY_CODE.get(r.type, 0)
        assert name == ctf.EVENT_TYPES[i][0]
        fields = ctf.event_fields(i)
        assert cyc == r.ts and f == {fields[0][0]: 0, fields[1][0]: r.src, fields[2][0]: r.type, fields[3][0]: r.data}
    assert [n for _, n, _ in events][-2:] == ["pio_event", "pio_event"]                # 0x07, 0x1E: generic


@needs_bt
def test_wrapped_unordered_records_with_drops(tmp_path):
    """3000 records over 3 timestamp wraps, delivered in a locally shuffled (priority) order, with
    DROPPED records: babeltrace2 sees them in time order with 64-bit times, every one once, and
    reports the discarded count."""
    rng = random.Random(7)
    m = 1 << 32
    times = sorted(rng.sample(range(5, 3 * m + 12345), 3000))
    true = []
    for k, t in enumerate(times):
        typ = DROPPED if k % 500 in (3, 499) else rng.choice([1, 2, 3, 4, 5, 6])
        true.append((t, Record(t & (m - 1), typ, rng.randrange(4) if typ != 3 else 0xFF,
                               rng.randrange(1, 20) if typ == DROPPED else rng.randrange(32))))
    arrival = [r for _, r in true]
    for i in range(0, len(arrival) - 4, 4):                     # priority order: swap neighbours
        arrival[i], arrival[i + 2] = arrival[i + 2], arrival[i]
    write_ctf(arrival, tmp_path, freq_hz=27_000_000, packet_events=256)
    events, other = read_back(tmp_path)
    assert [c for c, _, _ in events] == [t for t, _ in true]
    assert [f["event_type"] for _, _, f in events] == [r.type for _, r in true]
    lost = sum(r.data for _, r in true if r.type == DROPPED)
    assert sum(f["count"] for _, n, f in events if n == "pio_dropped") == lost
    reported = sum(int(m.group(1)) for line in other for m in [re.search(r"discarded (\d+) events", line)] if m)
    assert reported == lost, other
    assert "freq = 27000000;" in (tmp_path / "metadata").read_text()


@needs_bt
def test_cli_from_link_words(tmp_path):
    words = []
    for r in [Record(5, 1, 0, 3), Record(9, 2, 0, 3)]:
        u = r.to_u64()
        words += [u & 0xFFFF_FFFF, u >> 32]
    src = tmp_path / "records.bin"
    src.write_bytes(struct.pack(f"<{len(words)}I", *words))
    assert ctf.main([str(src), str(tmp_path / "ctf")]) == 0
    events, _ = read_back(tmp_path / "ctf")
    assert [(c, n, f["pc"]) for c, n, f in events] == [(5, "pio_stall_begin", 3), (9, "pio_stall_end", 3)]


# ---------------------------------------------------------------- end to end: RTL records -> CTF
@needs_bt
def test_debugger_session_records_to_ctf(tmp_path):
    """The debugger runs the RTL (pio2_core: tile, trace tap, event FIFO, [SPEC-TRC-03]): blink on SM 0
    stalls on its `pull`, an IRQ toggler on SM 1 makes a steady stream, and a stretch with the reader
    not taking records makes the FIFO drop and write a DROPPED record (timestamp: the first lost event).
    Out-of-order arrival is covered by test_wrapped_unordered_records_with_drops. Records -> link words -> CTF -> babeltrace2: the same
    records, sorted by time, with the RTL's timestamps, and the loss reported as discarded events."""
    from pio2.asm import assemble
    from pio2.debug.session import Pins, Session

    with Session() as s:
        s.load("blink", sm=0, pinctrl=Pins(set_base=25, set_count=1))
        s.load(assemble(".program t\nirq set 0\nirq clear 0 [2]")["programs"][0], sm=1, offset=8)
        s.step(10)
        s.push(0, 3)
        s.step(2000)
        s.trace_config(ready=False)
        s.step(5000)
        s.trace_config(ready=True)
        s.step(2000)
        evs = list(s.events)
        lost, _ = s.trace_counters()
    assert len(evs) > 2000 and lost > 0
    dropped = [e for e in evs if e.type == DROPPED]
    assert len(dropped) == 1 and dropped[0].data == min(lost, 0xFFFF)
    words = []
    for e in evs:                                       # as they come over the link: two words each
        u = Record(e.ts, e.type, e.src, e.data).to_u64()
        words += [u & 0xFFFF_FFFF, u >> 32]
    write_ctf(records_from_words(words), tmp_path, packet_events=200)
    events, other = read_back(tmp_path)
    got = [(c,) + tuple(list(f.values())[1:]) for c, _, f in events]
    assert got == sorted(((e.ts, e.src, e.type, e.data) for e in evs), key=lambda t: t[0])
    names = [n for _, n, _ in events]
    assert names.count("pio_stall_begin") == 1 and names.count("pio_stall_end") == 1
    assert names.count("pio_irq_set") > 1000 and names.count("pio_dropped") == 1
    assert [f["pc"] for _, n, f in events if n == "pio_stall_begin"] == [0], "blink stalls at `pull`"
    reported = sum(int(m.group(1)) for line in other for m in [re.search(r"discarded (\d+) events", line)] if m)
    assert reported == dropped[0].data, other
