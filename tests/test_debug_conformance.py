# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""The conformance programs (src/programs/conformance) stepped in the debugger give the results the
cocotb suite checks (tests/cocotb/test_asp_pio_conformance_cocotb.py), case by case.

Every case runs twice: clocks stepped in batches (`by="clock"`) and the program stepped one
instruction at a time with `Session.step_instr` (`by="instr"`, [SPEC-DBG-03]). The waits are the
same clock counts the cocotb test waits; the register sequence is the same. As in the cocotb
bench, every input line reads 0 (`i_gpio = 0`, no pull-up) unless the test drives it.
"""
# @verifies [SPEC-DBG-03] [SPEC-DBG-09] docs/DEBUGGER_SPECIFICATION.md

import pytest

from pio2.debug.session import (CLKDIV, EXECCTRL, INSTR, PINCTRL, REG_CTRL, REG_FSTAT, REG_IRQ, RXF,
                                SHIFTCTRL, SM, Pins, Session)

SET_PIN0 = Pins(set_base=0, set_count=1)
AUTOPUSH, AUTOPULL = 1 << 16, 1 << 17
OUT_RIGHT = 1 << 19
FJOIN_TX, FJOIN_RX = 1 << 30, 1 << 31


def push_thresh(n):
    return (n & 31) << 20


def pull_thresh(n):
    return (n & 31) << 25


@pytest.fixture(scope="module")
def sess():
    s = Session()
    yield s
    s.close()


class Bench:
    """The cocotb helpers (run, drain, pin, ClockCycles) on a Session."""

    def __init__(self, s, by):
        self.s, self.by = s, by
        s.reset()
        s.set_pull(0)
        s.drive(7, None)
        self.retired = 0

    def wait(self, n, sm=0):
        """n clocks; by instruction: step_instr on `sm` until n clocks have gone by."""
        s = self.s
        if self.by == "clock":
            s.step(n)
            return
        end = s.cycle + n
        while s.cycle < end:
            if not (s._get("sm_en") >> sm) & 1:
                s.step(end - s.cycle)
                break
            k = s.step_instr(sm, limit=end - s.cycle)
            if k < end - s.cycle or s.cycle == end:
                self.retired += 1

    def run(self, name, sm=0, pinctrl=None, shiftctrl=0, execctrl=0, enable=True):
        return self.s.load(name, sm=sm, pinctrl=pinctrl, shiftctrl=shiftctrl, execctrl=execctrl,
                           enable=enable)

    def drain(self, sm=0, limit=8):
        out = []
        while len(out) < limit and not self.s.rx_empty(sm):
            out.append(self.s.read(SM(sm, RXF)))
        return out

    def pin(self, n):
        return self.s.pins()[0] >> n & 1

    def pc(self, sm=0):
        return self.s.read(SM(sm, INSTR)) & 0x1F


BY = ["clock", "instr"]


@pytest.mark.parametrize("by", BY)
def test_pull_block_stalls_on_empty_fifo(sess, by):
    b = Bench(sess, by)
    b.run("c_pull_block", pinctrl=SET_PIN0)
    b.wait(50)
    assert b.pin(0) == 0
    assert b.pc() == 0
    assert sess.sm_state(0)["stalled"]
    sess.push(0, 0x12345678)
    b.wait(20)
    assert b.drain() == [0x12345678]


@pytest.mark.parametrize("by", BY)
def test_pull_noblock_copies_x(sess, by):
    b = Bench(sess, by)
    b.run("c_pull_noblock")
    b.wait(30)
    assert b.drain() == [21]


@pytest.mark.parametrize("by", BY)
def test_push_noblock_full_is_dropped(sess, by):
    b = Bench(sess, by)
    b.run("c_push_noblock", pinctrl=SET_PIN0)
    b.wait(60)
    assert b.pin(0) == 1
    assert b.drain() == [4, 3, 2, 1]


@pytest.mark.parametrize("by", BY)
def test_autopush_pushes_after_shift(sess, by):
    b = Bench(sess, by)
    b.run("c_autopush", shiftctrl=AUTOPUSH | push_thresh(8))
    b.wait(30)
    assert b.drain() == [0x5A]


@pytest.mark.parametrize("by", BY)
def test_autopush_full_stalls_without_loss(sess, by):
    b = Bench(sess, by)
    b.run("c_autopush_full", shiftctrl=AUTOPUSH)
    got = []
    for _ in range(40):
        b.wait(30)
        got += b.drain(limit=2)
        if len(got) >= 32:
            break
    assert got == list(range(31, -1, -1))


@pytest.mark.parametrize("by", BY)
def test_autopull_refill_and_stall(sess, by):
    b = Bench(sess, by)
    b.run("c_autopull", shiftctrl=AUTOPULL | pull_thresh(0) | OUT_RIGHT)
    b.wait(30)
    assert b.drain() == []
    sess.push(0, 0x44332211)
    b.wait(60)
    assert b.drain() == [0x11, 0x22, 0x33, 0x44]
    b.wait(60)
    assert b.drain() == []


@pytest.mark.parametrize("by", BY)
def test_mov_osr_resets_count(sess, by):
    b = Bench(sess, by)
    b.run("c_mov_osr")
    b.wait(40)
    assert b.drain() == [1]


@pytest.mark.parametrize("by", BY)
def test_mov_isr_clears_count(sess, by):
    b = Bench(sess, by)
    b.run("c_mov_isr", shiftctrl=AUTOPUSH | push_thresh(8))
    b.wait(40)
    assert b.drain() == [0x33]


@pytest.mark.parametrize("by", BY)
def test_mov_status(sess, by):
    b = Bench(sess, by)
    b.run("c_mov_status", execctrl=1)
    b.wait(20)
    assert b.drain() == [0xFFFFFFFF]
    b.run("c_mov_status", execctrl=1, enable=False)
    sess.push(0, 0xAAAA)
    sess.write(REG_CTRL, 1)
    b.wait(20)
    assert b.drain() == [0]


@pytest.mark.parametrize("by", BY)
def test_sideset_opt_count_includes_enable(sess, by):
    b = Bench(sess, by)
    b.run("c_sideset_opt", pinctrl=Pins(sideset_base=3), enable=False)
    c0 = sess.cycle
    sess.write(REG_CTRL, 1)
    b.wait(12)
    # The enable write itself takes clocks, and the program starts inside them (Session.write
    # returns later than cocotbext-axi's write does), so the pin is read from the per-clock trace
    # from the start of the write instead of being sampled after it returns.
    tr = sess.trace
    seen = [tr.out[c - tr.start] >> 3 & 1 for c in range(c0, sess.cycle)]
    assert 1 in seen and seen[-1] == 0, seen
    first = seen.index(1)
    assert seen[first:first + 3] == [1, 1, 1], seen


@pytest.mark.parametrize("by", BY)
def test_irq_wait_waits_for_clear(sess, by):
    b = Bench(sess, by)
    b.run("c_irq_wait", pinctrl=SET_PIN0)
    b.wait(40)
    assert sess.read(REG_IRQ) & 1 == 1
    assert b.pin(0) == 0
    sess.write(REG_IRQ, 1)
    b.wait(20)
    assert b.pin(0) == 1


@pytest.mark.parametrize("by", BY)
def test_irq_rel_keeps_bit2(sess, by):
    b = Bench(sess, by)
    b.run("c_irq_rel", sm=1)
    b.wait(20, sm=1)
    assert sess.read(REG_IRQ) & 0xFF == 1 << 5


@pytest.mark.parametrize("by", BY)
def test_wait_gpio(sess, by):
    b = Bench(sess, by)
    b.run("c_wait_gpio", pinctrl=SET_PIN0)
    b.wait(40)
    assert b.pin(0) == 0
    sess.drive(7, 1)
    b.wait(20)
    assert b.pin(0) == 1


def test_restart_keeps_pc_fifo_pins(sess):
    b = Bench(sess, "clock")
    sess.write(SM(0, PINCTRL), SET_PIN0.word())
    for ins in (0xE081, 0xE001, 0x0007, 0xE025):
        sess.write(SM(0, INSTR), ins)
    sess.push(0, 0xAB)
    sess.write(REG_CTRL, 1 << 4)
    b.wait(4)
    assert b.pc() == 7
    assert sess.pins()[1] & 1 == 1 and b.pin(0) == 1
    for ins in (0x8080, 0xA0C7, 0x8000, 0xA0C1, 0x8000):
        sess.write(SM(0, INSTR), ins)
    b.wait(2)
    assert b.drain() == [0xAB, 5]


def test_pin_latch_latest_writer_wins(sess):
    b = Bench(sess, "clock")
    sess.write(SM(0, PINCTRL), SET_PIN0.word())
    sess.write(SM(1, PINCTRL), SET_PIN0.word())
    sess.write(SM(1, INSTR), 0xE080)
    sess.write(SM(0, INSTR), 0xE001)
    sess.write(SM(0, INSTR), 0xE081)
    b.wait(2)
    assert sess.pins()[1] & 1 == 1 and b.pin(0) == 1
    sess.write(SM(1, INSTR), 0xE000)
    b.wait(2)
    assert sess.pins()[1] & 1 == 1 and b.pin(0) == 0
    sess.write(REG_CTRL, 0x3 << 4)
    b.wait(2)
    assert sess.pins()[1] & 1 == 1 and b.pin(0) == 0


def test_sm_instr_runs_while_disabled(sess):
    b = Bench(sess, "clock")
    sess.write(SM(0, PINCTRL), SET_PIN0.word())
    sess.write(SM(0, INSTR), 0xE001)
    b.wait(5)
    assert b.pin(0) == 1
    sess.write(SM(0, INSTR), 0x0005)
    b.wait(5)
    assert b.pc() == 5


def test_fifo_join_tx_and_flush(sess):
    Bench(sess, "clock")
    tx_full = lambda: sess.read(REG_FSTAT) >> 24 & 1  # noqa: E731
    sess.write(SM(0, SHIFTCTRL), FJOIN_TX)
    for i in range(7):
        sess.push(0, i)
        assert not tx_full()
    sess.push(0, 7)
    assert tx_full()
    sess.write(SM(0, SHIFTCTRL), 0)
    for i in range(3):
        sess.push(0, i)
        assert not tx_full()
    sess.push(0, 3)
    assert tx_full()


def test_fifo_join_rx_8_words(sess):
    b = Bench(sess, "clock")
    sess.write(SM(0, SHIFTCTRL), FJOIN_RX)
    for k in range(10):
        sess.write(SM(0, INSTR), 0xE020 | (k + 1))
        sess.write(SM(0, INSTR), 0xA0C1)
        sess.write(SM(0, INSTR), 0x8000)
    assert b.drain(limit=12) == [1, 2, 3, 4, 5, 6, 7, 8]
    assert sess.sm_state(0)["rx_fifo"] == []


def test_exec_stalled_latches_until_done(sess):
    b = Bench(sess, "clock")
    sess.write(SM(0, INSTR), 0x80A0)
    b.wait(10)
    assert sess.read(SM(0, EXECCTRL)) >> 31 == 1
    assert sess.sm_state(0)["exec_pending"]
    sess.push(0, 0x1234_5678)
    b.wait(4)
    assert sess.read(SM(0, EXECCTRL)) >> 31 == 0
    sess.write(SM(0, INSTR), 0xA0C7)
    sess.write(SM(0, INSTR), 0x8020)
    b.wait(2)
    assert b.drain() == [0x1234_5678]


def test_instruction_stepping_really_steps(sess):
    """The `instr` runs above retire instructions one at a time (not one big batch)."""
    b = Bench(sess, "instr")
    b.run("c_push_noblock", pinctrl=SET_PIN0)
    b.wait(60)
    assert b.retired >= 10, b.retired
    assert sess.read(SM(0, CLKDIV)) == 1 << 16
    assert b.drain() == [4, 3, 2, 1]
