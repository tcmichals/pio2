# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""Every RP2040 PIO opcode and operand on the RTL, as a cocotb suite ([SPEC-PIO-02]).

The same cases as tests/test_isa_every_opcode.py (the table and the datasheet rules are in
pio2.isa_cases), here on any cocotb simulator: Icarus as the second simulator, and Verilator.

    uv run pio2-cocotb --sim icarus --only isa
    uv run pio2-cocotb --only isa                  # Verilator

Registers go through the tile's AXI4-Lite port (cocotbext-axi AxiLiteMaster). The cases are blocking
code; each runs in a cocotb bridge thread and every hardware access resumes the simulator (cocotb
`bridge` / `resume`). State is read by name from the RTL (gen_sm[0].u_sm.*), the same signals the
debugger reads. The pads are modelled as in the debugger: a pin the tile drives (pindir 1) reads back
its own output, any other pin reads what the bench drives. The last test fails if any RP2040 opcode /
operand combination had no passing case.
"""
# cocotb-toplevel: asp_pio_tile
# @verifies [SPEC-PIO-02] docs/SPECIFICATION.md

import cocotb
from cocotb.clock import Clock
from cocotb.task import bridge, resume
from cocotb.triggers import ClockCycles, FallingEdge
from cocotbext.axi import AxiLiteBus, AxiLiteMaster

from pio2.isa_cases import CASES, M32, missing_coverage

PIO_BASE = 0x40000700
COVERED = set()


def _int(handle):
    v = handle.value
    return v.to_unsigned() if hasattr(v, "to_unsigned") else int(v)


class CocotbHw:
    """The isa_cases `hw` interface on the tile under cocotb (blocking; call from a bridge thread)."""

    def __init__(self, dut):
        self.dut = dut
        self.ext = 0
        self.axil = AxiLiteMaster(AxiLiteBus.from_prefix(dut, "s_axil"), dut.clk, dut.rst_n,
                                  reset_active_level=False)
        self.sm = dut.gen_sm[0].u_sm
        for name, value in (("s_axis_tx_tvalid", 0), ("s_axis_tx_tdata", 0), ("m_axis_rx_tready", 0)):
            getattr(dut, name).value = value
        dut.i_gpio.value = 0
        cocotb.start_soon(self._pads())

    def _pads_value(self):
        out, dir_ = _int(self.dut.o_gpio_out), _int(self.dut.o_gpio_pindir)
        return (out & dir_) | (self.ext & ~dir_ & M32)

    async def _pads(self):
        while True:
            await FallingEdge(self.dut.clk)
            self.dut.i_gpio.value = self._pads_value()

    # ---- async halves, run in the simulator by resume()
    async def _reset(self):
        self.ext = 0
        self.dut.i_gpio.value = 0
        self.dut.rst_n.value = 0
        await ClockCycles(self.dut.clk, 5)
        self.dut.rst_n.value = 1
        await ClockCycles(self.dut.clk, 2)
        await FallingEdge(self.dut.clk)

    async def _write(self, off, value):
        resp = await self.axil.write(PIO_BASE + off, (value & M32).to_bytes(4, "little"))
        assert int(resp.resp) == 0, f"write 0x{off:03X}: response {resp.resp}"
        await FallingEdge(self.dut.clk)          # state settled; no further rising edge passes

    async def _step(self, n):
        for _ in range(n):
            await FallingEdge(self.dut.clk)

    async def _sm_state(self):
        sm, dut = self.sm, self.dut
        st = {k: _int(getattr(sm, k)) for k in ("pc", "reg_x", "reg_y", "isr", "osr", "isr_count",
                                                  "osr_count", "stalled", "exec_pending", "rx_level",
                                                  "rx_rd_ptr")}
        shift = _int(dut.sm_shiftctrl[0])
        jt = bool(shift >> 30 & 1)
        jr = bool(shift >> 31 & 1) and not jt
        idx = (lambda p: p & 7) if jr else (lambda p: 4 | (p & 3))
        st["rx_fifo"] = [_int(sm.fifo_mem[idx(st["rx_rd_ptr"] + i)]) for i in range(st["rx_level"])]
        return st

    async def _pins(self):
        return _int(self.dut.o_gpio_out), _int(self.dut.o_gpio_pindir), _int(self.dut.i_gpio)

    async def _irq_flags(self):
        return _int(self.dut.irq_flags)

    async def _gpio_in(self, value):
        self.ext = value & M32
        self.dut.i_gpio.value = self._pads_value()

    # ---- the blocking interface the cases use
    def reset(self):
        resume(self._reset)()

    def write(self, off, value):
        resume(self._write)(off, value)

    def step(self, n=1):
        if n > 0:
            resume(self._step)(n)

    def sm_state(self, sm):
        assert sm == 0, "the cases use SM0"
        return resume(self._sm_state)()

    def pins(self):
        return resume(self._pins)()

    def irq_flags(self):
        return resume(self._irq_flags)()

    def gpio_in(self, value):
        resume(self._gpio_in)(value)


@cocotb.test()
@cocotb.parametrize(case=[cocotb.Param(value=(f, a), name=i) for i, f, a in CASES])
async def isa(dut, case):
    """One RP2040 opcode / operand case from pio2.isa_cases."""
    fn, args = case
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 2)                  # in reset, the outputs are known (Icarus starts at X)
    hw = CocotbHw(dut)
    COVERED.add(await bridge(fn)(hw, *args))


@cocotb.test()
async def zz_every_rp2040_opcode_is_covered(dut):
    """Runs last: fails if any RP2040 opcode / operand combination had no passing case."""
    del dut
    missing = missing_coverage(COVERED)
    assert not missing, f"{len(missing)} not covered (or failed): {missing[:20]}"
