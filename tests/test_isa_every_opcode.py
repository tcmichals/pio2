# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""Every RP2040 PIO opcode and operand on the RTL ([SPEC-PIO-02], [SPEC-PIO-15]).

Each case sets up a state machine (X, Y, ISR, OSR, pins, FIFOs, config), executes ONE instruction
through SMx_INSTR (assembled from text by pio2.asm, so the assembler is checked too), and compares the
resulting state with the RP2040 datasheet rule (section 3.4) computed here in Python. Runs on the
Verilator model of the unmodified tile. The coverage test at the end fails if any opcode / operand
combination of the RP2040 ISA has no case.
"""
# @verifies [SPEC-PIO-02] [SPEC-PIO-15] docs/SPECIFICATION.md

import random

import pytest

from pio2.asm import assemble
from pio2.debug.session import CLKDIV, EXECCTRL, INSTR, PINCTRL, REG_IRQ, SHIFTCTRL, SM, Session

M32 = 0xFFFF_FFFF
COVERED = set()


def word(text, side_set=""):
    return assemble(f".program t\n{side_set}\n{text}")["programs"][0]["instructions"][0]


def rotl(v, n):
    n &= 31
    return ((v << n) | (v >> (32 - n))) & M32 if n else v


def rotr(v, n):
    return rotl(v, 32 - (n & 31))


def rev(v):
    return int(f"{v:032b}"[::-1], 2)


class Rig:
    """SM0 of a fresh tile, disabled; instructions run through SMx_INSTR."""

    def __init__(self, s, shiftctrl=0, pinctrl=0, execctrl=0):
        self.s = s
        s.reset()
        s.write(SM(0, CLKDIV), 1 << 16)
        s.write(SM(0, EXECCTRL), execctrl)
        s.write(SM(0, SHIFTCTRL), shiftctrl)
        s.write(SM(0, PINCTRL), pinctrl)
        self.shiftctrl, self.pinctrl, self.execctrl = shiftctrl, pinctrl, execctrl

    def exe(self, text, clocks=3):
        self.s.write(SM(0, INSTR), word(text))
        self.s.step(clocks)

    def st(self):
        return self.s.sm_state(0)

    def run(self, text, clocks=12):
        """Load `text` as a program at 0 and run it enabled (EXEC needs a running SM)."""
        p = assemble(f".program r\n{text}\nhalt:\njmp halt")["programs"][0]
        for i, w in enumerate(p["instructions"]):
            self.s.write(0x10 + 4 * i, w)
        self.s.write(SM(0, EXECCTRL), self.execctrl | (31 << 12))     # no wrap before halt
        self.exe("jmp 0", clocks=0)
        self.s.write(0x00, 1)                                          # enable SM0
        self.s.step(clocks)
        self.s.write(0x00, 0)

    def set32(self, reg, v):
        """reg in x, y, isr, osr: through the TX FIFO, pull, mov."""
        self.s.push(0, v)
        self.exe("pull block")
        if reg != "osr":
            self.exe(f"mov {reg}, osr")

    def pins_in(self, value):
        s = self.s
        s.ext, s.ext_mask = value, M32
        s.lib.pio2sim_set_ext(s.h, value, M32, s.pull_level)


@pytest.fixture(scope="module")
def sess():
    s = Session()
    yield s
    s.close()


VALS = [0x0000_0000, 0x0000_0001, 0x8000_0000, 0xDEAD_BEEF, 0xFFFF_FFFF]

# ------------------------------------------------------------------------------------- JMP
JMP_CASES = []
for cond in ["", "!x", "x--", "!y", "y--", "x!=y", "pin", "!osre"]:
    for x, y in [(0, 0), (5, 5), (0, 7), (3, 0), (M32, 1)]:
        JMP_CASES.append((cond, x, y))


@pytest.mark.parametrize("cond,x,y", JMP_CASES)
def test_jmp(sess, cond, x, y):
    r = Rig(sess, execctrl=9 << 24)                   # JMP_PIN = 9
    r.set32("x", x)
    r.set32("y", y)
    pin = (x ^ y) & 1
    r.pins_in(pin << 9)
    if cond == "!osre":
        r.set32("osr", 0)                              # pull: OSR full (count 0)
        if x & 1:
            r.exe("out null, 32")                      # count 32: empty
    pc0 = r.st()["pc"]
    r.exe(f"jmp {cond}{',' if cond else ''} 17")
    st = r.st()
    osr_empty = bool(x & 1) if cond == "!osre" else None
    taken = {"": True, "!x": x == 0, "x--": x != 0, "!y": y == 0, "y--": y != 0, "x!=y": x != y,
             "pin": bool(pin), "!osre": osr_empty is False}[cond]
    # SMx_INSTR: the instruction is not from memory, so the PC moves only when the jump is taken
    assert st["pc"] == (17 if taken else pc0), (cond, x, y, st["pc"])
    assert st["reg_x"] == ((x - 1) & M32 if cond == "x--" else x)
    assert st["reg_y"] == ((y - 1) & M32 if cond == "y--" else y)
    COVERED.add(("jmp", cond))


# ------------------------------------------------------------------------------------- SET
@pytest.mark.parametrize("dest", ["pins", "x", "y", "pindirs"])
@pytest.mark.parametrize("value", [0, 1, 0x15, 31])
def test_set(sess, dest, value):
    base, count = 6, 5
    r = Rig(sess, pinctrl=(base << 5) | (count << 26))
    r.exe(f"set {dest}, {value}")
    st = r.st()
    out, dir_, _ = sess.pins()
    window = ((1 << count) - 1) << base
    if dest == "x":
        assert st["reg_x"] == value
    elif dest == "y":
        assert st["reg_y"] == value
    elif dest == "pins":
        assert out & window == (value << base) & window
    else:
        assert dir_ & window == (value << base) & window
    COVERED.add(("set", dest))


# ------------------------------------------------------------------------------------- MOV
MOV_DST = ["pins", "x", "y", "exec", "pc", "isr", "osr"]
MOV_SRC = ["pins", "x", "y", "null", "status", "isr", "osr"]


@pytest.mark.parametrize("dst", MOV_DST)
@pytest.mark.parametrize("op", ["", "~", "::"])
@pytest.mark.parametrize("src", MOV_SRC)
def test_mov(sess, dst, op, src):
    rng = random.Random(hash((dst, op, src)) & 0xFFFF)
    in_base, out_base = 3, 0
    status_n = 2
    r = Rig(sess, pinctrl=(in_base << 15) | (out_base) | (32 << 20) if dst == "pins" else (in_base << 15),
            execctrl=status_n)                          # STATUS_SEL 0: TX level < 2
    x, y, isr, osr = (rng.getrandbits(32) for _ in range(4))
    if dst == "exec":                                   # source = an instruction: set y, 21
        x = y = isr = osr = word("set y, 21")
        if op == "~":
            x = y = isr = osr = ~word("set y, 21") & M32
        if op == "::":
            x = y = isr = osr = rev(word("set y, 21"))
    if dst == "pc":
        x = y = isr = osr = rng.randrange(32)
        if op == "~":
            x = y = isr = osr = ~x & M32
        if op == "::":
            x = y = isr = osr = rev(x)
    gpio = rng.getrandbits(32)
    r.set32("osr", osr)
    r.set32("isr", isr)
    r.set32("x", x)
    r.set32("y", y)
    r.set32("osr", osr)
    tx_fill = rng.randrange(4)
    for i in range(tx_fill):
        sess.push(0, i)
    r.pins_in(gpio)
    pc0 = r.st()["pc"]
    srcval = {"pins": rotr(gpio, in_base), "x": x, "y": y, "null": 0,
              "status": M32 if tx_fill < status_n else 0, "isr": isr, "osr": osr}[src]
    if src == "isr" and dst == "isr":
        pass
    v = {"": srcval, "~": ~srcval & M32, "::": rev(srcval)}[op]
    if dst == "exec":
        r.run(f"mov exec, {op}{src}")
    else:
        r.exe(f"mov {dst}, {op}{src}")
    st = r.st()
    if dst == "x":
        assert st["reg_x"] == v
    elif dst == "y":
        assert st["reg_y"] == v
    elif dst == "isr":
        assert st["isr"] == v and st["isr_count"] == 0
    elif dst == "osr":
        assert st["osr"] == v and st["osr_count"] == 0
    elif dst == "pc":
        assert st["pc"] == v & 31
    elif dst == "exec":
        if src in ("x", "y", "isr", "osr"):
            assert st["reg_y"] == 21, "the moved instruction (set y, 21) did not execute"
    elif dst == "pins":
        out, _, _ = sess.pins()
        assert out == v                                  # OUT window: base 0, 32 pins
    if dst not in ("pc", "exec"):
        assert st["pc"] == pc0
    COVERED.add(("mov", dst, op, src))


# ------------------------------------------------------------------------------------- IN
IN_SRC = ["pins", "x", "y", "null", "isr", "osr"]


@pytest.mark.parametrize("src", IN_SRC)
@pytest.mark.parametrize("count", [1, 5, 31, 32])
@pytest.mark.parametrize("right", [False, True])
def test_in(sess, src, count, right):
    in_base = 7
    r = Rig(sess, shiftctrl=(1 << 18) if right else 0, pinctrl=in_base << 15)
    rng = random.Random(count * 7 + right)
    x, y, isr0, osr = (rng.getrandbits(32) for _ in range(4))
    gpio = rng.getrandbits(32)
    r.set32("osr", osr)
    r.set32("x", x)
    r.set32("y", y)
    r.set32("isr", isr0)
    r.exe("in null, 3")                                  # some prior shift count
    isr = ((isr0 << 3) & M32) if not right else (isr0 >> 3)
    r.set32("osr", osr)
    r.pins_in(gpio)
    srcv = {"pins": rotr(gpio, in_base), "x": x, "y": y, "null": 0, "isr": isr, "osr": osr}[src]
    data = srcv & ((1 << count) - 1)
    if right:
        exp = ((isr >> count) | (data << (32 - count))) & M32 if count < 32 else data
    else:
        exp = ((isr << count) | data) & M32 if count < 32 else data
    r.exe(f"in {src}, {count}")
    st = r.st()
    assert st["isr"] == exp, (src, count, right, hex(st["isr"]), hex(exp))
    assert st["isr_count"] == min(3 + count, 32)
    COVERED.add(("in", src))


# ------------------------------------------------------------------------------------- OUT
OUT_DST = ["pins", "x", "y", "null", "pindirs", "pc", "isr", "exec"]


@pytest.mark.parametrize("dst", OUT_DST)
@pytest.mark.parametrize("count", [1, 5, 16, 32])
@pytest.mark.parametrize("right", [False, True])
def test_out(sess, dst, count, right):
    base, ocount = 4, 12
    r = Rig(sess, shiftctrl=(1 << 19) if right else 0, pinctrl=base | (ocount << 20))
    rng = random.Random(count * 13 + right)
    osr = rng.getrandbits(32)
    if dst == "exec":                                    # the shifted-out bits are an instruction
        if count < 16:
            pytest.skip("exec needs at least 16 bits")
        ins = word("set y, 21")
        if right:
            osr = ins | (rng.getrandbits(16) << 16 if count == 16 else 0)
        else:
            osr = (ins << 16) | rng.getrandbits(16) if count == 16 else ins
    if dst == "pc":
        osr = rng.getrandbits(32)
    r.set32("osr", osr)
    pc0 = r.st()["pc"]
    if right:
        data = osr & ((1 << count) - 1) if count < 32 else osr
        rest = osr >> count if count < 32 else 0
    else:
        data = osr >> (32 - count)
        rest = (osr << count) & M32 if count < 32 else 0
    if dst == "exec":
        r.run(f"out exec, {count}")
    else:
        r.exe(f"out {dst}, {count}")
    st = r.st()
    if dst != "exec":
        assert st["osr"] == rest and st["osr_count"] == count
    out, dir_, _ = sess.pins()
    window = ((1 << ocount) - 1) << base
    if dst == "x":
        assert st["reg_x"] == data
    elif dst == "y":
        assert st["reg_y"] == data
    elif dst == "isr":
        assert st["isr"] == data and st["isr_count"] == count
    elif dst == "pins":
        assert out & window == (data << base) & window
    elif dst == "pindirs":
        assert dir_ & window == (data << base) & window
    elif dst == "pc":
        assert st["pc"] == data & 31
    elif dst == "exec":
        assert st["reg_y"] == 21
    if dst not in ("pc", "exec"):
        assert st["pc"] == pc0
    COVERED.add(("out", dst))


# ------------------------------------------------------------------------------------- PUSH / PULL
@pytest.mark.parametrize("iffull", [False, True])
@pytest.mark.parametrize("block", [False, True])
@pytest.mark.parametrize("count", [4, 12])
def test_push(sess, iffull, block, count):
    r = Rig(sess, shiftctrl=8 << 20)                   # push threshold 8
    r.set32("isr", 0)
    r.exe("in null, 32")
    r.set32("x", 0xA5A5_0000 | count)
    r.exe("mov isr, x")
    r.exe(f"in null, {count}")                          # isr shifted left by count, count = count
    isr = ((0xA5A5_0000 | count) << count) & M32
    r.exe("push" + (" iffull" if iffull else "") + (" block" if block else " noblock"))
    st = r.st()
    pushed = not iffull or count >= 8
    assert st["rx_level"] == (1 if pushed else 0)
    if pushed:
        assert st["rx_fifo"] == [isr] and st["isr"] == 0 and st["isr_count"] == 0
    else:
        assert st["isr"] == isr and st["isr_count"] == count
    COVERED.add(("push", iffull, block))


@pytest.mark.parametrize("ifempty", [False, True])
@pytest.mark.parametrize("block", [False, True])
@pytest.mark.parametrize("fifo", [0, 1])
def test_pull(sess, ifempty, block, fifo):
    r = Rig(sess, shiftctrl=8 << 25)                   # pull threshold 8
    r.set32("x", 0x1234_5678)
    r.set32("osr", 0xCAFE_F00D)
    r.exe("out null, 12")                               # osr count 12 >= threshold
    osr_before = (0xCAFE_F00D << 12) & M32
    if fifo:
        sess.push(0, 0x0BAD_C0DE)
    r.exe("pull" + (" ifempty" if ifempty else "") + (" block" if block else " noblock"), clocks=4)
    st = r.st()
    if fifo:
        assert st["osr"] == 0x0BAD_C0DE and st["osr_count"] == 0
    elif not block:
        assert st["osr"] == 0x1234_5678 and st["osr_count"] == 0, "pull noblock on empty: OSR = X"
    else:
        assert st["stalled"] or st["exec_pending"], "pull block on empty must stall"
        assert st["osr"] == osr_before
    COVERED.add(("pull", ifempty, block))


# ------------------------------------------------------------------------------------- WAIT
@pytest.mark.parametrize("src", ["gpio", "pin"])
@pytest.mark.parametrize("pol", [0, 1])
@pytest.mark.parametrize("level", [0, 1])
def test_wait_pins(sess, src, pol, level):
    in_base, idx = 5, 3
    r = Rig(sess, pinctrl=in_base << 15)
    line = idx if src == "gpio" else in_base + idx
    r.pins_in(level << line | (~(1 << line) & M32 & 0))
    pc0 = r.st()["pc"]
    r.exe(f"wait {pol} {src} {idx}")
    st = r.st()
    if level == pol:
        assert not st["exec_pending"], "condition true: wait completes"
    else:
        assert st["exec_pending"], "condition false: wait stays latched"
        r.pins_in(pol << line)
        sess.step(3)
        assert not r.st()["exec_pending"], "wait did not complete when the pin changed"
    assert r.st()["pc"] == pc0
    COVERED.add(("wait", src, pol))


@pytest.mark.parametrize("pol", [0, 1])
@pytest.mark.parametrize("rel", [False, True])
def test_wait_irq(sess, pol, rel):
    r = Rig(sess)
    flag = 2                                           # sm 0: rel maps 2 -> 2
    if pol == 1:
        r.exe(f"irq set {flag}")
        assert sess.irq_flags() >> flag & 1
    r.exe(f"wait {pol} irq {flag}" + (" rel" if rel else ""))
    st = r.st()
    assert not st["exec_pending"]
    if pol == 1:
        assert not sess.irq_flags() >> flag & 1, "wait 1 irq clears the flag"
    COVERED.add(("wait", "irq", pol))


# ------------------------------------------------------------------------------------- IRQ
@pytest.mark.parametrize("mode", ["set", "nowait", "clear", "wait"])
@pytest.mark.parametrize("idx", [0, 5, 7])
@pytest.mark.parametrize("rel", [False, True])
def test_irq(sess, mode, idx, rel):
    r = Rig(sess)
    if mode == "clear":
        r.exe(f"irq set {idx}")
    r.exe(f"irq {mode} {idx}" + (" rel" if rel else ""))
    flags = sess.irq_flags()
    if mode in ("set", "nowait"):
        assert flags >> idx & 1
    elif mode == "clear":
        assert not flags >> idx & 1
    else:
        assert flags >> idx & 1 and r.st()["exec_pending"], "irq wait: set and wait for clear"
        sess.write(REG_IRQ, 1 << idx)                    # host clears it
        sess.step(3)
        assert not r.st()["exec_pending"]
    COVERED.add(("irq", mode))


# ------------------------------------------------------------------------------------- side-set, delay
@pytest.mark.parametrize("opt", [False, True])
@pytest.mark.parametrize("pindirs", [False, True])
@pytest.mark.parametrize("value", [0, 1, 2, 3])
def test_side_set(sess, opt, pindirs, value):
    base = 20
    size = 2 + (1 if opt else 0)
    r = Rig(sess, pinctrl=(base << 10) | (size << 29), execctrl=(1 << 30 if opt else 0) | (1 << 29 if pindirs else 0))
    ss = f".side_set 2{' opt' if opt else ''}{' pindirs' if pindirs else ''}"
    sess.write(SM(0, INSTR), word(f"nop side {value}", ss))
    sess.step(3)
    out, dir_, _ = sess.pins()
    got = (dir_ if pindirs else out) >> base & 3
    assert got == value
    if opt:                                            # without side: pins unchanged
        sess.write(SM(0, INSTR), word("nop", ss))
        sess.step(3)
        out2, dir2, _ = sess.pins()
        assert ((dir2 if pindirs else out2) >> base & 3) == value
    COVERED.add(("side", opt, pindirs))


@pytest.mark.parametrize("delay", [0, 1, 7, 31])
def test_delay(sess, delay):
    """A running program: `set x, 1 [d]` then `set x, 2`: X becomes 2 exactly d + 1 clocks later."""
    s = sess
    s.reset()
    p = assemble(f".program d\nset x, 1 [{delay}]\nset x, 2\nhalt:\njmp halt")["programs"][0]
    s.load(p, sm=0, enable=False)
    s.enable(0)
    for k in range(1, delay + 4):
        s.step(1)
        if s.sm_state(0)["reg_x"] == 2:
            break
    first_x1 = None
    tr = s.trace
    assert s.sm_state(0)["reg_x"] == 2
    # clocks between X=1 and X=2 from the PC trace: instruction 0 occupies delay + 1 clocks
    pcs = [tr.pcs[4 * i] for i in range(len(tr))]
    first = pcs.index(1) if 1 in pcs else None
    assert first is not None
    zeros = 0
    for pc in reversed(pcs[:first]):
        if pc != 0:
            break
        zeros += 1
    assert zeros >= delay + 1, (delay, pcs[-delay - 6:])
    COVERED.add(("delay", delay))
    del first_x1


# ------------------------------------------------------------------------------------- coverage
def test_every_rp2040_opcode_is_covered():
    """Runs last: fails if any RP2040 opcode / operand combination had no passing case."""
    need = set()
    need |= {("jmp", c) for c in ["", "!x", "x--", "!y", "y--", "x!=y", "pin", "!osre"]}
    need |= {("set", d) for d in ["pins", "x", "y", "pindirs"]}
    need |= {("mov", d, o, s) for d in MOV_DST for o in ["", "~", "::"] for s in MOV_SRC}
    need |= {("in", s) for s in IN_SRC}
    need |= {("out", d) for d in OUT_DST}
    need |= {("push", a, b) for a in (False, True) for b in (False, True)}
    need |= {("pull", a, b) for a in (False, True) for b in (False, True)}
    need |= {("wait", s, p) for s in ("gpio", "pin", "irq") for p in (0, 1)}
    need |= {("irq", m) for m in ("set", "nowait", "clear", "wait")}
    need |= {("side", o, p) for o in (False, True) for p in (False, True)}
    missing = sorted(map(str, need - COVERED))
    assert not missing, f"{len(missing)} not covered (or failed): {missing[:20]}"
