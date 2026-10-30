# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""
pio2_axil_regs, the AXI4-Lite front end of pio2's register blocks ([SPEC-AXI-01]).

* Back to back: with READY / VALID always high, one write and one read are taken every clock.
* 1000 random writes and reads with random stalls on all five channels: every write strobes o_wr
  once with its address and data, every read strobes o_rd once and returns i_rdata of that clock.
* A partial WSTRB answers SLVERR and gives no o_wr.
"""
# cocotb-toplevel: pio2_axil_regs
# @verifies [SPEC-AXI-01] docs/INTERCONNECT_SPECIFICATION.md

import itertools
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, Edge, First, RisingEdge
from cocotbext.axi import AxiLiteBus, AxiLiteMaster


async def start(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst_n.value = 0
    dut.i_rdata.value = 0
    m = AxiLiteMaster(AxiLiteBus.from_prefix(dut, "s_axil"), dut.clk, dut.rst_n, reset_active_level=False)
    await ClockCycles(dut.clk, 3)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)
    return m


class Regs:
    """Model register block: 4096 bytes of words; i_rdata follows o_raddr combinationally."""

    def __init__(self, dut):
        self.dut, self.mem, self.wr, self.rd = dut, {}, [], 0

    async def run(self):
        while True:
            await RisingEdge(self.dut.clk)
            if int(self.dut.o_wr.value):
                a, d = int(self.dut.o_waddr.value), int(self.dut.o_wdata.value)
                self.wr.append((a, d))
                self.mem[a] = d
            if int(self.dut.o_rd.value):
                self.rd += 1

    async def data(self):
        """i_rdata follows o_raddr (and the written words) with no clock in between."""
        while True:
            a = int(self.dut.o_raddr.value)
            self.dut.i_rdata.value = self.mem.get(a, 0xDEAD_0000 | a)
            await First(Edge(self.dut.o_raddr), RisingEdge(self.dut.clk))


@cocotb.test(timeout_time=10, timeout_unit="ms")
async def test_one_write_and_one_read_per_clock(dut):
    for sig, v in [("awvalid", 1), ("wvalid", 1), ("bready", 1), ("arvalid", 1), ("rready", 1),
                   ("wstrb", 0xF)]:
        getattr(dut, f"s_axil_{sig}").value = v
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 3)
    dut.rst_n.value = 1
    wr = rd = 0
    for k in range(100):
        dut.s_axil_awaddr.value = 4 * k
        dut.s_axil_wdata.value = k
        dut.s_axil_araddr.value = 4 * k
        await RisingEdge(dut.clk)
        wr += int(dut.o_wr.value)
        rd += int(dut.o_rd.value)
    assert wr >= 99 and rd >= 99, f"{wr} writes and {rd} reads in 100 clocks"


@cocotb.test(timeout_time=50, timeout_unit="ms")
async def test_random_traffic_with_stalls_1000(dut):
    m = await start(dut)
    regs = Regs(dut)
    cocotb.start_soon(regs.run())
    cocotb.start_soon(regs.data())
    rng = random.Random(4)
    for ch in (m.write_if.aw_channel, m.write_if.w_channel, m.write_if.b_channel,
               m.read_if.ar_channel, m.read_if.r_channel):
        ch.set_pause_generator(itertools.cycle([rng.random() < 0.4 for _ in range(997)]))
    model, nwr, nrd = {}, 0, 0
    for i in range(1000):
        a = 4 * rng.randrange(64)
        if rng.random() < 0.5:
            v = rng.getrandbits(32)
            r = await m.write(a, v.to_bytes(4, "little"))
            assert r.resp == 0
            model[a] = v
            nwr += 1
        else:
            r = await m.read(a, 4)
            assert r.resp == 0
            got = int.from_bytes(r.data, "little")
            assert got == model.get(a, 0xDEAD_0000 | a), f"#{i} read 0x{a:x}: {got:#x}"
            nrd += 1
    await ClockCycles(dut.clk, 2)
    assert len(regs.wr) == nwr and regs.rd == nrd, "a strobe fired twice or was missing"


@cocotb.test(timeout_time=10, timeout_unit="ms")
async def test_partial_write_is_slverr_without_effect(dut):
    m = await start(dut)
    regs = Regs(dut)
    cocotb.start_soon(regs.run())
    cocotb.start_soon(regs.data())
    r = await m.write(0x10, b"\x01\x02")
    assert r.resp == 2
    await ClockCycles(dut.clk, 2)
    assert regs.wr == [], "a partial write strobed o_wr"
