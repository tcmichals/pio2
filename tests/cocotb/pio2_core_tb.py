# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared bench for pio2_core: AXI4-Lite master, AXI-Stream source / sink on the core's boundary
([SPEC-ICN-11]), program loading as the Pico SDK does it, and the trace records out of channel 1."""

import itertools
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge
from cocotbext.axi import (AxiLiteBus, AxiLiteMaster, AxiStreamBus, AxiStreamFrame, AxiStreamSink,
                           AxiStreamSource)

from pio2.asm import assemble
from pio2.regs import (CLKDIV, DMA_CFG, DMA_CH, DMA_FLUSH, EXECCTRL, INSTR, IRAM, PINCTRL, REG_CTRL,
                       SHIFTCTRL, SM, TRACE_MASK)


def pauses(rng, p):
    return itertools.cycle([rng.random() < p for _ in range(1009)])


class Core:
    def __init__(self, dut):
        self.dut = dut
        self.axil = AxiLiteMaster(AxiLiteBus.from_prefix(dut, "s_axil"), dut.clk, dut.rst_n,
                                  reset_active_level=False)
        # byte_size 32: the streams carry whole words (no TKEEP), one list element per beat
        self.src = AxiStreamSource(AxiStreamBus.from_prefix(dut, "s_axis"), dut.clk, dut.rst_n,
                                   reset_active_level=False, byte_size=32)
        self.sink = AxiStreamSink(AxiStreamBus.from_prefix(dut, "m_axis"), dut.clk, dut.rst_n,
                                  reset_active_level=False, byte_size=32)
        self.frames = {}          # channel -> list of received blocks (lists of words)
        self.records = []         # trace records (64-bit) from channel 1
        self._rx = None

    @classmethod
    async def start(cls, dut, gpio=0, hz=100e6):
        period_ps = 2 * round(1e12 / hz / 2)              # cocotb needs an even period
        cocotb.start_soon(Clock(dut.clk, period_ps, unit="ps").start())
        dut.s_axis_tvalid.value = 0
        dut.m_axis_tready.value = 0
        dut.rst_n.value = 0
        dut.i_gpio.value = gpio
        core = cls(dut)
        await ClockCycles(dut.clk, 5)
        dut.rst_n.value = 1
        await ClockCycles(dut.clk, 2)
        core._rx = cocotb.start_soon(core._collect())
        return core

    def stall(self, seed, p_in=0.3, p_out=0.3):
        """Random gaps on the input stream's TVALID and the output stream's TREADY."""
        rng = random.Random(seed)
        self.src.set_pause_generator(pauses(rng, p_in))
        self.sink.set_pause_generator(pauses(rng, p_out))

    async def _collect(self):
        half = None
        while True:
            f = await self.sink.recv()
            dest = f.tdest if isinstance(f.tdest, int) else f.tdest[0]
            assert f.tdest == dest or all(d == dest for d in f.tdest), "TDEST changed inside a block"
            words = list(f.tdata)
            if dest == 1:
                for w in words:
                    if half is None:
                        half = w
                    else:
                        self.records.append((w << 32) | half)
                        half = None
            self.frames.setdefault(dest, []).append(words)

    def words(self, chan):
        return [w for f in self.frames.get(chan, []) for w in f]

    async def write(self, addr, value, expect=0):
        r = await self.axil.write(addr, int(value & 0xFFFF_FFFF).to_bytes(4, "little"))
        assert r.resp == expect, f"write 0x{addr:04X}: response {r.resp}, expected {expect}"

    async def read(self, addr, expect=0):
        r = await self.axil.read(addr, 4)
        assert r.resp == expect, f"read 0x{addr:04X}: response {r.resp}, expected {expect}"
        return int.from_bytes(r.data, "little")

    async def send(self, chan, words):
        await self.src.send(AxiStreamFrame(tdata=list(words), tdest=chan))

    async def load(self, source, sm, offset=0, shiftctrl=0, pinctrl=0, enable=True, clkdiv=1.0,
                   execctrl=0):
        """Load `source` (pioasm text, or an image dict from load_program) and start it on SM `sm`
        like pio_sm_init: wrap and side-set from the image, divider, restart, jump to the start."""
        p = assemble(source)["programs"][0] if isinstance(source, str) else source
        for i, w in enumerate(p["instructions"]):
            await self.write(IRAM + 4 * (offset + i), w)
        execctrl |= ((p["wrap"] + offset) << 12) | ((p["wrap_target"] + offset) << 7)
        ss = p["sideset"]
        if ss["size"] and ss["optional"]:
            execctrl |= 1 << 30
        if ss["size"] and ss["pindirs"]:
            execctrl |= 1 << 29
        div_int = int(clkdiv)
        div_frac = int(round((clkdiv - div_int) * 256))
        if div_frac == 256:
            div_int, div_frac = div_int + 1, 0
        await self.write(SM(sm, CLKDIV), (div_int << 16) | (div_frac << 8))
        await self.write(SM(sm, EXECCTRL), execctrl)
        await self.write(SM(sm, SHIFTCTRL), shiftctrl)
        await self.write(SM(sm, PINCTRL), pinctrl)
        ctrl = await self.read(REG_CTRL) & 0xF
        await self.write(REG_CTRL, (ctrl & ~(1 << sm)) | (1 << (sm + 4)))
        await self.write(SM(sm, INSTR), offset)
        if enable:
            await self.write(REG_CTRL, (ctrl | (1 << sm)))
        return p

    async def trace_on(self, mask=0xFFFF_FFFF):
        await self.write(TRACE_MASK, mask)
        await self.write(DMA_CH(1, DMA_FLUSH), 1)
        await self.write(DMA_CH(1, DMA_CFG), 1)

    async def wait_for(self, cond, limit=100_000, what="condition"):
        for _ in range(limit):
            if cond():
                return
            await ClockCycles(self.dut.clk, 1)
        raise AssertionError(f"timed out waiting for {what}")


class Board:
    """Pads of the core: a pin the core drives (pindir 1) shows its output; otherwise an outside
    driver in `ext` (pin -> level), else the pull-up (1). `pin` / `esc` / `level()` are the motor
    wire of the DShot and ESC benches, so their timing helpers work on this board."""

    def __init__(self, dut, pin=0):
        self.dut, self.pin = dut, pin
        self.ext = {}

    @property
    def esc(self):
        return self.ext.get(self.pin)

    @esc.setter
    def esc(self, level):
        if level is None:
            self.ext.pop(self.pin, None)
        else:
            self.ext[self.pin] = level

    def pads(self):
        out, dirs = int(self.dut.o_gpio_out.value), int(self.dut.o_gpio_pindir.value)
        v = 0
        for n in range(32):
            if (dirs >> n) & 1:
                bit = (out >> n) & 1
            else:
                bit = self.ext.get(n, 1)
            v |= bit << n
        return v

    async def run(self):
        while True:
            await FallingEdge(self.dut.clk)
            self.dut.i_gpio.value = self.pads()

    def level(self, pin=None):
        return (int(self.dut.i_gpio.value) >> (self.pin if pin is None else pin)) & 1


# Programs used by the DMA and core suites (pioasm syntax).
ECHO = ".program echo\npull block\nmov isr, osr\npush block\n"
# Pushes x, x-1, x-2, ... starting at 0 (0, 0xFFFFFFFF, 0xFFFFFFFE, ...): one word per 3 SM clocks.
COUNTER = ".program counter\n.wrap_target\nin x, 32\npush block\njmp x-- next\nnext:\n.wrap\n"
# Takes words and drops them: the TX FIFO drains, so a stopped ring shows an underrun.
SINK = ".program sink\npull block\nout null, 32\n"


def counter_words(n, start=0):
    return [(start - i) & 0xFFFF_FFFF for i in range(n)]
