# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""pio2.debug.Session on the Verilator model ([SPEC-DBG-01], [SPEC-DBG-02], [SPEC-DBG-03],
[SPEC-DBG-09]) and the first parts ([SPEC-DBG-15])."""
# @verifies [SPEC-DBG-01] [SPEC-DBG-02] [SPEC-DBG-03] [SPEC-DBG-09] [SPEC-DBG-15] docs/DEBUGGER_SPECIFICATION.md

import pytest

from pio2.debug.bench import Bench
from pio2.debug.examples import EXAMPLES, button_led, uart_hello
from pio2.debug.session import Pins, Session, SimError
from pio2.parts import Button, Led, Part, UartTerminal


@pytest.fixture
def s():
    sess = Session()
    yield sess
    sess.close()


def load(sess, example):
    """An example on a Bench, the run loop the GUI and the CLI use (parts attached)."""
    b = Bench(session=sess)
    return b, b.load(example)


def test_registers_round_trip_through_axi4lite(s):
    s.write(0x10C, 0x1234_5678)                      # SM0_PINCTRL, plain register
    assert s.read(0x10C) == 0x1234_5678
    assert s.sm_state(0)["pinctrl"] == 0x1234_5678   # VPI sees the same register


def test_one_session_per_process(s):
    with pytest.raises(SimError):
        Session()


def test_blink_period_follows_the_delay(s):
    """pico-examples blink: half period = delay + 3 clocks (mov, set, delay + 1 jmp)."""
    s.load("blink", sm=0, pinctrl=Pins(set_base=25, set_count=1), enable=False)
    s.set_pindirs(0, 1 << 25, 1 << 25)
    s.enable(0)
    for delay in (20, 57):
        s.push(0, delay) if delay == 20 else None
        if delay != 20:
            s.reset()
            s.load("blink", sm=0, pinctrl=Pins(set_base=25, set_count=1), enable=False)
            s.set_pindirs(0, 1 << 25, 1 << 25)
            s.enable(0)
            s.push(0, delay)
        s.step(20 * (delay + 3))
        tr = s.trace
        line = [(tr.inp[i] >> 25) & 1 for i in range(len(tr))]
        edges = [i for i in range(1, len(line)) if line[i] != line[i - 1]]
        periods = {b - a for a, b in zip(edges[2:], edges[3:])}
        assert periods == {delay + 3}, (delay, periods)


def test_trace_has_no_gaps(s):
    """Clocks spent in register accesses are in the trace too ([SPEC-DBG-01] pads)."""
    start = s.trace.start
    for i in range(50):
        s.write(0x10C, i)
        s.step(7)
        s.read(0x10C)
    assert s.trace.start + len(s.trace) == s.cycle and s.trace.start == start


def test_uart_tx_reaches_the_terminal_1000_chars(s):
    b, ex = load(s, uart_hello)
    b.run(1000 * 160 + 2000)
    term = next(p for p in ex.parts if isinstance(p, UartTerminal))
    assert isinstance(term, UartTerminal) and isinstance(term, Part)
    assert term.framing_errors == 0
    msg = "Hello from the pio2 RTL!\r\n"
    assert len(term.text) >= 1000
    assert term.text == (msg * (len(term.text) // len(msg) + 1))[:len(term.text)]


def test_button_drives_led_through_mov_pins(s):
    b, ex = load(s, button_led)
    btn = next(p for p in ex.parts if isinstance(p, Button))
    led = next(p for p in ex.parts if isinstance(p, Led))
    b.run(200)
    assert not led.on
    btn.set(s, True)
    run_more = 50
    s.step(run_more)
    led.update(s)
    assert led.on
    btn.set(s, False)
    s.step(run_more)
    led.update(s)
    assert not led.on


def test_step_instr_retires_one_instruction(s):
    """Stepping by instruction walks blink's PC 2 -> 3 -> 4 and then stays on the jmp-to-self
    loop at 4 for exactly `delay` more steps."""
    s.load("blink", sm=0, pinctrl=Pins(set_base=25, set_count=1))
    s.push(0, 5)
    s.step(10)                                      # pull + out y have run
    for _ in range(200):                            # bounded: a broken step must fail, not hang
        if s.sm_state(0)["pc"] == 2:
            break
        s.step_instr(0)
    assert s.sm_state(0)["pc"] == 2
    pcs = []
    for _ in range(9):
        s.step_instr(0)
        pcs.append(s.sm_state(0)["pc"])
    assert pcs[:2] == [3, 4], pcs
    assert pcs[2:].count(4) == 5 and pcs[pcs.index(5)] == 5, pcs


def test_stalled_pull_is_not_retired(s):
    """A blocking pull on an empty FIFO stalls: stepping by instruction does not retire it."""
    s.load("blink", sm=0, pinctrl=Pins(set_base=25, set_count=1))
    s.step(20)
    st = s.sm_state(0)
    assert st["pc"] == 0 and st["stalled"], st
    assert s.step_instr(0, limit=200) == 200            # never retired
    assert s.sm_state(0)["pc"] == 0
    s.push(0, 3)                                        # the write's own clocks let the pull finish
    # (how many instructions run during the write depends on the access latency through the core's
    # crossbar; the point is that the pull retired)
    assert s.sm_state(0)["pc"] != 0 and not s.sm_state(0)["stalled"]


def test_step_instr_runs_one_clock_at_divider_1(s):
    """At clock divider 1 an unstalled SM retires one instruction per clock: no hidden clocks."""
    s.load("blink", sm=0, pinctrl=Pins(set_base=25, set_count=1))
    s.push(0, 5)
    s.step(10)
    c0 = s.cycle
    for _ in range(50):
        assert s.step_instr(0) == 1
    assert s.cycle == c0 + 50


@pytest.mark.parametrize("name", list(EXAMPLES))
def test_every_example_runs_headless(s, name):
    b, ex = load(s, name)
    b.run(5000)
    assert s.cycle >= 5000


def test_program_loaded_at_an_offset_is_relocated(s):
    """pio_add_program adds the load offset to every JMP target; blink at offset 21 (as in a tile
    shared with other programs) keeps blinking with the same period as at offset 0."""
    def half_periods(offset):
        s.reset()
        s.load("blink", sm=0, offset=offset, pinctrl=Pins(set_base=25, set_count=1), enable=False)
        s.set_pindirs(0, 1 << 25, 1 << 25)
        s.enable(0)
        s.push(0, 50)
        s.step(2000)
        tr = s.trace
        lv = [(tr.out[i] >> 25) & 1 for i in range(len(tr))]
        edges = [i for i in range(1, len(lv)) if lv[i] != lv[i - 1]]
        return [b - a for a, b in zip(edges, edges[1:])][2:6]

    at0, at21 = half_periods(0), half_periods(21)
    assert at0 and at0 == at21, (at0, at21)
    assert all(21 <= s.sm_state(0)["pc"] < 21 + 8 for _ in range(1))
