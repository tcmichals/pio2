# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""
pio2_event_fifo ([SPEC-TRC-01]): producers never wait; one slot each; fixed priority; drop and count
with a DROPPED record; type mask; Forencich axis_fifo as storage.

Every enabled event is either delivered exactly once (with the timestamp of the clock it was offered)
or counted in a DROPPED record; per producer the order is kept; the DROPPED counts add up to
o_dropped_total; the FIFO drains back to level 0 (steady state).
"""
# cocotb-toplevel: pio2_event_fifo
# @verifies [SPEC-TRC-01] docs/INTERCONNECT_SPECIFICATION.md

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

N = 4
T_DROPPED = 0x1F


def unpack(rec):
    return {"data": rec & 0xFFFF, "src": (rec >> 16) & 0xFF, "type": (rec >> 24) & 0xFF, "ts": rec >> 32}


class Bench:
    """Drives producers, timestamp and TREADY once per clock and collects handshaken records."""

    def __init__(self, dut):
        self.dut = dut
        self.ts = 0
        self.out = []
        self.snap = (0, 0, 0)            # (tvalid, tready, tdata) settled before the next edge
        self.stable_errors = 0

    async def start(self, mask=0xFFFF_FFFF):
        d = self.dut
        cocotb.start_soon(Clock(d.clk, 10, unit="ns").start())
        d.rst_n.value = 0
        d.i_type_mask.value = mask
        d.i_timestamp.value = 0
        d.ev_valid.value = 0
        d.ev_type.value = 0
        d.ev_src.value = 0
        d.ev_data.value = 0
        d.m_axis_tready.value = 0
        await ClockCycles(d.clk, 4)
        d.rst_n.value = 1
        await RisingEdge(d.clk)

    async def clock(self, events=(), tready=1):
        """events: list of (producer, type, src, data) offered in this clock. Returns the timestamp
        those events carry."""
        d = self.dut
        v = t = s = dt = 0
        for p, ty, sr, da in events:
            v |= 1 << p
            t |= ty << (8 * p)
            s |= sr << (8 * p)
            dt |= da << (16 * p)
        d.ev_valid.value = v
        d.ev_type.value = t
        d.ev_src.value = s
        d.ev_data.value = dt
        d.i_timestamp.value = self.ts
        d.m_axis_tready.value = tready
        stamp = self.ts
        await ReadOnly()
        prev_valid, prev_ready, prev_data = self.snap
        valid, data = int(d.m_axis_tvalid.value), int(d.m_axis_tdata.value) if int(d.m_axis_tvalid.value) else 0
        if prev_valid and not prev_ready and (not valid or data != prev_data):
            self.stable_errors += 1      # TVALID dropped or TDATA changed before TREADY
        self.snap = (valid, tready, data)
        await RisingEdge(d.clk)
        if valid and tready:
            self.out.append(unpack(data))
        self.ts = (self.ts + 1) & 0xFFFF_FFFF
        return stamp

    async def drain(self, max_clocks=5000):
        for _ in range(max_clocks):
            await self.clock(tready=1)
            if not int(self.dut.m_axis_tvalid.value) and int(self.dut.o_level.value) == 0:
                for _ in range(8):           # slots and the output stage
                    await self.clock(tready=1)
                if not int(self.dut.m_axis_tvalid.value):
                    return
        raise AssertionError("event FIFO did not drain")


@cocotb.test(timeout_time=5, timeout_unit="ms")
async def test_record_fields_and_timestamp(dut):
    b = Bench(dut)
    await b.start()
    stamp = await b.clock([(2, 0x01, 3, 0xBEEF)])
    await b.drain()
    assert b.out == [{"data": 0xBEEF, "src": 3, "type": 0x01, "ts": stamp}], b.out


@cocotb.test(timeout_time=5, timeout_unit="ms")
async def test_priority_same_clock(dut):
    b = Bench(dut)
    await b.start()
    await b.clock([(p, 0x05, p, 0x100 + p) for p in (3, 1, 0, 2)])
    await b.drain()
    assert [r["src"] for r in b.out] == [0, 1, 2, 3], b.out
    assert int(dut.o_dropped_total.value) == 0


@cocotb.test(timeout_time=5, timeout_unit="ms")
async def test_mask_and_reserved_types(dut):
    b = Bench(dut)
    await b.start(mask=1 << 0x03)
    await b.clock([(0, 0x03, 0, 1), (1, 0x04, 0, 2), (2, 0x23, 0, 3)])   # 0x04 masked, 0x23 reserved
    for _ in range(20):                                                  # masked: never dropped either
        await b.clock([(1, 0x04, 0, 9)], tready=0)
    await b.drain()
    assert [(r["type"], r["data"]) for r in b.out] == [(0x03, 1)], b.out
    assert int(dut.o_dropped_total.value) == 0


@cocotb.test(timeout_time=20, timeout_unit="ms")
async def test_drop_count_and_dropped_record(dut):
    """FIFO blocked: the FIFO, output stage and slot fill, further events are dropped and counted,
    and the DROPPED record comes out when the slots are idle, before records offered later."""
    b = Bench(dut)
    await b.start()
    depth = int(dut.DEPTH.value)
    offered = 0
    for i in range(depth + 40):
        await b.clock([(2, 0x01, 0, i)], tready=0)
        offered += 1
    for _ in range(4):                          # let the DROPPED record and the slot drain first
        await b.clock(tready=1)
    for i in range(5):
        await b.clock([(2, 0x02, 0, 0x8000 + i)], tready=1)
    await b.drain()
    total = int(dut.o_dropped_total.value)
    assert total > 0, "no drops with the FIFO blocked"
    kinds = [r["type"] for r in b.out]
    assert kinds.count(T_DROPPED) == 1, kinds
    k = kinds.index(T_DROPPED)
    assert b.out[k]["data"] == total
    kept = [r for r in b.out if r["type"] == 0x01]
    assert len(kept) + total == offered, (len(kept), total, offered)
    assert [r["data"] for r in kept] == list(range(len(kept))), "kept records are the first ones, in order"
    # DROPPED is written once the slots are idle: after the record held in producer 2's slot, before
    # everything offered after the blockage; its timestamp is the clock of the first loss.
    assert kinds[k - 1] == 0x01 and kinds[k + 1:] == [0x02] * 5, kinds[k - 2:]
    assert b.out[k]["ts"] < b.out[k - 1]["ts"] + 40 and b.out[k]["ts"] > kept[0]["ts"]


@cocotb.test(timeout_time=200, timeout_unit="ms")
async def test_random_conservation_1000(dut):
    """Random producers, types and back-pressure: delivered + dropped == offered, nothing duplicated,
    per-producer order kept, timestamps right, TDATA stable under back-pressure, level returns to 0."""
    b = Bench(dut)
    await b.start()
    rng = random.Random(29)
    seq = [0] * N
    sent = {}                                   # (producer, seq) -> expected record
    for rnd in range(3):
        burst_p = rng.choice([0.05, 0.4, 0.9])  # quiet, busy, overloaded phases
        for _ in range(1000):
            events = []
            for p in range(N):
                if rng.random() < burst_p:
                    ty = rng.choice([0x01, 0x02, 0x03, 0x08, 0x0A, 0x10])
                    events.append((p, ty, p, seq[p] & 0xFFFF))
            ready = 1 if rng.random() < 0.6 else 0
            stamp = await b.clock(events, tready=ready)
            for p, ty, sr, da in events:
                sent[(p, seq[p])] = {"data": da, "src": sr, "type": ty, "ts": stamp}
                seq[p] += 1
        await b.drain()
        assert int(dut.o_level.value) == 0, f"round {rnd}: level {int(dut.o_level.value)} after drain"
    dropped_records = sum(r["data"] for r in b.out if r["type"] == T_DROPPED)
    total = int(dut.o_dropped_total.value)
    assert dropped_records == total, f"DROPPED records sum {dropped_records} != total {total}"
    got = [r for r in b.out if r["type"] != T_DROPPED]
    assert len(got) + total == len(sent), f"{len(got)} delivered + {total} dropped != {len(sent)} offered"
    last = [-1] * N
    for r in got:
        p = r["src"]
        # find this producer's next sequence number that matches (data wraps at 16 bits)
        s = last[p] + 1
        while s < seq[p] and sent[(p, s)]["data"] != r["data"]:
            s += 1
        assert s < seq[p], f"record {r} was never offered (or duplicated)"
        assert sent[(p, s)] == r, f"record {r} != offered {sent[(p, s)]}"
        last[p] = s
    assert b.stable_errors == 0, f"{b.stable_errors} TVALID/TDATA changes before TREADY"
    assert total > 0 and len(got) > 500, f"test did not exercise both paths ({len(got)} delivered, {total} dropped)"
    # Reporting losses must not cause losses: under overload DROPPED records may not crowd out real
    # ones (the first design wrote DROPPED first and delivered 396 records for 3544 lost).
    n_dropped_records = sum(1 for r in b.out if r["type"] == T_DROPPED)
    assert n_dropped_records * 4 < len(got), f"{n_dropped_records} DROPPED records vs {len(got)} delivered"
