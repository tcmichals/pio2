# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""Bench: reverse step and breakpoints on the Verilator model ([SPEC-DBG-04], [SPEC-DBG-06])."""
# @verifies [SPEC-DBG-04] [SPEC-DBG-06] docs/DEBUGGER_SPECIFICATION.md

import pytest

from pio2.debug.bench import Bench, Watch
from pio2.debug.session import Session


@pytest.fixture
def b():
    s = Session()
    bench = Bench(session=s)
    yield bench
    s.close()


def snapshot_view(s):
    """What a user sees at this cycle: pins and every SM's registers."""
    return s.cycle, s.pins(), [s.sm_state(sm) for sm in range(4)]


def test_back_returns_the_exact_earlier_state(b):
    """UART TX with its FIFO feeder: run 2000 clocks, remember, run 5000 more, step back to 2000.
    Pins and all SM registers must match bit for bit, and running forward again repeats them."""
    b.load("UART TX")
    b.run(2000)
    t0, before = b.s.cycle, snapshot_view(b.s)     # loading the example runs clocks too
    b.run(5000)
    after = snapshot_view(b.s)
    b.back_to(t0)
    assert snapshot_view(b.s) == before
    b.run(5000)
    assert snapshot_view(b.s) == after


def test_pc_breakpoint_stops_where_the_pc_arrives(b):
    """Pick an address SM0 keeps visiting (the loop, from the per-clock trace) other than where it is now; the run stops
    on the clock SM0 arrives there."""
    b.load("Blink")
    b.run(20_000)                               # blink waits ~3000 clocks at each of two addresses
    tr = b.s.trace
    visited = {tr.pcs[4 * i] for i in range(len(tr) - 15_000, len(tr))}   # the loop, not the start-up
    target = next(a for a in sorted(visited) if a != b.s.sm_state(0)["pc"])
    b.watches.append(Watch.pc(0, target))
    stop = b.run(200_000)
    assert stop is not None, "breakpoint never fired"
    assert b.s.sm_state(0)["pc"] == target
    assert b.s.cycle == stop.cycle
