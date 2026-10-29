# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""
The PIO tile's AXI4-Lite register port ([SPEC-AXI-04]).

* 1000 random register writes and reads with random stalls on all five channels, checked against
  a model of the plain read/write registers.
* Write address before data, data before address, and both together (driven by hand).
* A partial-strobe write answers SLVERR and changes nothing.
* Side effects happen exactly once: under back-pressure, every TX FIFO write pushes one word and
  every RX FIFO read pops one (SM0 echoes TX into RX; order and count checked).
"""
# @verifies [SPEC-AXI-04] docs/INTERCONNECT_SPECIFICATION.md

import itertools
import random

import cocotb
from cocotb.triggers import ClockCycles, RisingEdge
from cocotbext.axi import AxiLiteBus, AxiLiteMaster

from test_asp_pio_cocotb import (IRAM_BASE, PIO_BASE, PIO_MOV, PIO_PULL, PIO_PUSH, REG_CTRL,
                                 REG_FIFO_STAT, SM_CLKDIV, SM_EXECCTRL, SM_PINCTRL, SM_RXF,
                                 SM_SHIFTCTRL, SM_TXF, init_tile)

# Plain read/write registers (no side effects): SMx_SHIFTCTRL and SMx_PINCTRL for all four SMs.
PLAIN = [f(n) for n in range(4) for f in (SM_SHIFTCTRL, SM_PINCTRL)]


def random_pauses(rng, p):
    return itertools.cycle([rng.random() < p for _ in range(997)])


def stalled_master(dut, seed, p=0.4):
    m = AxiLiteMaster(AxiLiteBus.from_prefix(dut, "s_axil"), dut.clk, dut.rst_n, reset_active_level=False)
    rng = random.Random(seed)
    for ch in (m.write_if.aw_channel, m.write_if.w_channel, m.read_if.ar_channel):
        ch.set_pause_generator(random_pauses(rng, p))
    for ch in (m.write_if.b_channel, m.read_if.r_channel):
        ch.set_pause_generator(random_pauses(rng, p))
    return m


async def write(m, off, val, nbytes=4):
    """A write of nbytes at a word address: cocotbext-axi sets WSTRB from the length (2 -> 0x3)."""
    return await m.write(PIO_BASE + off, int(val).to_bytes(4, "little")[:nbytes])


async def read(m, off):
    r = await m.read(PIO_BASE + off, 4)
    return r.resp, int.from_bytes(r.data, "little")


@cocotb.test(timeout_time=20, timeout_unit="ms")   # a hung handshake fails, never blocks
async def test_random_traffic_with_stalls_1000(dut):
    await init_tile(dut)
    m = stalled_master(dut, 1)
    rng = random.Random(7)
    model = {}
    for off in PLAIN:                       # reset values are part of the model
        _, model[off] = await read(m, off)
    for i in range(1000):
        off = rng.choice(PLAIN)
        if rng.random() < 0.5:
            v = rng.getrandbits(32)
            r = await write(m, off, v)
            assert r.resp == 0, f"#{i} write response {r.resp}"
            model[off] = v
        else:
            resp, v = await read(m, off)
            assert resp == 0 and v == model[off], f"#{i} read 0x{off:03X}: {v:#x} != {model[off]:#x}"


async def drive_write(dut, off, val, order):
    """Drive one write by hand: order is 'aw_first', 'w_first' or 'together'. Returns BRESP."""
    dut.s_axil_bready.value = 1
    dut.s_axil_wstrb.value = 0xF

    async def aw():
        dut.s_axil_awaddr.value = PIO_BASE + off
        dut.s_axil_awvalid.value = 1
        while True:
            await RisingEdge(dut.clk)
            if int(dut.s_axil_awready.value):
                break
        dut.s_axil_awvalid.value = 0

    async def w():
        dut.s_axil_wdata.value = val
        dut.s_axil_wvalid.value = 1
        while True:
            await RisingEdge(dut.clk)
            if int(dut.s_axil_wready.value):
                break
        dut.s_axil_wvalid.value = 0

    if order == "aw_first":
        await aw()
        await ClockCycles(dut.clk, 3)
        await w()
    elif order == "w_first":
        await w()
        await ClockCycles(dut.clk, 3)
        await aw()
    else:
        t = cocotb.start_soon(aw())
        await w()
        await t
    for _ in range(20):
        await RisingEdge(dut.clk)
        if int(dut.s_axil_bvalid.value):
            resp = int(dut.s_axil_bresp.value)
            await RisingEdge(dut.clk)
            return resp
    raise AssertionError(f"no write response ({order})")


@cocotb.test(timeout_time=20, timeout_unit="ms")   # a hung handshake fails, never blocks
async def test_write_address_and_data_in_any_order(dut):
    await init_tile(dut)
    for sig in ("awvalid", "wvalid", "arvalid", "bready", "rready"):
        getattr(dut, f"s_axil_{sig}").value = 0
    await ClockCycles(dut.clk, 2)
    for i, order in enumerate(("aw_first", "w_first", "together")):
        off = SM_PINCTRL(i)
        assert await drive_write(dut, off, 0x1234_0000 + i, order) == 0, order
    m = AxiLiteMaster(AxiLiteBus.from_prefix(dut, "s_axil"), dut.clk, dut.rst_n, reset_active_level=False)
    for i in range(3):
        resp, v = await read(m, SM_PINCTRL(i))
        assert resp == 0 and v == 0x1234_0000 + i


@cocotb.test(timeout_time=20, timeout_unit="ms")   # a hung handshake fails, never blocks
async def test_partial_strobe_is_slverr_and_no_change(dut):
    await init_tile(dut)
    m = stalled_master(dut, 3, p=0.2)
    assert (await write(m, SM_EXECCTRL(0), 0x0001_F000)).resp == 0
    _, before = await read(m, SM_EXECCTRL(0))
    r = await write(m, SM_EXECCTRL(0), 0xFFFF_FFFF, nbytes=2)   # WSTRB = 0x3
    assert r.resp == 2, f"partial write answered {r.resp}, expected SLVERR (2)"
    _, after = await read(m, SM_EXECCTRL(0))
    assert after == before, f"partial write changed the register: {before:#x} -> {after:#x}"


@cocotb.test(timeout_time=20, timeout_unit="ms")   # a hung handshake fails, never blocks
async def test_side_effect_once_per_transaction(dut):
    """Each TX FIFO write pushes exactly one word and each RX FIFO read pops exactly one, however the
    handshakes are stalled: SM0 echoes every TX word into its RX FIFO (PULL; MOV ISR, OSR; PUSH), the
    host writes 4 words and reads 4 back in order, then RX is empty. 200 rounds."""
    await init_tile(dut)
    m = stalled_master(dut, 5, p=0.5)
    prog = [PIO_PULL(block=1), PIO_MOV(dest=6, op=0, src=7), PIO_PUSH(block=1)]
    for i, inst in enumerate(prog):
        assert (await write(m, IRAM_BASE + 4 * i, inst)).resp == 0
    assert (await write(m, SM_EXECCTRL(0), (2 << 12) | (0 << 7))).resp == 0   # wrap 0..2
    assert (await write(m, SM_CLKDIV(0), 1 << 16)).resp == 0
    assert (await write(m, REG_CTRL, 0x1 | (0x1 << 4))).resp == 0           # enable + restart SM0
    for rnd in range(200):
        words = [(rnd << 8) | k for k in range(4)]
        for w in words:
            assert (await write(m, SM_TXF(0), w)).resp == 0
        await ClockCycles(dut.clk, 20)
        got = []
        for _ in range(4):
            resp, v = await read(m, SM_RXF(0))
            assert resp == 0
            got.append(v)
        assert got == words, f"round {rnd}: echoed {got} != written {words}"
        _, st = await read(m, REG_FIFO_STAT)
        assert (st >> 28) & 1, f"round {rnd}: RX not empty after 4 reads (status {st:#x}): a read popped twice or a write pushed twice"
