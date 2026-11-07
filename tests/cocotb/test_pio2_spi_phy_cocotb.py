# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""
pio2_spi_phy ([SPEC-LINK-01]) alone, with a host model on the pins: single and dual, both directions,
SCK slower and faster than f_sys, at the host timing limits (CS_n low to first SCK rising edge exactly
3 f_sys clocks, CS_n high exactly 8), and the uses of CS_n the lint rule SYNCASYNCNET points at: async
reset of the SCK counters, and the guard in the rising-edge block.
"""
# cocotb-toplevel: pio2_spi_phy
# @verifies [SPEC-LINK-01] docs/INTERCONNECT_SPECIFICATION.md

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge, Timer

WRITE, READ, DUAL = 0x01, 0x02, 0x80
ISR = 0xA5


def tx_pattern(buf, i):
    return (i * 3 + 1 + buf * 0x80) & 0xFF


class Bench:
    def __init__(self, dut, period_ps=20_000):
        self.dut = dut
        self.t = period_ps
        self.wins = []

    async def start(self):
        d = self.dut
        Clock(d.clk, self.t, unit="ps").start()
        d.sck.value = 0
        d.cs_n.value = 1
        d.io0_i.value = 0
        d.io1_i.value = 0
        d.i_isr.value = ISR
        d.i_rx_ok.value = 1
        d.i_rx_sel.value = 0
        d.i_tx_valid.value = 0
        d.i_tx_sel.value = 0
        d.i_tx_len0.value = 0
        d.i_tx_len1.value = 0
        d.i_rx_rbuf.value = 0
        d.i_rx_raddr.value = 0
        d.i_tx_we.value = 0
        d.i_tx_wbuf.value = 0
        d.i_tx_waddr.value = 0
        d.i_tx_wdata.value = 0
        d.i_tx_raddr1.value = 0
        await self.reset()
        cocotb.start_soon(self.monitor())

    async def reset(self):
        self.dut.rst_n.value = 0
        await ClockCycles(self.dut.clk, 4)
        self.dut.rst_n.value = 1
        await ClockCycles(self.dut.clk, 4)

    async def monitor(self):
        d = self.dut
        while True:
            await RisingEdge(d.clk)
            if d.o_win.value.is_resolvable and int(d.o_win.value):
                self.wins.append({"cmd": int(d.o_win_cmd.value), "count": int(d.o_win_count.value),
                                  "rx_ok": int(d.o_win_rx_ok.value), "rx_sel": int(d.o_win_rx_sel.value),
                                  "tx_valid": int(d.o_win_tx_valid.value),
                                  "tx_sel": int(d.o_win_tx_sel.value)})

    async def fill_tx(self):
        d = self.dut
        for buf in (0, 1):
            for i in range(128):
                d.i_tx_we.value = 1
                d.i_tx_wbuf.value = buf
                d.i_tx_waddr.value = i
                d.i_tx_wdata.value = tx_pattern(buf, i)
                await RisingEdge(d.clk)
        d.i_tx_we.value = 0
        await RisingEdge(d.clk)

    async def read_rx(self, buf, n):
        d = self.dut
        out = []
        d.i_rx_rbuf.value = buf
        for i in range(n):
            d.i_rx_raddr.value = i
            await RisingEdge(d.clk)
            await Timer(1, unit="ps")
            out.append(int(d.o_rx_rdata.value))
        return out

    async def clock_bit(self, half, io0=None, io1=None):
        """One SCK clock: lines set, half low, sample at the rising edge, half high, falling edge.
        Returns the lines as the host samples them at the rising edge."""
        d = self.dut
        if io0 is not None:
            d.io0_i.value = io0
        if io1 is not None:
            d.io1_i.value = io1
        await Timer(half, unit="ps")
        got = (int(d.io1_o.value), int(d.io0_o.value), int(d.io1_oe.value), int(d.io0_oe.value))
        d.sck.value = 1
        await Timer(half, unit="ps")
        d.sck.value = 0
        return got

    async def window(self, cmd, data=b"", nread=0, half=100_000, lead=None, tail=None):
        """A CS_n window. `lead`: CS_n low to the first rising edge (default half); `tail`: last falling
        edge to CS_n high (0: the same instant). Returns (isr byte, bytes read, oe samples of the data)."""
        d = self.dut
        lead = half if lead is None else lead
        tail = half if tail is None else tail
        d.cs_n.value = 0
        await Timer(max(lead - half, 1), unit="ps")
        isr = 0
        dual = bool(cmd & DUAL)
        for b in range(7, -1, -1):
            io1, _, oe1, oe0 = await self.clock_bit(half, io0=(cmd >> b) & 1)
            assert oe1 == 1 and oe0 == 0, f"command byte bit {b}: oe1={oe1} oe0={oe0}"
            isr = (isr << 1) | io1
        out = []
        oes = []
        if cmd & 0x7F == WRITE:
            for byte in data:
                if dual:
                    for b in (6, 4, 2, 0):
                        got = await self.clock_bit(half, io0=(byte >> b) & 1, io1=(byte >> (b + 1)) & 1)
                        oes.append(got[2:])
                else:
                    for b in range(7, -1, -1):
                        got = await self.clock_bit(half, io0=(byte >> b) & 1)
                        oes.append(got[2:])
        elif cmd & 0x7F == READ:
            if dual:
                got = await self.clock_bit(half)           # turnaround: nobody drives
                assert got[2:] == (0, 0), f"turnaround: oe {got[2:]}"
            for _ in range(nread):
                v = 0
                if dual:
                    for _ in range(4):
                        got = await self.clock_bit(half)
                        v = (v << 2) | (got[0] << 1) | got[1]
                        oes.append(got[2:])
                else:
                    for _ in range(8):
                        got = await self.clock_bit(half)
                        v = (v << 1) | got[0]
                        oes.append(got[2:])
                out.append(v)
        if tail:
            await Timer(tail, unit="ps")
        d.cs_n.value = 1
        return isr, out, oes

    async def settle(self):
        await ClockCycles(self.dut.clk, 8)

    async def gap(self):
        """CS_n high for exactly 8 f_sys clocks ([SPEC-LINK-01], the limit)."""
        await Timer(8 * self.t, unit="ps")


@cocotb.test()
async def single_write_lands_in_the_chosen_buffer_and_is_reported(dut):
    b = Bench(dut)
    await b.start()
    data = bytes(random.randrange(256) for _ in range(40))
    for sel in (0, 1):
        dut.i_rx_sel.value = sel
        await ClockCycles(dut.clk, 2)
        isr, _, oes = await b.window(WRITE, data)
        assert isr == ISR, f"ISR during the command byte: {isr:#x}"
        assert all(o == (1, 0) for o in oes), "single write: FPGA drives IO1 only"
        await b.settle()
        assert b.wins[-1] == {"cmd": WRITE, "count": 40, "rx_ok": 1, "rx_sel": sel, "tx_valid": 0, "tx_sel": 0}
        assert bytes(await b.read_rx(sel, 40)) == data
    assert len(b.wins) == 2


@cocotb.test()
async def rx_not_ok_writes_nothing(dut):
    b = Bench(dut)
    await b.start()
    await b.window(WRITE, bytes([0x11] * 8))
    await b.settle()
    dut.i_rx_ok.value = 0
    await ClockCycles(dut.clk, 2)
    await b.window(WRITE, bytes([0x22] * 8))
    await b.settle()
    assert await b.read_rx(0, 8) == [0x11] * 8
    assert b.wins[-1]["rx_ok"] == 0


@cocotb.test()
async def bytes_past_128_are_ignored(dut):
    b = Bench(dut)
    await b.start()
    data = bytes(i & 0xFF for i in range(128)) + bytes([0xEE] * 5)
    await b.window(WRITE, data, half=20_000)
    await b.settle()
    assert b.wins[-1]["count"] == 128
    assert bytes(await b.read_rx(0, 128)) == data[:128]


@cocotb.test()
async def single_read_sends_the_frame_then_zeros(dut):
    b = Bench(dut)
    await b.start()
    await b.fill_tx()
    dut.i_tx_valid.value = 1
    dut.i_tx_sel.value = 1
    dut.i_tx_len1.value = 12
    await ClockCycles(dut.clk, 2)
    isr, out, oes = await b.window(READ, nread=16)
    assert isr == ISR
    assert out == [tx_pattern(1, i) for i in range(12)] + [0] * 4, out
    assert all(o == (1, 0) for o in oes)
    await b.settle()
    assert b.wins[-1] == {"cmd": READ, "count": 16, "rx_ok": 1, "rx_sel": 0, "tx_valid": 1, "tx_sel": 1}


@cocotb.test()
async def read_with_nothing_offered_is_all_zeros(dut):
    b = Bench(dut)
    await b.start()
    await b.fill_tx()
    _, out, _ = await b.window(READ, nread=8)
    assert out == [0] * 8


@cocotb.test()
async def dual_write_two_bits_per_clock(dut):
    b = Bench(dut)
    await b.start()
    data = bytes(random.randrange(256) for _ in range(33))
    isr, _, oes = await b.window(WRITE | DUAL, data)
    assert isr == ISR
    assert all(o[1] == 0 for o in oes), "dual write: FPGA does not drive IO0"
    assert all(o[0] == 0 for o in oes[1:]), f"dual write: FPGA releases IO1 {oes[:4]}"
    await b.settle()
    assert b.wins[-1]["count"] == 33 and b.wins[-1]["cmd"] == WRITE | DUAL
    assert bytes(await b.read_rx(0, 33)) == data


@cocotb.test()
async def dual_read_turnaround_then_both_lines(dut):
    b = Bench(dut)
    await b.start()
    await b.fill_tx()
    dut.i_tx_valid.value = 1
    dut.i_tx_len0.value = 20
    await ClockCycles(dut.clk, 2)
    isr, out, oes = await b.window(READ | DUAL, nread=22)
    assert isr == ISR
    assert out == [tx_pattern(0, i) for i in range(20)] + [0, 0], out
    assert all(o == (1, 1) for o in oes), "dual read: FPGA drives both lines"
    await b.settle()
    assert b.wins[-1]["count"] == 22


async def soak(dut, period_ps, windows=1000):
    """Steady state at the limits: lead exactly 3 f_sys clocks, CS_n high exactly 8, SCK from well above
    to well below f_sys, CS_n rising 0 or half a clock after the last falling edge. One report per
    window, right summary, right data, on every window."""
    b = Bench(dut, period_ps)
    await b.start()
    await b.fill_tx()
    rng = random.Random(period_ps)
    expect = []
    sel = 0
    for w in range(windows):
        half = rng.randrange(max(period_ps // 6, 1000), period_ps * 6)
        dual = rng.choice((0, DUAL))
        n = rng.randrange(0, 12)
        tail = rng.choice((0, half))
        if rng.random() < 0.5:
            data = bytes(rng.randrange(256) for _ in range(n))
            sel = rng.randrange(2)
            dut.i_rx_sel.value = sel                  # the offer changes only while CS_n is high
            await b.window(WRITE | dual, data, half=half, lead=3 * period_ps, tail=tail)
            await b.gap()
            mem = dut.rx_buf1 if sel else dut.rx_buf0
            assert bytes(int(mem[i].value) for i in range(n)) == data, f"window {w}: rx data"
            expect.append((WRITE | dual, n))
        else:
            tx_sel = rng.randrange(2)
            length = rng.randrange(0, 12)
            dut.i_tx_sel.value = tx_sel
            dut.i_tx_valid.value = 1
            dut.i_tx_len0.value = length
            dut.i_tx_len1.value = length
            _, out, _ = await b.window(READ | dual, nread=n, half=half, lead=3 * period_ps, tail=tail)
            await b.gap()
            want = [tx_pattern(tx_sel, i) if i < length else 0 for i in range(n)]
            assert out == want, f"window {w}: read {out} want {want} half={half} dual={dual}"
            expect.append((READ | dual, n))
        assert len(b.wins) == w + 1, f"window {w}: {len(b.wins)} reports"
    assert [(x["cmd"], x["count"]) for x in b.wins] == expect


@cocotb.test()
async def steady_state_at_the_timing_limits_100mhz(dut):
    await soak(dut, 10_000)


@cocotb.test()
async def steady_state_at_the_timing_limits_50mhz(dut):
    await soak(dut, 20_000)


@cocotb.test()
async def steady_state_at_the_timing_limits_27mhz(dut):
    await soak(dut, 37_037)


@cocotb.test()
async def sck_while_cs_high_is_ignored(dut):
    """Another device on a shared bus clocks SCK while our CS_n is high. The guard on CS_n in the rising-edge
    block is what stops this: the counter is held at 0 by the async reset, so without the guard every
    stray edge would start a "window". No report, the last summary and the buffers stay."""
    b = Bench(dut)
    await b.start()
    data = bytes(range(1, 9))
    await b.window(WRITE, data)
    await b.settle()
    assert len(b.wins) == 1
    summary = dict(b.wins[0])
    dut.i_rx_ok.value = 0                         # a stray latch of the offer would show in the summary
    await ClockCycles(dut.clk, 2)
    for k in range(64):
        dut.io0_i.value = k & 1
        dut.sck.value = 1
        await Timer(50_000, unit="ps")
        dut.sck.value = 0
        await Timer(50_000, unit="ps")
    await b.settle()
    assert len(b.wins) == 1, f"stray SCK reported {len(b.wins) - 1} windows"
    assert int(dut.o_win_cmd.value) == summary["cmd"] and int(dut.o_win_count.value) == summary["count"]
    assert int(dut.o_win_rx_ok.value) == summary["rx_ok"], "stray SCK latched the offer"
    assert bytes(await b.read_rx(0, 8)) == data
    assert int(dut.io1_oe.value) == 0 and int(dut.io0_oe.value) == 0, "drives while CS_n is high"
    # and the next real window is unharmed
    dut.i_rx_ok.value = 1
    await ClockCycles(dut.clk, 2)
    await b.window(WRITE, bytes([0x5A] * 4))
    await b.settle()
    assert len(b.wins) == 2 and b.wins[1]["count"] == 4
    assert bytes(await b.read_rx(0, 4)) == bytes([0x5A] * 4)


@cocotb.test()
async def a_short_window_restarts_cleanly(dut):
    """CS_n rising in mid byte (async reset of the SCK counters): the next window starts from edge 0."""
    b = Bench(dut)
    await b.start()
    dut.cs_n.value = 0
    await Timer(100_000, unit="ps")
    for k in range(3):                            # 3 of the 8 command bits
        await b.clock_bit(50_000, io0=k & 1)
    dut.cs_n.value = 1
    await b.settle()
    await b.window(WRITE, bytes([0x77] * 3))
    await b.settle()
    assert b.wins[-1]["cmd"] == WRITE and b.wins[-1]["count"] == 3
    assert await b.read_rx(0, 3) == [0x77] * 3


@cocotb.test()
async def reset_does_not_report_a_window(dut):
    """A system reset after an odd number of windows: the system-domain toggle flops clear, so the SCK-domain
    toggle has to as well, or the PHY reports a window nobody ran, with stale summary."""
    b = Bench(dut)
    await b.start()
    await b.window(WRITE, bytes(4))
    await b.settle()
    assert len(b.wins) == 1
    await b.reset()
    await b.settle()
    assert len(b.wins) == 1, f"{len(b.wins) - 1} window(s) reported after reset, none ran"
    await b.window(WRITE, bytes(5))
    await b.settle()
    assert len(b.wins) == 2 and b.wins[1]["count"] == 5, b.wins
