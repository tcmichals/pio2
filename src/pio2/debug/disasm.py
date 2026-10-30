# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""PIO instruction disassembler (RP2040 / RP2350 encoding), for the debugger's source view."""
# @impl [SPEC-DBG-02] docs/DEBUGGER_SPECIFICATION.md

JMP_COND = ["", "!x, ", "x--, ", "!y, ", "y--, ", "x!=y, ", "pin, ", "!osre, "]
WAIT_SRC = ["gpio", "pin", "irq", "jmppin"]
IN_SRC = ["pins", "x", "y", "null", "?4", "?5", "isr", "osr"]
OUT_DST = ["pins", "x", "y", "null", "pindirs", "pc", "isr", "exec"]
MOV_DST = ["pins", "x", "y", "pindirs", "exec", "pc", "isr", "osr"]
MOV_OP = ["", "~", "::", "?3 "]
MOV_SRC = ["pins", "x", "y", "null", "?4", "status", "isr", "osr"]
SET_DST = ["pins", "x", "y", "?3", "pindirs", "?5", "?6", "?7"]


def _irq_index(i):
    return f"{i & 7} rel" if i & 0x10 else str(i & 7)


def disasm(word, sideset_size=0, sideset_opt=False, sideset_pindirs=False):
    """One instruction as pioasm-style text. sideset_size is pioasm's JSON `sideset.size`, which
    counts the enable bit when the side-set is optional ([SPEC-PIO-02] rule 5)."""
    op = (word >> 13) & 7
    arg1 = (word >> 5) & 7
    arg2 = word & 0x1F
    field = (word >> 8) & 0x1F
    delay_bits = 5 - sideset_size
    delay = field & ((1 << delay_bits) - 1)
    value_bits = sideset_size - (1 if sideset_opt and sideset_size else 0)
    side = None
    if sideset_size:
        bits = field >> delay_bits
        if not sideset_opt:
            side = bits
        elif bits >> value_bits:
            side = bits & ((1 << value_bits) - 1)

    if op == 0:
        text = f"jmp {JMP_COND[arg1]}{arg2}"
    elif op == 1:
        pol = (word >> 7) & 1
        src = (word >> 5) & 3
        text = f"wait {pol} {WAIT_SRC[src]} {_irq_index(arg2) if src == 2 else arg2}"
    elif op == 2:
        text = f"in {IN_SRC[arg1]}, {arg2 or 32}"
    elif op == 3:
        text = f"out {OUT_DST[arg1]}, {arg2 or 32}"
    elif op == 4:
        is_pull = (word >> 7) & 1
        flag = (word >> 6) & 1
        block = (word >> 5) & 1
        if is_pull:
            text = "pull" + (" ifempty" if flag else "") + (" block" if block else " noblock")
        else:
            text = "push" + (" iffull" if flag else "") + (" block" if block else " noblock")
    elif op == 5:
        dst, mop, src = arg1, (word >> 3) & 3, word & 7
        text = "nop" if (dst, mop, src) == (2, 0, 2) else f"mov {MOV_DST[dst]}, {MOV_OP[mop]}{MOV_SRC[src]}"
    elif op == 6:
        clr = (word >> 6) & 1
        wait = (word >> 5) & 1
        verb = "clear" if clr else ("wait" if wait else "set")
        text = f"irq {verb} {_irq_index(arg2)}"
    else:
        text = f"set {SET_DST[arg1]}, {arg2}"

    if side is not None:
        text += f" side {side}"
    if delay:
        text += f" [{delay}]"
    return text


def listing(program):
    """[(address, word, text)] for a program image from pio2.programs.load_program."""
    ss = program.get("sideset", {})
    return [(i, w, disasm(w, ss.get("size", 0), ss.get("optional", False), ss.get("pindirs", False)))
            for i, w in enumerate(program["instructions"])]
