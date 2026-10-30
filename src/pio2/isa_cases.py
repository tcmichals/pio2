# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""Every RP2040 PIO opcode and operand: the case table, shared by two test benches ([SPEC-PIO-02]).

Each case sets up SM0 of a fresh tile (X, Y, ISR, OSR, pins, FIFOs, config), executes ONE instruction
through SMx_INSTR (assembled from text by pio2.asm, so the assembler is checked too), and compares the
resulting state with the RP2040 datasheet rule (section 3.4) computed here in Python.

The same cases run on
  * the Verilator debugger model: tests/test_isa_every_opcode.py (pytest, pio2.debug.Session)
  * Icarus and Verilator through cocotb: tests/cocotb/test_isa_every_opcode_cocotb.py (cocotbext-axi
    on the AXI4-Lite port; the blocking case code runs in a cocotb bridge thread)

A case is plain blocking code against a small `hw` interface; each bench supplies one:
    hw.reset()                    reset the tile (and the pins driven from outside to 0)
    hw.write(offset, value)       AXI4-Lite register write (OKAY expected)
    hw.step(n)                    run n system clocks
    hw.sm_state(sm) -> dict       pc, reg_x, reg_y, isr, osr, isr_count, osr_count, stalled,
                                  exec_pending, rx_level, rx_fifo (RTL state by name)
    hw.pins() -> (out, dir, in)   the 32 GPIO lines now
    hw.gpio_in(value)             drive all 32 lines from outside (pins the tile drives read back its
                                  own output, as on the RP2040 pads)
    hw.irq_flags() -> int         the tile's 8 IRQ flags

Each case returns its coverage key; `required_coverage()` is the set every RP2040 opcode / operand
combination needs, and both benches fail if a key has no passing case.
"""

import random
import zlib

from pio2.asm import assemble

M32 = 0xFFFF_FFFF

REG_CTRL, REG_IRQ = 0x00, 0x04
IRAM = 0x10
CLKDIV, EXECCTRL, SHIFTCTRL, PINCTRL, TXF, INSTR = 0x00, 0x04, 0x08, 0x0C, 0x10, 0x18


def SM(n, off):
    return 0x100 + 0x20 * n + off


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

    def __init__(self, hw, shiftctrl=0, pinctrl=0, execctrl=0):
        self.hw = hw
        hw.reset()
        hw.write(SM(0, CLKDIV), 1 << 16)
        hw.write(SM(0, EXECCTRL), execctrl)
        hw.write(SM(0, SHIFTCTRL), shiftctrl)
        hw.write(SM(0, PINCTRL), pinctrl)
        self.shiftctrl, self.pinctrl, self.execctrl = shiftctrl, pinctrl, execctrl

    def exe(self, text, clocks=3):
        self.hw.write(SM(0, INSTR), word(text))
        self.hw.step(clocks)

    def st(self):
        return self.hw.sm_state(0)

    def push(self, v):
        self.hw.write(SM(0, TXF), v)

    def load(self, text):
        """`text` as a program at 0, then `halt: jmp halt`; no wrap before halt; PC at 0."""
        p = assemble(f".program r\n{text}\nhalt:\njmp halt")["programs"][0]
        for i, w in enumerate(p["instructions"]):
            self.hw.write(IRAM + 4 * i, w)
        self.hw.write(SM(0, EXECCTRL), self.execctrl | (31 << 12))
        self.exe("jmp 0", clocks=0)

    def run(self, text, clocks=12):
        """Load `text` and run it enabled for `clocks` (EXEC needs a running SM), then disable."""
        self.load(text)
        self.hw.write(REG_CTRL, 1)                                     # enable SM0
        self.hw.step(clocks)
        self.hw.write(REG_CTRL, 0)

    def set32(self, reg, v):
        """reg in x, y, isr, osr: through the TX FIFO, pull, mov."""
        self.push(v)
        self.exe("pull block")
        if reg != "osr":
            self.exe(f"mov {reg}, osr")

    def pins_in(self, value):
        self.hw.gpio_in(value)


VALS = [0x0000_0000, 0x0000_0001, 0x8000_0000, 0xDEAD_BEEF, 0xFFFF_FFFF]

JMP_CONDS = ["", "!x", "x--", "!y", "y--", "x!=y", "pin", "!osre"]
SET_DST = ["pins", "x", "y", "pindirs"]
MOV_DST = ["pins", "x", "y", "exec", "pc", "isr", "osr"]
MOV_SRC = ["pins", "x", "y", "null", "status", "isr", "osr"]
MOV_OPS = ["", "~", "::"]
IN_SRC = ["pins", "x", "y", "null", "isr", "osr"]
OUT_DST = ["pins", "x", "y", "null", "pindirs", "pc", "isr", "exec"]


# ------------------------------------------------------------------------------------- JMP
def case_jmp(hw, cond, x, y):
    r = Rig(hw, execctrl=9 << 24)                    # JMP_PIN = 9
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
    return ("jmp", cond)


# ------------------------------------------------------------------------------------- SET
def case_set(hw, dest, value):
    base, count = 6, 5
    r = Rig(hw, pinctrl=(base << 5) | (count << 26))
    r.exe(f"set {dest}, {value}")
    st = r.st()
    out, dir_, _ = hw.pins()
    window = ((1 << count) - 1) << base
    if dest == "x":
        assert st["reg_x"] == value
    elif dest == "y":
        assert st["reg_y"] == value
    elif dest == "pins":
        assert out & window == (value << base) & window
    else:
        assert dir_ & window == (value << base) & window
    return ("set", dest)


# ------------------------------------------------------------------------------------- MOV
def case_mov(hw, dst, op, src):
    rng = random.Random(zlib.crc32(f"{dst}|{op}|{src}".encode()))   # same values every run
    in_base, out_base = 3, 0
    status_n = 2
    r = Rig(hw, pinctrl=(in_base << 15) | (out_base) | (32 << 20) if dst == "pins" else (in_base << 15),
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
        r.push(i)
    r.pins_in(gpio)
    pc0 = r.st()["pc"]
    srcval = {"pins": rotr(gpio, in_base), "x": x, "y": y, "null": 0,
              "status": M32 if tx_fill < status_n else 0, "isr": isr, "osr": osr}[src]
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
        out, _, _ = hw.pins()
        assert out == v                                  # OUT window: base 0, 32 pins
    if dst not in ("pc", "exec"):
        assert st["pc"] == pc0
    return ("mov", dst, op, src)


# ------------------------------------------------------------------------------------- IN
def case_in(hw, src, count, right):
    in_base = 7
    r = Rig(hw, shiftctrl=(1 << 18) if right else 0, pinctrl=in_base << 15)
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
    return ("in", src)


# ------------------------------------------------------------------------------------- OUT
def case_out(hw, dst, count, right):
    base, ocount = 4, 12
    r = Rig(hw, shiftctrl=(1 << 19) if right else 0, pinctrl=base | (ocount << 20))
    rng = random.Random(count * 13 + right)
    osr = rng.getrandbits(32)
    if dst == "exec":                                    # the shifted-out bits are an instruction
        assert count >= 16, "out exec needs at least 16 bits"
        ins = word("set y, 21")
        if right:
            osr = ins | (rng.getrandbits(16) << 16 if count == 16 else 0)
        else:
            osr = (ins << 16) | rng.getrandbits(16) if count == 16 else ins
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
    out, dir_, _ = hw.pins()
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
    return ("out", dst)


# ------------------------------------------------------------------------------------- PUSH / PULL
def case_push(hw, iffull, block, count):
    r = Rig(hw, shiftctrl=8 << 20)                     # push threshold 8
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
    return ("push", iffull, block)


def case_pull(hw, ifempty, block, fifo):
    r = Rig(hw, shiftctrl=8 << 25)                     # pull threshold 8
    r.set32("x", 0x1234_5678)
    r.set32("osr", 0xCAFE_F00D)
    r.exe("out null, 12")                               # osr count 12 >= threshold
    osr_before = (0xCAFE_F00D << 12) & M32
    if fifo:
        r.push(0x0BAD_C0DE)
    r.exe("pull" + (" ifempty" if ifempty else "") + (" block" if block else " noblock"), clocks=4)
    st = r.st()
    if fifo:
        assert st["osr"] == 0x0BAD_C0DE and st["osr_count"] == 0
    elif not block:
        assert st["osr"] == 0x1234_5678 and st["osr_count"] == 0, "pull noblock on empty: OSR = X"
    else:
        assert st["stalled"] or st["exec_pending"], "pull block on empty must stall"
        assert st["osr"] == osr_before
    return ("pull", ifempty, block)


# ------------------------------------------------------------------------------------- WAIT
def case_wait_pins(hw, src, pol, level):
    in_base, idx = 5, 3
    r = Rig(hw, pinctrl=in_base << 15)
    line = idx if src == "gpio" else in_base + idx
    r.pins_in(level << line)
    pc0 = r.st()["pc"]
    r.exe(f"wait {pol} {src} {idx}")
    st = r.st()
    if level == pol:
        assert not st["exec_pending"], "condition true: wait completes"
    else:
        assert st["exec_pending"], "condition false: wait stays latched"
        r.pins_in(pol << line)
        hw.step(3)
        assert not r.st()["exec_pending"], "wait did not complete when the pin changed"
    assert r.st()["pc"] == pc0
    return ("wait", src, pol)


def case_wait_irq(hw, pol, rel):
    r = Rig(hw)
    flag = 2                                           # sm 0: rel maps 2 -> 2
    if pol == 1:
        r.exe(f"irq set {flag}")
        assert hw.irq_flags() >> flag & 1
    r.exe(f"wait {pol} irq {flag}" + (" rel" if rel else ""))
    st = r.st()
    assert not st["exec_pending"]
    if pol == 1:
        assert not hw.irq_flags() >> flag & 1, "wait 1 irq clears the flag"
    return ("wait", "irq", pol)


# ------------------------------------------------------------------------------------- IRQ
def case_irq(hw, mode, idx, rel):
    r = Rig(hw)
    if mode == "clear":
        r.exe(f"irq set {idx}")
    r.exe(f"irq {mode} {idx}" + (" rel" if rel else ""))
    flags = hw.irq_flags()
    if mode in ("set", "nowait"):
        assert flags >> idx & 1
    elif mode == "clear":
        assert not flags >> idx & 1
    else:
        assert flags >> idx & 1 and r.st()["exec_pending"], "irq wait: set and wait for clear"
        hw.write(REG_IRQ, 1 << idx)                      # host clears it
        hw.step(3)
        assert not r.st()["exec_pending"]
    return ("irq", mode)


# ------------------------------------------------------------------------------------- side-set, delay
def case_side_set(hw, opt, pindirs, value):
    base = 20
    size = 2 + (1 if opt else 0)
    Rig(hw, pinctrl=(base << 10) | (size << 29), execctrl=(1 << 30 if opt else 0) | (1 << 29 if pindirs else 0))
    ss = f".side_set 2{' opt' if opt else ''}{' pindirs' if pindirs else ''}"
    hw.write(SM(0, INSTR), word(f"nop side {value}", ss))
    hw.step(3)
    out, dir_, _ = hw.pins()
    got = (dir_ if pindirs else out) >> base & 3
    assert got == value
    if opt:                                            # without side: pins unchanged
        hw.write(SM(0, INSTR), word("nop", ss))
        hw.step(3)
        out2, dir2, _ = hw.pins()
        assert ((dir2 if pindirs else out2) >> base & 3) == value
    return ("side", opt, pindirs)


def case_delay(hw, delay):
    """A running program `wait 1 gpio 0` / `set x, 1 [d]` / `set x, 2`: X reads 1 for exactly d + 1
    clocks. The wait holds the program until the bench releases it, so the register writes that enable
    the SM take no part in the count."""
    r = Rig(hw)
    r.load(f"wait 1 gpio 0\nset x, 1 [{delay}]\nset x, 2")
    hw.write(REG_CTRL, 1)                                # enable SM0: it waits on GPIO 0
    hw.step(4)
    assert r.st()["reg_x"] == 0
    r.pins_in(1)
    xs = []
    for _ in range(delay + 8):
        hw.step(1)
        xs.append(r.st()["reg_x"])
    assert xs[-1] == 2, xs
    assert xs.count(1) == delay + 1, (delay, xs)
    return ("delay", delay)


# ------------------------------------------------------------------------------------- the table
def _cases():
    c = []
    for cond in JMP_CONDS:
        for x, y in [(0, 0), (5, 5), (0, 7), (3, 0), (M32, 1)]:
            c.append((f"jmp[{cond or 'always'}-{x:x}-{y:x}]", case_jmp, (cond, x, y)))
    for dest in SET_DST:
        for value in [0, 1, 0x15, 31]:
            c.append((f"set[{dest}-{value}]", case_set, (dest, value)))
    for dst in MOV_DST:
        for op in MOV_OPS:
            for src in MOV_SRC:
                c.append((f"mov[{dst}-{op or 'none'}-{src}]", case_mov, (dst, op, src)))
    for src in IN_SRC:
        for count in [1, 5, 31, 32]:
            for right in [False, True]:
                c.append((f"in[{src}-{count}-{'right' if right else 'left'}]", case_in, (src, count, right)))
    for dst in OUT_DST:
        for count in [1, 5, 16, 32]:
            if dst == "exec" and count < 16:           # an instruction is 16 bits
                continue
            for right in [False, True]:
                c.append((f"out[{dst}-{count}-{'right' if right else 'left'}]", case_out, (dst, count, right)))
    for iffull in [False, True]:
        for block in [False, True]:
            for count in [4, 12]:
                c.append((f"push[{iffull:d}-{block:d}-{count}]", case_push, (iffull, block, count)))
    for ifempty in [False, True]:
        for block in [False, True]:
            for fifo in [0, 1]:
                c.append((f"pull[{ifempty:d}-{block:d}-{fifo}]", case_pull, (ifempty, block, fifo)))
    for src in ["gpio", "pin"]:
        for pol in [0, 1]:
            for level in [0, 1]:
                c.append((f"wait[{src}-{pol}-{level}]", case_wait_pins, (src, pol, level)))
    for pol in [0, 1]:
        for rel in [False, True]:
            c.append((f"wait[irq-{pol}-{rel:d}]", case_wait_irq, (pol, rel)))
    for mode in ["set", "nowait", "clear", "wait"]:
        for idx in [0, 5, 7]:
            for rel in [False, True]:
                c.append((f"irq[{mode}-{idx}-{rel:d}]", case_irq, (mode, idx, rel)))
    for opt in [False, True]:
        for pindirs in [False, True]:
            for value in [0, 1, 2, 3]:
                c.append((f"side[{opt:d}-{pindirs:d}-{value}]", case_side_set, (opt, pindirs, value)))
    for delay in [0, 1, 7, 31]:
        c.append((f"delay[{delay}]", case_delay, (delay,)))
    return c


CASES = _cases()          # [(id, function, args)]: call function(hw, *args) -> coverage key


def required_coverage():
    """Every RP2040 opcode / operand combination that must have a passing case."""
    need = set()
    need |= {("jmp", c) for c in JMP_CONDS}
    need |= {("set", d) for d in SET_DST}
    need |= {("mov", d, o, s) for d in MOV_DST for o in MOV_OPS for s in MOV_SRC}
    need |= {("in", s) for s in IN_SRC}
    need |= {("out", d) for d in OUT_DST}
    need |= {("push", a, b) for a in (False, True) for b in (False, True)}
    need |= {("pull", a, b) for a in (False, True) for b in (False, True)}
    need |= {("wait", s, p) for s in ("gpio", "pin", "irq") for p in (0, 1)}
    need |= {("irq", m) for m in ("set", "nowait", "clear", "wait")}
    need |= {("side", o, p) for o in (False, True) for p in (False, True)}
    return need


def missing_coverage(covered):
    """Sorted list (as text) of required keys not in `covered`."""
    return sorted(map(str, required_coverage() - set(covered)))
