# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Pin window and PIN_BASE ([SPEC-PIO-21]), tile built with NUM_PINS = 16 (tests/cocotb/Makefile): pin numbers
wrap at the window, the window moves with PIN_BASE, GPIOs outside it are not driven and not read.
"""
# @verifies [SPEC-PIO-21] docs/SPECIFICATION.md

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles
from cocotbext.axi import AxiLiteBus, AxiLiteMaster

BASE = 0x40000700
CTRL, PIN_BASE = 0x00, 0x9C
SM = 0x100
PINCTRL, RXF, INSTR, EXECCTRL = 0x0C, 0x14, 0x18, 0x04

SET_PINS, SET_PINDIRS = 0xE000, 0xE080
IN_PINS_32 = 0x4000             # in pins, 32
PUSH = 0x8020                   # push block


def pinctrl(out_base=0, set_base=0, side_base=0, in_base=0, out_count=0, set_count=0):
    return out_base | set_base << 5 | side_base << 10 | in_base << 15 | out_count << 20 | set_count << 26


class Bench:
    def __init__(self, dut):
        self.dut = dut
        self.axi = AxiLiteMaster(AxiLiteBus.from_prefix(dut, "s_axil"), dut.clk, dut.rst_n, reset_active_level=False)

    async def start(self):
        d = self.dut
        Clock(d.clk, 10, unit="ns").start()
        d.i_gpio.value = 0
        d.s_axis_tx_tvalid.value = 0
        d.s_axis_tx_tdata.value = 0
        d.m_axis_rx_tready.value = 0
        d.rst_n.value = 0
        await ClockCycles(d.clk, 5)
        d.rst_n.value = 1
        await ClockCycles(d.clk, 5)

    async def wr(self, off, v):
        await self.axi.write(BASE + off, v.to_bytes(4, "little"))

    async def rd(self, off):
        r = await self.axi.read(BASE + off, 4)
        return int.from_bytes(r.data, "little")

    async def run(self, *instrs):
        for i in instrs:
            await self.wr(SM + INSTR, i)
        await ClockCycles(self.dut.clk, 4)

    async def pins(self):
        await ClockCycles(self.dut.clk, 1)
        return int(self.dut.o_gpio_out.value), int(self.dut.o_gpio_pindir.value)

    async def read_pins(self, in_base=0):
        """What an SM reads from its pins: `in pins, 32`, then `push` and the RX FIFO."""
        await self.wr(SM + PINCTRL, pinctrl(in_base=in_base))
        await self.run(IN_PINS_32, PUSH)
        return await self.rd(SM + RXF)


async def bench(dut):
    assert int(dut.NUM_PINS.value) == 16, "this suite needs the tile built with NUM_PINS = 16"
    b = Bench(dut)
    await b.start()
    return b


@cocotb.test()
async def set_pins_land_in_the_window(dut):
    b = await bench(dut)
    await b.wr(SM + PINCTRL, pinctrl(set_base=2, set_count=2))
    await b.run(SET_PINDIRS | 3, SET_PINS | 3)
    assert await b.pins() == (0b1100, 0b1100)


@cocotb.test()
async def a_pin_group_wraps_at_the_window(dut):
    b = await bench(dut)
    await b.wr(SM + PINCTRL, pinctrl(set_base=15, set_count=2))
    await b.run(SET_PINDIRS | 3, SET_PINS | 3)
    assert await b.pins() == (0x8001, 0x8001)


@cocotb.test()
async def a_base_above_the_window_counts_modulo(dut):
    b = await bench(dut)
    await b.wr(SM + PINCTRL, pinctrl(set_base=18, set_count=1))       # 18 mod 16 = pin 2
    await b.run(SET_PINDIRS | 1, SET_PINS | 1)
    assert await b.pins() == (1 << 2, 1 << 2)


@cocotb.test()
async def pin_base_moves_the_outputs(dut):
    b = await bench(dut)
    await b.wr(SM + PINCTRL, pinctrl(set_base=15, set_count=2))
    await b.wr(PIN_BASE, 16)
    assert await b.rd(PIN_BASE) == 16
    await b.run(SET_PINDIRS | 3, SET_PINS | 3)
    assert await b.pins() == (0x8001 << 16, 0x8001 << 16), "window on GPIO 16..31, nothing below"
    await b.wr(PIN_BASE, 0)                                           # the latches stay with the window
    assert await b.pins() == (0x8001, 0x8001)


@cocotb.test()
async def pin_base_low_bits_are_dropped(dut):
    b = await bench(dut)
    await b.wr(PIN_BASE, 17)
    assert await b.rd(PIN_BASE) == 16
    await b.wr(PIN_BASE, 5)
    assert await b.rd(PIN_BASE) == 0


@cocotb.test()
async def reading_pins_sees_the_window_only(dut):
    b = await bench(dut)
    dut.i_gpio.value = 0xABCD_1234
    await ClockCycles(dut.clk, 1)
    assert await b.read_pins() == 0x1234, "bits above the window read 0"
    await b.wr(PIN_BASE, 16)
    assert await b.read_pins() == 0xABCD
    assert await b.read_pins(in_base=4) == 0xDABC, "IN_BASE rotates within the window"


@cocotb.test()
async def jmp_pin_and_wait_gpio_use_the_window(dut):
    b = await bench(dut)
    dut.i_gpio.value = 1 << 21                                        # GPIO 21 = window pin 5 at base 16
    await b.wr(PIN_BASE, 16)
    await b.wr(SM + EXECCTRL, (31 << 12) | (5 << 24))                 # JMP_PIN = 5
    await b.run(0x00C7)                                               # jmp pin, 7
    assert int(dut.gen_sm[0].u_sm.pc.value) == 7, "JMP PIN taken"
    await b.run(0x2085)                                               # wait 1 gpio 5: passes at once
    assert int(dut.gen_sm[0].u_sm.stalled.value) == 0
    await b.wr(PIN_BASE, 0)                                           # now window pin 5 is GPIO 5: low
    await b.run(0x0007, 0x00D0)                                       # jmp 7; jmp pin, 16: not taken
    assert int(dut.gen_sm[0].u_sm.pc.value) == 7, "JMP PIN not taken"


@cocotb.test()
async def pin_numbers_above_the_window_count_modulo(dut):
    """JMP_PIN and WAIT GPIO numbers wrap at the window, like the bases: 21 mod 16 = window pin 5."""
    b = await bench(dut)
    dut.i_gpio.value = 1 << 5
    await b.wr(SM + EXECCTRL, (31 << 12) | (21 << 24))                # JMP_PIN = 21
    await b.run(0x00C7)                                               # jmp pin, 7
    assert int(dut.gen_sm[0].u_sm.pc.value) == 7, "JMP PIN 21 reads window pin 5"
    await b.run(0x2095)                                               # wait 1 gpio 21: passes at once
    assert int(dut.gen_sm[0].u_sm.stalled.value) == 0, "WAIT GPIO 21 reads window pin 5"
