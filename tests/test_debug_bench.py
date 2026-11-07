# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""Bench: reverse step and breakpoints on the Verilator model ([SPEC-DBG-04], [SPEC-DBG-06])."""
# @verifies [SPEC-DBG-04] [SPEC-DBG-06] docs/DEBUGGER_SPECIFICATION.md

import pytest

from pio2.debug.bench import Bench, Watch
from pio2.debug.examples import EXAMPLES
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


# ---------------------------------------------------------------- stepping ([SPEC-DBG-03])
# @verifies [SPEC-DBG-03] docs/DEBUGGER_SPECIFICATION.md
STEP_CLOCKS = 3000


def trace_from(s, c0, n):
    """Per-clock out / dir / in / PCs of clocks c0 .. c0 + n - 1, as plain lists."""
    tr = s.trace
    i = c0 - tr.start
    return (list(tr.out[i:i + n]), list(tr.dir[i:i + n]), list(tr.inp[i:i + n]),
            list(tr.pcs[4 * i:4 * (i + n)]))


def traced(name, advance):
    """Load `name` in a fresh model, call advance(bench) until STEP_CLOCKS have gone by."""
    s = Session()
    try:
        bench = Bench(session=s)
        bench.load(name)
        c0 = s.cycle
        while s.cycle < c0 + STEP_CLOCKS:
            advance(bench)
        return trace_from(s, c0, STEP_CLOCKS)
    finally:
        s.close()


def first_difference(a, b):
    for field, x, y in zip(("out", "dir", "in", "pcs"), a, b):
        if x != y:
            k = next(i for i, (p, q) in enumerate(zip(x, y)) if p != q)
            return f"{field}[{k}]: {x[k]:#x} != {y[k]:#x}"
    return None


@pytest.mark.parametrize("name", list(EXAMPLES))
def test_stepping_by_clock_gives_the_run_trace(name):
    """A step must not change what the program does: same pins and PCs on every clock as a run."""
    run = traced(name, lambda b: b.run(STEP_CLOCKS))
    assert first_difference(run, traced(name, lambda b: b.step_clock())) is None


@pytest.mark.parametrize("name", list(EXAMPLES))
def test_stepping_by_instruction_gives_the_run_trace(name):
    run = traced(name, lambda b: b.run(STEP_CLOCKS))

    def step(b):
        sm = min(b.s.programs)
        if not (b.s._get("sm_en") >> sm) & 1:
            b.step_clock()
        else:
            b.step_instr(sm, limit=64)
    assert first_difference(run, traced(name, step)) is None


@pytest.mark.parametrize("name", list(EXAMPLES))
def test_step_calls_return_the_clocks_actually_run(name):
    s = Session()
    try:
        bench = Bench(session=s)
        bench.load(name)
        sm = min(s.programs)
        for _ in range(300):
            c0 = s.cycle
            n = bench.step_clock()
            assert n == s.cycle - c0 >= 1
        for _ in range(100):
            c0 = s.cycle
            n = bench.step_instr(sm, limit=2000)
            assert n == s.cycle - c0 >= 1
    finally:
        s.close()


@pytest.mark.parametrize("name", [n for n in EXAMPLES if n in ("Blink", "Square wave", "Button to LED")])
def test_without_a_host_script_a_step_is_exact(name):
    """No part makes register accesses after the set-up here, so one clock is one clock and one
    instruction is one PC change at most, checked against the RTL trace."""
    s = Session()
    try:
        bench = Bench(session=s)
        bench.load(name)
        bench.run(1000)
        sm = min(s.programs)
        for _ in range(200):
            c0 = s.cycle
            assert bench.step_clock() == 1 and s.cycle == c0 + 1
        for _ in range(200):
            pc0 = s.sm_state(sm)["pc"]
            c0 = s.cycle
            bench.step_instr(sm, limit=2000)
            pcs = [pc0] + [s.trace.pcs[4 * (c - s.trace.start) + sm] for c in range(c0, s.cycle)]
            assert sum(p != q for p, q in zip(pcs, pcs[1:])) <= 1, pcs
    finally:
        s.close()


def test_blink_loop_retires_one_jmp_per_step(b):
    """Blink's delay loop stays on one address: the X register is the proof, one less per step."""
    b.load("Blink")
    b.run(1000)
    assert b.s.sm_state(0)["pc"] == 4
    x = b.s.sm_state(0)["reg_x"]
    for k in range(1, 21):
        assert b.step_instr(0) == 1
        assert b.s.sm_state(0)["reg_x"] == x - k
