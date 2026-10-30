# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""PIO assembler (pioasm syntax) producing pioasm's JSON image format ([SPEC-DBG-16]).

    from pio2.asm import assemble
    images = assemble(source_text)          # {"programs": [ {name, instructions, ...}, ... ]}

Errors raise AsmError with the line number. Checked against every pioasm image in src/programs.
"""
# @impl [SPEC-DBG-16] docs/DEBUGGER_SPECIFICATION.md

import ast
import operator
import re

IGNORED = {".pio_version", ".clock_div", ".fifo", ".in", ".out", ".set", ".mov_status", ".lang_opt"}

JMP_COND = {"": 0, "!x": 1, "x--": 2, "!y": 3, "y--": 4, "x!=y": 5, "pin": 6, "!osre": 7}
WAIT_SRC = {"gpio": 0, "pin": 1, "irq": 2, "jmppin": 3}
IN_SRC = {"pins": 0, "x": 1, "y": 2, "null": 3, "isr": 6, "osr": 7}
OUT_DST = {"pins": 0, "x": 1, "y": 2, "null": 3, "pindirs": 4, "pc": 5, "isr": 6, "exec": 7}
MOV_DST = {"pins": 0, "x": 1, "y": 2, "pindirs": 3, "exec": 4, "pc": 5, "isr": 6, "osr": 7}
MOV_SRC = {"pins": 0, "x": 1, "y": 2, "null": 3, "status": 5, "isr": 6, "osr": 7}
SET_DST = {"pins": 0, "x": 1, "y": 2, "pindirs": 4}

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.FloorDiv: operator.floordiv, ast.Div: operator.floordiv, ast.LShift: operator.lshift,
        ast.RShift: operator.rshift, ast.BitOr: operator.or_, ast.BitAnd: operator.and_,
        ast.BitXor: operator.xor}


class AsmError(ValueError):
    def __init__(self, line, msg):
        super().__init__(f"line {line}: {msg}")
        self.line = line
        self.msg = msg


def _strip_comment(line):
    for mark in (";", "//"):
        i = line.find(mark)
        if i >= 0:
            line = line[:i]
    return line.strip()


class _Program:
    def __init__(self, name):
        self.name = name
        self.items = []              # (lineno, text) of instructions, or (lineno, ".word", expr)
        self.labels = {}
        self.defines = {}
        self.side_size = 0           # value bits
        self.side_opt = False
        self.side_pindirs = False
        self.wrap_target = None
        self.wrap = None
        self.origin = -1


def _eval(expr, names, lineno):
    expr = expr.strip()
    try:
        tree = ast.parse(expr, mode="eval").body
    except SyntaxError:
        raise AsmError(lineno, f"cannot read expression {expr!r}") from None

    def ev(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, int):
            return n.value
        if isinstance(n, ast.Name):
            if n.id in names:
                return names[n.id]
            raise AsmError(lineno, f"unknown name {n.id!r}")
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
            return -ev(n.operand)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Invert):
            return ~ev(n.operand)
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.left), ev(n.right))
        raise AsmError(lineno, f"unsupported expression {expr!r}")
    return ev(tree)


def _split_args(s):
    return [a.strip() for a in s.split(",")] if s.strip() else []


def assemble(source, ignored_out=None):
    """Assemble pioasm source; returns {"programs": [...]} like `pioasm -o json`.
    Directives in IGNORED are skipped and listed in ignored_out (a list) when given."""
    programs, prog, global_defines = [], None, {}
    in_block = False
    for lineno, raw in enumerate(source.splitlines(), 1):
        stripped = raw.strip()
        if in_block:
            if stripped.startswith("%}"):
                in_block = False
            continue
        if stripped.startswith("%") and "{" in stripped:
            in_block = True
            continue
        line = _strip_comment(raw)
        if not line:
            continue
        low = line.lower()
        if low.startswith("."):
            word, _, rest = line.partition(" ")
            word = word.lower()
            rest = rest.strip()
            if word == ".program":
                prog = _Program(rest.split()[0])
                prog.defines.update(global_defines)
                programs.append(prog)
            elif word == ".define":
                parts = rest.split()
                if parts and parts[0].lower() == "public":
                    parts = parts[1:]
                if len(parts) < 2:
                    raise AsmError(lineno, ".define needs a name and a value")
                target = prog.defines if prog else global_defines
                target[parts[0]] = _eval(" ".join(parts[1:]), {**global_defines, **target}, lineno)
            elif word in IGNORED:
                if ignored_out is not None:
                    ignored_out.append((lineno, word))
            elif prog is None:
                raise AsmError(lineno, f"{word} before .program")
            elif word == ".side_set":
                parts = rest.lower().split()
                if not parts:
                    raise AsmError(lineno, ".side_set needs a count")
                prog.side_size = _eval(parts[0], prog.defines, lineno)
                prog.side_opt = "opt" in parts[1:]
                prog.side_pindirs = "pindirs" in parts[1:]
                total = prog.side_size + (1 if prog.side_opt else 0)
                if prog.side_size < 0 or total > 5:
                    raise AsmError(lineno, "side-set uses more than 5 bits")
            elif word == ".wrap_target":
                prog.wrap_target = len(prog.items)
            elif word == ".wrap":
                if not prog.items:
                    raise AsmError(lineno, ".wrap before any instruction")
                prog.wrap = len(prog.items) - 1
            elif word == ".origin":
                prog.origin = _eval(rest, prog.defines, lineno)
            elif word == ".word":
                prog.items.append((lineno, ".word", rest))
            else:
                raise AsmError(lineno, f"unknown directive {word}")
            continue
        if prog is None:
            raise AsmError(lineno, "instruction before .program")
        m = re.match(r"^(?:public\s+)?([A-Za-z_][\w]*)\s*:\s*(.*)$", line, re.I)
        if m:
            name = m.group(1)
            if name in prog.labels:
                raise AsmError(lineno, f"label {name!r} defined twice")
            prog.labels[name] = len(prog.items)
            line = m.group(2).strip()
            if not line:
                continue
        prog.items.append((lineno, "ins", line))

    out = []
    for p in programs:
        if len(p.items) > 32:
            raise AsmError(p.items[32][0], f"program {p.name!r} has {len(p.items)} instructions (32 max)")
        names = {**p.defines, **p.labels}
        words = []
        for lineno, kind, text in p.items:
            if kind == ".word":
                words.append(_eval(text, names, lineno) & 0xFFFF)
            else:
                words.append(_encode(text, p, names, lineno))
        out.append({
            "name": p.name, "instructions": words,
            "wrap_target": p.wrap_target if p.wrap_target is not None else 0,
            "wrap": p.wrap if p.wrap is not None else max(len(words) - 1, 0),
            "origin": p.origin,
            "sideset": {"size": p.side_size + (1 if p.side_opt else 0), "optional": p.side_opt,
                        "pindirs": p.side_pindirs},
            "labels": dict(p.labels),
        })
    return {"programs": out}


def _encode(text, p, names, lineno):
    # [delay] and side N come last, in either order
    delay = 0
    side = None
    m = re.search(r"\[([^\]]*)\]\s*$", text)
    if m:
        delay = _eval(m.group(1), names, lineno)
        text = text[:m.start()].strip()
    m = re.search(r"\bside\s+(.+)$", text, re.I)
    if m:
        side = _eval(m.group(1), names, lineno)
        text = text[:m.start()].strip()
        if not re.search(r"\[([^\]]*)\]\s*$", text) is None:
            dm = re.search(r"\[([^\]]*)\]\s*$", text)
            delay = _eval(dm.group(1), names, lineno)
            text = text[:dm.start()].strip()

    op, _, rest = text.partition(" ")
    op = op.lower()
    rest = rest.strip()
    args = _split_args(rest)

    def val(e):
        return _eval(e, names, lineno)

    def pick(table, key, what):
        k = key.lower().replace(" ", "")
        if k not in table:
            raise AsmError(lineno, f"bad {what} {key!r}")
        return table[k]

    def count32(e):
        n = val(e)
        if not 1 <= n <= 32:
            raise AsmError(lineno, f"bit count {n} out of 1..32")
        return n & 31

    if op == "nop":
        word = 0xA042
    elif op == "jmp":
        m = re.match(r"^(!\s*x|x\s*--|!\s*y|y\s*--|x\s*!=\s*y|pin|!\s*osre)\s*,?\s*(.+)$", rest, re.I)
        cond, target = (m.group(1), m.group(2)) if m else ("", rest)
        if not target:
            raise AsmError(lineno, "jmp needs [condition,] target")
        addr = val(target)
        if not 0 <= addr < 32:
            raise AsmError(lineno, f"jmp target {addr} out of range")
        word = 0x0000 | pick(JMP_COND, cond, "jmp condition") << 5 | addr
    elif op == "wait":
        parts = rest.replace(",", " ").split()
        if len(parts) < 3:
            raise AsmError(lineno, "wait needs polarity, source, index")
        pol = val(parts[0])
        src = pick(WAIT_SRC, parts[1], "wait source")
        rel = len(parts) > 3 and parts[3].lower() == "rel"
        idx = val(parts[2]) if src != 3 else val(parts[2])
        if src == 2:
            idx = (idx & 7) | (0x10 if rel else 0)
        word = 0x2000 | (pol & 1) << 7 | src << 5 | (idx & 31)
    elif op == "in":
        if len(args) != 2:
            raise AsmError(lineno, "in needs source, bit count")
        word = 0x4000 | pick(IN_SRC, args[0], "in source") << 5 | count32(args[1])
    elif op == "out":
        if len(args) != 2:
            raise AsmError(lineno, "out needs destination, bit count")
        word = 0x6000 | pick(OUT_DST, args[0], "out destination") << 5 | count32(args[1])
    elif op in ("push", "pull"):
        opts = rest.lower().split()
        flag = ("iffull" if op == "push" else "ifempty") in opts
        block = "noblock" not in opts
        for o in opts:
            if o not in ("iffull", "ifempty", "block", "noblock"):
                raise AsmError(lineno, f"bad {op} option {o!r}")
        word = 0x8000 | (0x80 if op == "pull" else 0) | flag << 6 | block << 5
    elif op == "mov":
        if len(args) != 2:
            raise AsmError(lineno, "mov needs destination, source")
        dst = pick(MOV_DST, args[0], "mov destination")
        src = args[1].replace(" ", "")
        mop = 0
        if src.startswith("!") or src.startswith("~"):
            mop, src = 1, src[1:]
        elif src.startswith("::"):
            mop, src = 2, src[2:]
        word = 0xA000 | dst << 5 | mop << 3 | pick(MOV_SRC, src, "mov source")
    elif op == "irq":
        parts = rest.replace(",", " ").split()
        mode = "set"
        if parts and parts[0].lower() in ("set", "nowait", "wait", "clear"):
            mode = parts.pop(0).lower()
        if not parts:
            raise AsmError(lineno, "irq needs an index")
        rel = len(parts) > 1 and parts[1].lower() == "rel"
        idx = val(parts[0]) & 7
        word = (0xC000 | (mode == "clear") << 6 | (mode == "wait") << 5 | idx | (0x10 if rel else 0))
    elif op == "set":
        if len(args) != 2:
            raise AsmError(lineno, "set needs destination, value")
        v = val(args[1])
        if not 0 <= v <= 31:
            raise AsmError(lineno, f"set value {v} out of 0..31")
        word = 0xE000 | pick(SET_DST, args[0], "set destination") << 5 | v
    else:
        raise AsmError(lineno, f"unknown instruction {op!r}")

    total = p.side_size + (1 if p.side_opt else 0)
    delay_bits = 5 - total
    if not 0 <= delay <= (1 << delay_bits) - 1:
        raise AsmError(lineno, f"delay {delay} does not fit {delay_bits} bits")
    field = delay
    if side is not None:
        if p.side_size == 0:
            raise AsmError(lineno, "side used without .side_set")
        if not 0 <= side < (1 << p.side_size):
            raise AsmError(lineno, f"side value {side} does not fit {p.side_size} bits")
        field |= side << delay_bits
        if p.side_opt:
            field |= 1 << 4
    elif p.side_size and not p.side_opt:
        raise AsmError(lineno, "this program's side-set is not optional: every instruction needs `side`")
    return word | field << 8
