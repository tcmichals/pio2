# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""Every RP2040 PIO opcode and operand on the RTL ([SPEC-PIO-02], [SPEC-PIO-15]).

The cases and the datasheet rules are in pio2.isa_cases (shared with the cocotb suite
tests/cocotb/test_isa_every_opcode_cocotb.py, which runs them on Icarus and Verilator). Here they run on
the Verilator debugger model of the unmodified tile (pio2.debug.Session). The coverage test at the
end fails if any opcode / operand combination of the RP2040 ISA has no passing case.
"""
# @verifies [SPEC-PIO-02] [SPEC-PIO-15] docs/SPECIFICATION.md

import pytest

from pio2.debug.session import Session
from pio2.isa_cases import CASES, M32, missing_coverage

COVERED = set()


class SessionHw:
    """The isa_cases `hw` interface on a debugger Session."""

    def __init__(self, s):
        self.s = s
        self.write, self.step = s.write, s.step
        self.sm_state, self.pins, self.irq_flags = s.sm_state, s.pins, s.irq_flags

    def reset(self):
        self.gpio_in(0)
        self.s.reset()

    def gpio_in(self, value):
        s = self.s
        s.ext, s.ext_mask = value, M32
        s.lib.pio2sim_set_ext(s.h, value, M32, s.pull_level)


@pytest.fixture(scope="module")
def hw():
    s = Session()
    yield SessionHw(s)
    s.close()


@pytest.mark.parametrize("fn,args", [pytest.param(f, a, id=i) for i, f, a in CASES])
def test_case(hw, fn, args):
    COVERED.add(fn(hw, *args))


def test_every_rp2040_opcode_is_covered():
    """Runs last: fails if any RP2040 opcode / operand combination had no passing case."""
    missing = missing_coverage(COVERED)
    assert not missing, f"{len(missing)} not covered (or failed): {missing[:20]}"
