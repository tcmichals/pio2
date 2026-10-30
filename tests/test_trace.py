# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""PIO trace records from the RTL ([SPEC-TRC-03], [SPEC-TRC-01]): made by pio2_trace_tap inside pio2_core
(simulated by Session), checked against the per-clock trace."""
# @verifies [SPEC-TRC-03] docs/INTERCONNECT_SPECIFICATION.md

import pytest

from pio2.asm import assemble
from pio2.debug.session import Pins, Session


@pytest.fixture
def s():
    sess = Session()
    yield sess
    sess.close()


def edges(s, sm, key):
    """(rises, falls) of a per-clock signal: 'stall' from the trace."""
    tr = s.trace
    v = [tr.stalls[4 * i + sm] for i in range(len(tr))]
    rises = [tr.start + i for i in range(1, len(v)) if v[i] and not v[i - 1]]
    falls = [tr.start + i for i in range(1, len(v)) if not v[i] and v[i - 1]]
    return rises, falls


def test_stall_records_match_the_per_clock_trace_1000_rounds(s):
    """blink stalls on `pull` until a word arrives: every stall begin / end record has the clock
    and the PC of the per-clock trace."""
    s.load("blink", sm=0, pinctrl=Pins(set_base=25, set_count=1))
    for k in range(1000):
        s.step(20)
        s.push(0, 0)                                     # y = 0, then pull again after one blink
        s.step(30)
    rises, falls = edges(s, 0, "stall")
    begins = [e for e in s.events if e.name == "stall begin" and e.src == 0]
    ends = [e for e in s.events if e.name == "stall end" and e.src == 0]
    assert [e.cycle for e in begins] == rises[:len(begins)] and len(begins) == len(rises)
    assert [e.cycle for e in ends] == falls[:len(ends)] and len(ends) == len(falls)
    assert all(e.data == 0 for e in begins + ends), "blink stalls at its `pull` (PC 0)"
    assert s.trace_counters() == (0, 0)


def test_irq_set_and_clear_records(s):
    prog = assemble(".program i\nirq set 3\nirq clear 3\nirq set 5 [7]\nirq clear 5\nhalt:\njmp halt")
    s.load(prog["programs"][0], sm=1)
    s.step(40)
    irq = [(e.name, e.data) for e in s.events if e.src == 0xFF]
    assert irq == [("irq set", 3), ("irq clear", 3), ("irq set", 5), ("irq clear", 5)], irq


def test_fifo_edges(s):
    """RX full when the SM pushes 4 words; TX empty when it has pulled the last word."""
    prog = assemble(".program f\npull block\nmov isr, osr\npush block\njmp 0")
    s.load(prog["programs"][0], sm=2)
    for w in range(4):
        s.push(2, w)
    s.step(60)
    names = [e.name for e in s.events if e.src == 2]
    assert "RX FIFO full" in names and "TX FIFO empty" in names, names
    full = next(e for e in s.events if e.src == 2 and e.name == "RX FIFO full")
    assert s.sm_state(2)["rx_level"] == 4 and full.data == 1


def test_mask_turns_types_off(s):
    s.trace_config(type_mask=~(1 << 0x01) & 0xFFFF_FFFF)    # no stall-begin records
    s.load("blink", sm=0, pinctrl=Pins(set_base=25, set_count=1))
    s.step(50)
    s.push(0, 1)
    s.step(50)
    assert not [e for e in s.events if e.name == "stall begin"]
    assert [e for e in s.events if e.name == "stall end"]


def test_backpressure_drops_are_counted(s):
    """Reader not taking records: the FIFO fills, losses are counted, a DROPPED record follows."""
    s.trace_config(ready=False)
    prog = assemble(".program t\nirq set 0\nirq clear 0\njmp 0")
    s.load(prog["programs"][0], sm=0)
    s.step(3000)
    dropped, _ = s.trace_counters()
    assert dropped > 0
    s.trace_config(ready=True)
    s.load(assemble(".program h\nhalt:\njmp halt")["programs"][0], sm=0)
    s.step(2000)
    lost = sum(e.data for e in s.events if e.name == "DROPPED")
    final, _ = s.trace_counters()
    assert final >= dropped and lost == min(final, 0xFFFF), (lost, final)
