# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""Cocotb regression suite for the pio2 PIO tile.

Verifies:
- [SPEC-PIO-01]: four state machines, clock dividers.
- [SPEC-PIO-02]: RP2040 16-bit instruction set (SET, MOV, IN, OUT, PUSH, PULL, JMP, WAIT, IRQ).
- [SPEC-PIO-03]: internal IRQ bus and supervisor / worker chaining.
- [SPEC-PIO-05]: register map through the AXI4-Lite port.
(The TLP packetizer test moved to AbstractX with the TLP path, [SPEC-ICN-09].)
"""
# @verifies [SPEC-PIO-01] [SPEC-PIO-02] [SPEC-PIO-03] [SPEC-PIO-05] docs/SPECIFICATION.md

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge, Timer
from cocotbext.axi import AxiLiteBus, AxiLiteMaster

PIO_BASE = 0x40000700

# Wishbone Register Offsets
REG_CTRL = 0x00
REG_IRQ = 0x04
REG_FIFO_STAT = 0x08
IRAM_BASE = 0x10

# State Machine Register Offsets (Base = 0x100 + 0x20 * N)
def SM_CLKDIV(n): return 0x100 + 0x20 * n + 0x00
def SM_EXECCTRL(n): return 0x100 + 0x20 * n + 0x04
def SM_SHIFTCTRL(n): return 0x100 + 0x20 * n + 0x08
def SM_PINCTRL(n): return 0x100 + 0x20 * n + 0x0C
def SM_TXF(n): return 0x100 + 0x20 * n + 0x10
def SM_RXF(n): return 0x100 + 0x20 * n + 0x14
def SM_STAT(n): return 0x100 + 0x20 * n + 0x18
def SM_TRIG(n): return 0x100 + 0x20 * n + 0x1C

# RP2040 PIO Instruction Encoders
def encode_delay_sideset(delay=0, sideset=0, sideset_count=0):
    val = (delay & 0x1F)
    if sideset_count > 0:
        shift = 5 - sideset_count
        val = ((sideset & ((1 << sideset_count) - 1)) << shift) | (delay & ((1 << shift) - 1))
    return val & 0x1F

def PIO_JMP(cond=0, addr=0, delay=0):
    return (0b000 << 13) | (delay << 8) | ((cond & 7) << 5) | (addr & 0x1F)

def PIO_WAIT(pol=1, src=0, index=0, delay=0):
    return (0b001 << 13) | (delay << 8) | ((pol & 1) << 7) | ((src & 3) << 5) | (index & 0x1F)

def PIO_IN(src=0, count=0, delay=0):
    return (0b010 << 13) | (delay << 8) | ((src & 7) << 5) | (count & 0x1F)

def PIO_OUT(dest=0, count=0, delay=0):
    return (0b011 << 13) | (delay << 8) | ((dest & 7) << 5) | (count & 0x1F)

def PIO_PUSH(iffull=0, block=1, delay=0):
    return (0b100 << 13) | (delay << 8) | (0 << 7) | ((iffull & 1) << 6) | ((block & 1) << 5)

def PIO_PULL(ifempty=0, block=1, delay=0):
    return (0b100 << 13) | (delay << 8) | (1 << 7) | ((ifempty & 1) << 6) | ((block & 1) << 5)

def PIO_MOV(dest=0, op=0, src=0, delay=0):
    return (0b101 << 13) | (delay << 8) | ((dest & 7) << 5) | ((op & 3) << 3) | (src & 7)

def PIO_IRQ(clr=0, wait=0, index=0, delay=0):
    return (0b110 << 13) | (delay << 8) | (0 << 7) | ((clr & 1) << 6) | ((wait & 1) << 5) | (index & 0x1F)

def PIO_SET(dest=0, data=0, delay=0):
    return (0b111 << 13) | (delay << 8) | ((dest & 7) << 5) | (data & 0x1F)


# Register access through the tile's AXI4-Lite port ([SPEC-AXI-04]), driven by cocotbext-axi.
_axil = None


def axil_setup(dut):
    """New AXI4-Lite master for this test (call once the clock runs, before reset release)."""
    global _axil
    _axil = AxiLiteMaster(AxiLiteBus.from_prefix(dut, "s_axil"), dut.clk, dut.rst_n,
                          reset_active_level=False)
    return _axil


async def reg_write(dut, offset, value):
    """Write a 32-bit register (AXI4-Lite; asserts an OKAY response)."""
    resp = await _axil.write(PIO_BASE + offset, int(value).to_bytes(4, "little"))
    assert resp.resp == 0, f"write 0x{offset:03X}: response {resp.resp}"


async def reg_read(dut, offset):
    """Read a 32-bit register (AXI4-Lite; asserts an OKAY response)."""
    resp = await _axil.read(PIO_BASE + offset, 4)
    assert resp.resp == 0, f"read 0x{offset:03X}: response {resp.resp}"
    return int.from_bytes(resp.data, "little")


async def init_tile(dut):
    """Reset the PIO tile and set defaults."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())  # 100 MHz clock
    dut.rst_n.value = 0
    dut.i_gpio.value = 0
    axil_setup(dut)
    dut.s_axis_tx_tvalid.value = 0
    dut.s_axis_tx_tdata.value = 0
    dut.m_axis_rx_tready.value = 0

    await ClockCycles(dut.clk, 5)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)


@cocotb.test()
async def test_wishbone_iram_and_registers(dut):
    """[SPEC-PIO-05] Verify Wishbone register access and 32-word IRAM read/write."""
    await init_tile(dut)

    # 1. Verify default reset state
    ctrl = await reg_read(dut, REG_CTRL)
    assert ctrl == 0, f"Expected REG_CTRL == 0, got 0x{ctrl:08X}"

    irq = await reg_read(dut, REG_IRQ)
    assert irq == 0, f"Expected REG_IRQ == 0, got 0x{irq:08X}"

    # 2. Write and read back all 32 words in IRAM
    test_instructions = [(i * 0x0505 + 0xA5) & 0xFFFF for i in range(32)]
    for i in range(32):
        await reg_write(dut, IRAM_BASE + i * 4, test_instructions[i])

    for i in range(32):
        readback = await reg_read(dut, IRAM_BASE + i * 4)
        assert readback == test_instructions[i], f"IRAM[{i}]: expected 0x{test_instructions[i]:04X}, got 0x{readback:04X}"

    # 3. Configure SM0 clock divider and control registers
    await reg_write(dut, SM_CLKDIV(0), (2 << 16))  # Div = 2.0
    clkdiv0 = await reg_read(dut, SM_CLKDIV(0))
    assert clkdiv0 == (2 << 16), f"SM0 CLKDIV expected 0x20000 (RP2040 INT[31:16]), got 0x{clkdiv0:X}"

    await reg_write(dut, SM_EXECCTRL(0), (31 << 12) | (0 << 7))  # Wrap top 31, bottom 0
    execctrl0 = await reg_read(dut, SM_EXECCTRL(0))
    assert execctrl0 == ((31 << 12) | (0 << 7)), f"SM0 EXECCTRL mismatch: 0x{execctrl0:X}"


@cocotb.test()
async def test_sm_gpio_set_and_jmp(dut):
    """[SPEC-PIO-01] [SPEC-PIO-02] Verify SET pins, PINDIRS, and JMP execution on SM0."""
    await init_tile(dut)

    # Program SM0:
    # 0: SET PINDIRS, 0x1F   (pins 0..4 as outputs)
    # 1: SET PINS, 0x15      (set pins 0, 2, 4 high, pins 1, 3 low)
    # 2: SET PINS, 0x0A      (set pins 1, 3 high, pins 0, 2, 4 low)
    # 3: JMP 1
    prog = [
        PIO_SET(dest=4, data=0x1F),  # 4 = pindirs
        PIO_SET(dest=0, data=0x15),  # 0 = pins
        PIO_SET(dest=0, data=0x0A),
        PIO_JMP(cond=0, addr=1)
    ]
    for i, inst in enumerate(prog):
        await reg_write(dut, IRAM_BASE + i * 4, inst)

    # PINCTRL for SM0: set_base = 0, set_count = 5
    await reg_write(dut, SM_PINCTRL(0), (5 << 26) | (0 << 10))
    # Clock divider = 1.0 (full speed)
    await reg_write(dut, SM_CLKDIV(0), (1 << 16))
    # Wrap config: wrap_top = 31, wrap_bottom = 0
    await reg_write(dut, SM_EXECCTRL(0), (31 << 12) | (0 << 7))

    # Enable SM0
    await reg_write(dut, REG_CTRL, 0x01)
    await ClockCycles(dut.clk, 3)

    # Check that PINDIRS for pins 0..4 are driven high
    assert (int(dut.o_gpio_pindir.value) & 0x1F) == 0x1F, f"Expected PINDIR 0x1F, got {int(dut.o_gpio_pindir.value):08X}"

    # Cycle through and observe GPIO pin toggles
    pins_seen = set()
    for _ in range(10):
        await RisingEdge(dut.clk)
        val = int(dut.o_gpio_out.value) & 0x1F
        pins_seen.add(val)

    assert 0x15 in pins_seen, f"0x15 pattern not observed: seen {pins_seen}"
    assert 0x0A in pins_seen, f"0x0A pattern not observed: seen {pins_seen}"

    # Disable SM0
    await reg_write(dut, REG_CTRL, 0x00)


@cocotb.test()
async def test_tx_fifo_pull_and_out(dut):
    """[SPEC-PIO-02] Test TX FIFO write via Wishbone, PULL, and OUT to GPIO pins."""
    await init_tile(dut)

    # Program SM0:
    # 0: SET PINDIRS, 0xFF (pins 0..7 output)
    # 1: PULL BLOCK
    # 2: OUT PINS, 8
    # 3: JMP 1
    prog = [
        PIO_SET(dest=4, data=0x1F),  # Set lower 5 pindirs
        PIO_PULL(ifempty=0, block=1),
        PIO_OUT(dest=0, count=8),    # 0 = pins, count 8
        PIO_JMP(cond=0, addr=1)
    ]
    for i, inst in enumerate(prog):
        await reg_write(dut, IRAM_BASE + i * 4, inst)

    # PINCTRL: out_base = 0, out_count = 8, set_base = 0, set_count = 5
    await reg_write(dut, SM_PINCTRL(0), (8 << 20) | (5 << 26) | 0)
    await reg_write(dut, SM_SHIFTCTRL(0), (1 << 19))  # Shift right (LSB first)
    await reg_write(dut, SM_CLKDIV(0), (1 << 16))

    # Enable SM0
    await reg_write(dut, REG_CTRL, 0x01)
    await ClockCycles(dut.clk, 5)

    # Write test byte 0x5A to TX FIFO via Wishbone
    await reg_write(dut, SM_TXF(0), 0x5A)

    # Wait for SM to execute PULL and OUT
    out_val = 0
    for _ in range(20):
        await RisingEdge(dut.clk)
        out_val = int(dut.o_gpio_out.value) & 0xFF
        if out_val == 0x5A:
            break
    assert out_val == 0x5A, f"Expected 0x5A on GPIO, got 0x{out_val:02X}"

    # Write next test byte 0xC3 to TX FIFO
    await reg_write(dut, SM_TXF(0), 0xC3)
    for _ in range(20):
        await RisingEdge(dut.clk)
        out_val = int(dut.o_gpio_out.value) & 0xFF
        if out_val == 0xC3:
            break
    assert out_val == 0xC3, f"Expected 0xC3 on GPIO, got 0x{out_val:02X}"

    # Disable SM0
    await reg_write(dut, REG_CTRL, 0x00)


@cocotb.test()
async def test_supervisor_worker_irq_chaining(dut):
    """[SPEC-PIO-03] Test internal IRQ bus handshake between SM0 (Worker) and SM1 (Supervisor)."""
    await init_tile(dut)

    # Shared IRAM layout:
    # SM0 (Worker) at offset 0:
    #   0: WAIT 1 IRQ 4        ; Wait for Supervisor to trigger
    #   1: SET PINS, 0x01      ; Indicate worker active
    #   2: IRQ SET 5           ; Signal completion to supervisor
    #   3: JMP 0
    #
    # SM1 (Supervisor) at offset 8:
    #   8: SET PINS, 0x02      ; Supervisor running
    #   9: IRQ SET 4           ; Trigger worker
    #  10: WAIT 1 IRQ 5        ; Wait for worker to finish
    #  11: IRQ CLEAR 5         ; Clear completion flag
    #  12: IRQ CLEAR 4         ; Clear trigger flag
    #  13: JMP 8
    prog_sm0 = [
        PIO_WAIT(pol=1, src=2, index=4),  # src 2 = IRQ
        PIO_SET(dest=0, data=0x01),
        PIO_IRQ(clr=0, wait=0, index=5),
        PIO_JMP(cond=0, addr=0)
    ]
    prog_sm1 = [
        PIO_SET(dest=0, data=0x02),
        PIO_IRQ(clr=0, wait=0, index=4),
        PIO_WAIT(pol=1, src=2, index=5),
        PIO_IRQ(clr=1, wait=0, index=5),
        PIO_IRQ(clr=1, wait=0, index=4),
        PIO_JMP(cond=0, addr=8)
    ]

    for i, inst in enumerate(prog_sm0):
        await reg_write(dut, IRAM_BASE + (0 + i) * 4, inst)

    for i, inst in enumerate(prog_sm1):
        await reg_write(dut, IRAM_BASE + (8 + i) * 4, inst)

    # Configure SM0 PINCTRL & EXECCTRL
    await reg_write(dut, SM_PINCTRL(0), (2 << 26) | 0)
    await reg_write(dut, SM_EXECCTRL(0), (3 << 12) | (0 << 7))  # Wrap 0..3
    await reg_write(dut, SM_CLKDIV(0), (1 << 16))

    # Configure SM1 PINCTRL & EXECCTRL (start at PC = 8, wrap 8..13)
    await reg_write(dut, SM_PINCTRL(1), (2 << 26) | 0)
    await reg_write(dut, SM_EXECCTRL(1), (13 << 12) | (8 << 7))  # Wrap 8..13
    await reg_write(dut, SM_CLKDIV(1), (1 << 16))

    # Start SM1 at PC = 8 the way pio_sm_init does: restart, then an SMx_INSTR `jmp 8`
    # (a restart keeps the PC, [SPEC-PIO-02] rule 7)
    await reg_write(dut, REG_CTRL, 0x02 << 4)
    await reg_write(dut, 0x100 + 0x20 * 1 + 0x18, PIO_JMP(cond=0, addr=8))
    await ClockCycles(dut.clk, 2)

    # Now enable both SM0 and SM1
    await reg_write(dut, REG_CTRL, 0x03)

    # Watch the flags every clock: `wait 1 irq` clears a flag the clock it fires, so a flag
    # lives 1-2 clocks and bus reads every few clocks can miss it
    seen4 = seen5 = rounds = 0
    for _ in range(200):
        await RisingEdge(dut.clk)
        f = int(dut.irq_flags.value)
        seen4 += (f >> 4) & 1
        seen5 += (f >> 5) & 1
        rounds += int(dut.gen_sm[1].u_sm.pc.value) == 8
    assert seen4 and seen5, f"IRQ 4 seen {seen4} clocks, IRQ 5 seen {seen5} clocks"
    assert rounds >= 5, f"supervisor completed only {rounds} rounds in 200 clocks"

    # Disable both
    await reg_write(dut, REG_CTRL, 0x00)
