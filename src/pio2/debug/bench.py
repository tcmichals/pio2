# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""Bench: a Session with an example's parts and stimulus, breakpoints, and a history to step back
through. The GUI, the CLI and the tests drive the simulation through this one run loop.

    b = Bench(example="Blink")
    b.watches.append(Watch.pc(0, 4))
    stop = b.run(100_000)          # stops at the cycle SM0 reaches address 4
    b.back(10)                     # ten clocks earlier, bit-identical ([SPEC-DBG-06])

Run loop: the bench steps the model in batches as long as no part needs to act: a batch ends at the
earliest `Part.wake` cycle, at the next snapshot, at the run's end, or after one clock when a part is
reactive or a watch needs the per-clock state. Parts act at exact cycles, so the result does not
depend on how a run is cut up (tested), which is what makes replay exact.

Breakpoints and watchpoints ([SPEC-DBG-04]) stop at the cycle their condition becomes true (false on
the clock before, true after it). PC, pin edge and stall conditions are found in the per-clock trace
after each batch (the bench then goes back to that cycle by replay); IRQ flag, FIFO level and X / Y /
ISR / OSR conditions need the model state, so with one of those active the bench steps one clock at a
time. Clocks inside a register access (a few clocks each, FIFO feeders) are checked at its end.
"""
# @impl [SPEC-DBG-04] [SPEC-DBG-06] [SPEC-DBG-09] docs/DEBUGGER_SPECIFICATION.md

import copy
from dataclasses import dataclass

from pio2.debug.disasm import disasm
from pio2.debug.session import Session, SimError
from pio2.debug.snapshots import Action, History


# ---------------------------------------------------------------------------- watches
@dataclass
class Watch:
    """kind: pc (sm, arg=address), pin (arg=line, edge rise / fall / any), stall (sm, what=reason
    text or ""), irq (arg=flag, edge set / clear), fifo (sm, what tx / rx, edge >= / <= / ==,
    arg=level), reg (sm, what x / y / isr / osr, arg=value)."""
    kind: str
    sm: int = 0
    arg: int = 0
    edge: str = ""
    what: str = ""
    enabled: bool = True
    hits: int = 0

    FAST = ("pc", "pin", "stall")

    @staticmethod
    def pc(sm, address):
        return Watch("pc", sm, address)

    @staticmethod
    def pin(pin, edge="rise"):
        return Watch("pin", 0, pin, edge)

    @staticmethod
    def stall(sm, reason=""):
        return Watch("stall", sm, 0, "", reason)

    @staticmethod
    def irq(flag, edge="set"):
        return Watch("irq", 0, flag, edge)

    @staticmethod
    def fifo(sm, which, op, level):
        return Watch("fifo", sm, level, op, which)

    @staticmethod
    def reg(sm, name, value):
        return Watch("reg", sm, value & 0xFFFF_FFFF, "", name)

    @property
    def fast(self):
        return self.kind in self.FAST

    def describe(self):
        k = self.kind
        if k == "pc":
            return f"SM{self.sm} PC == {self.arg}"
        if k == "pin":
            return f"GP{self.arg} {self.edge or 'rise'} edge"
        if k == "stall":
            return f"SM{self.sm} stalls" + (f" ({self.what})" if self.what else "")
        if k == "irq":
            return f"IRQ flag {self.arg} {self.edge or 'set'}"
        if k == "fifo":
            return f"SM{self.sm} {self.what.upper()} FIFO level {self.edge} {self.arg}"
        if k == "reg":
            return f"SM{self.sm} {self.what.upper()} == 0x{self.arg:x}"
        return k

    # slow conditions: evaluated on the model state now
    def state_true(self, s):
        if self.kind == "irq":
            on = bool(s.irq_flags() >> self.arg & 1)
            return on if self.edge in ("", "set") else not on
        st = s.sm_state(self.sm)
        if self.kind == "fifo":
            lvl = st["tx_level" if self.what == "tx" else "rx_level"]
            return {">=": lvl >= self.arg, "<=": lvl <= self.arg, "==": lvl == self.arg}[self.edge or "=="]
        if self.kind == "reg":
            key = {"x": "reg_x", "y": "reg_y", "isr": "isr", "osr": "osr"}[self.what]
            return st[key] == self.arg
        raise ValueError(self.kind)


def stall_reason_of(word):
    """Why an instruction stalls, from its opcode (pull / out: TX FIFO, push / in: RX FIFO, ...)."""
    op = word >> 13 & 7
    if op == 4:
        return "TX FIFO empty" if word >> 7 & 1 else "RX FIFO full"
    if op == 3:
        return "TX FIFO empty"            # autopull
    if op == 2:
        return "RX FIFO full"             # autopush
    if op == 1:
        return "wait"
    if op == 6:
        return "irq wait"
    return "stalled"


@dataclass
class Stop:
    cycle: int
    watch: Watch
    text: str


# ---------------------------------------------------------------------------- bench
class Bench:
    def __init__(self, session=None, example=None, snapshot_every=1024, max_snapshots=512):
        self.s = session or Session()
        self.own = session is None
        self.history = History(snapshot_every, max_snapshots)
        self.watches = []
        self.ex = None
        self.name = None
        self.replaying = False
        self.last_stop = None
        if example is not None:
            self.load(example)

    def close(self):
        if self.own:
            self.s.close()

    # ------------------------------------------------------------ loading
    def load(self, example):
        from pio2.debug.examples import EXAMPLES
        fn = EXAMPLES[example] if isinstance(example, str) else example
        s = self.s
        s.reset()
        s.release_all()
        s.set_pull(0xFFFF_FFFF)
        s.set_oe_invert(0)
        self.ex = fn(s)
        self.name = example if isinstance(example, str) else getattr(fn, "__name__", "example")
        for p in self.parts:
            p.attach(s)
        self.history.clear()
        self.history.take(s, self.parts, 0)
        self.last_stop = None
        return self.ex

    @property
    def parts(self):
        return self.ex.parts if self.ex else []

    def part(self, name):
        return next(p for p in self.parts if p.name == name)

    # ------------------------------------------------------------ user actions (logged)
    def _new_action(self, kind, args=()):
        if not self.replaying:
            self.history.drop_after(self.s.cycle)
        return self.history.record(Action(self.s.cycle, kind, tuple(args)))

    def run(self, n):
        """Run n clocks (or until a watch fires: returns a Stop, else None)."""
        return self.run_to(self.s.cycle + n)

    def run_to(self, end):
        if end <= self.s.cycle:
            return None
        pos = self._new_action("run_to", (end,))
        start = self.s.cycle
        hit = self._run_to(end, pos)
        if hit is None:
            return None
        # found at hit.cycle: the run ends there; rebuild that cycle exactly
        self.history.log[pos] = Action(start, "run_to", (hit.cycle,))
        self.history.points = [p for p in self.history.points if p.cycle <= hit.cycle]
        if self.s.cycle != hit.cycle:
            self.back_to(hit.cycle)
        self.last_stop = hit
        return hit

    def step_clock(self):
        self._new_action("clock")
        self._exec(self.history.log[-1])

    def step_instr(self, sm, limit=10_000):
        """Run until SM `sm` retires one instruction ([SPEC-DBG-03]); parts act every clock.
        Returns the clocks run (== limit: nothing retired)."""
        if not (self.s._get("sm_en") >> sm) & 1:
            raise SimError(f"SM{sm} is disabled")
        self._new_action("instr", (sm, limit))
        return self._exec(self.history.log[-1])

    def write(self, off, value):
        self._new_action("write", (off, value))
        self._exec(self.history.log[-1])

    def push(self, sm, word):
        self.write(0x110 + 0x20 * sm, word)

    def part_call(self, name, method, *args):
        """Call a part's method that changes what it does (press a button, send UART bytes, set a
        throttle) as a logged action, so reverse step replays it."""
        self._new_action("part", (name, method, args))
        return self._exec(self.history.log[-1])

    def _exec(self, a):
        s = self.s
        if a.kind == "clock":
            s.step(1)
            self._update_parts()
        elif a.kind == "instr":
            return s.step_instr(a.args[0], limit=a.args[1], on_clock=self._update_parts)
        elif a.kind == "write":
            s.write(*a.args)
            self._update_parts()
        elif a.kind == "part":
            name, method, args = a.args
            p = self.part(name)
            r = getattr(p, method)(s, *args)
            self._update_parts()
            return r
        else:
            raise ValueError(a.kind)

    def _update_parts(self):
        for p in self.parts:
            p.update(self.s)

    # ------------------------------------------------------------ run loop
    def _run_to(self, end, pos, limit=None):
        s = self.s
        watch_on = not self.replaying and any(w.enabled for w in self.watches)
        slow = [w for w in self.watches if w.enabled and not w.fast] if watch_on else []
        fast = [w for w in self.watches if w.enabled and w.fast] if watch_on else []
        slow_prev = {id(w): w.state_true(s) for w in slow}
        while s.cycle < end:
            c = s.cycle
            nxt = end
            for p in self.parts:
                w = c + 1 if p.reactive else p.wake(c)
                if w is not None:
                    nxt = min(nxt, max(w, c + 1))
            nxt = min(nxt, c + max(1, s.trace.depth // 4))
            if not self.replaying:
                nxt = min(nxt, max(c + 1, self.history.points[-1].cycle + self.history.every))
            if slow:
                nxt = c + 1
            pos0 = s.trace.start + len(s.trace)
            s.step(nxt - c)
            if limit is not None and limit - s.cycle < 1024:
                pre = (s.snapshot(), copy.deepcopy(self.parts))
                self._update_parts()
                if s.cycle > limit:        # a register access went past the target: stop before it
                    s.restore(pre[0])
                    self.parts[:] = pre[1]
                    return None
            else:
                self._update_parts()
            if fast:
                hit = self._scan(fast, pos0)
                if hit:
                    return hit
            for w in slow:
                now = w.state_true(s)
                if now and not slow_prev[id(w)]:
                    w.hits += 1
                    return Stop(s.cycle, w, w.describe())
                slow_prev[id(w)] = now
            if not self.replaying and self.history.due(s.cycle):
                self.history.take(s, self.parts, pos)
        return None

    def _scan(self, watches, pos0):
        """First trace position at or after `pos0` where a fast watch becomes true."""
        tr = self.s.trace
        a = max(pos0 - tr.start, 1)
        b = len(tr)
        best = None
        for w in watches:
            i = self._find(w, tr, a, b if best is None else min(b, best[0] + 1))
            if i is not None and (best is None or i < best[0]):
                best = (i, w)
        if best is None:
            return None
        i, w = best
        w.hits += 1
        return Stop(tr.start + i + 1, w, w.describe())

    def _find(self, w, tr, a, b):
        if w.kind == "pc":
            col = bytes(tr.pcs[4 * (a - 1) + w.sm:4 * b:4])      # element a-1 .. b-1
            k = 1
            while True:
                k = col.find(bytes([w.arg]), k)
                if k < 0:
                    return None
                if col[k - 1] != w.arg:
                    return a - 1 + k
                k += 1
        if w.kind == "stall":
            col = bytes(tr.stalls[4 * (a - 1) + w.sm:4 * b:4])
            k = 1
            while True:
                k = col.find(b"\x01", k)
                if k < 0:
                    return None
                if col[k - 1] == 0:
                    i = a - 1 + k
                    if not w.what:
                        return i
                    word = self.s._get("iram", tr.pcs[4 * i + w.sm])
                    if w.what.lower() in stall_reason_of(word).lower():
                        return i
                k += 1
        if w.kind == "pin":
            m = 1 << w.arg
            prev = tr.inp[a - 1] & m
            for i in range(a, b):
                v = tr.inp[i] & m
                if v != prev:
                    if w.edge == "any" or (v and w.edge in ("", "rise")) or (not v and w.edge == "fall"):
                        return i
                prev = v
            return None
        raise ValueError(w.kind)

    # ------------------------------------------------------------ reverse ([SPEC-DBG-06])
    def back_to(self, target):
        """Rebuild the state at cycle `target` (or the nearest cycle before it that a register access
        does not straddle). Returns the cycle reached."""
        h, s = self.history, self.s
        target = max(target, h.points[0].cycle)
        pt = h.before(target)
        s.restore(pt.session)
        self.parts[:] = copy.deepcopy(pt.parts)
        self.replaying = True
        try:
            i = pt.log_pos
            while i < len(h.log) and s.cycle < target:
                a = h.log[i]
                if a.kind == "run_to":
                    if a.args[0] > s.cycle:
                        self._run_to(min(a.args[0], target), i, limit=target)
                        if a.args[0] >= target:
                            break
                else:
                    if a.cycle >= target:
                        break
                    pre = (s.snapshot(), copy.deepcopy(self.parts))
                    self._exec(a)
                    if s.cycle > target:
                        s.restore(pre[0])
                        self.parts[:] = pre[1]
                        break
                i += 1
        finally:
            self.replaying = False
        return s.cycle

    def back(self, clocks=1):
        return self.back_to(self.s.cycle - clocks)

    def back_instr(self, sm):
        """Back to the clock at which SM `sm`'s previous instruction began (from the per-clock PC
        trace: the last PC change before the current one)."""
        tr = self.s.trace
        n = len(tr)
        col = bytes(tr.pcs[sm::4])
        i = n - 1
        while i > 0 and col[i - 1] == col[n - 1]:
            i -= 1
        j = i - 1
        while j > 0 and col[j - 1] == col[i - 1]:
            j -= 1
        if j <= 0:
            return self.back(1)
        return self.back_to(tr.start + j + 1)
