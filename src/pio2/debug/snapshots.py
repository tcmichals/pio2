# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""Session history for reverse step ([SPEC-DBG-06]): model snapshots every N cycles, bounded, plus
the log of what was done between them, so any earlier cycle can be rebuilt bit for bit.

A snapshot holds the Verilator model (`Session.snapshot`, a few kB) and a deep copy of every part's
state (parts are pure Python models, the stimulus included). The log holds the bench actions
(run to a cycle, one clock, one instruction, a register write, a button press, ...) with the cycle
each began at. Going back to cycle T restores the newest snapshot at or before T and replays the
log up to T; parts get no new input from outside during the replay, so the result is identical.

Memory: when more than `max_snapshots` are held, every second one of the older half is dropped
(the first one, right after the example was loaded, is kept), so old history gets sparser instead of
growing; replay from a sparser region just takes longer.
"""
# @impl [SPEC-DBG-06] docs/DEBUGGER_SPECIFICATION.md

import copy
from dataclasses import dataclass, field


@dataclass
class Action:
    cycle: int                         # session cycle when the action began
    kind: str                          # run_to, clock, instr, write, push, part
    args: tuple = ()


@dataclass
class Point:
    cycle: int
    session: object                    # pio2.debug.session.Snapshot
    parts: list                        # deep copy of the parts
    log_pos: int                       # index of the action in progress (or the next one)
    extra: dict = field(default_factory=dict)


class History:
    def __init__(self, every=1024, max_snapshots=512):
        self.every = every
        self.max = max_snapshots
        self.points = []
        self.log = []

    def clear(self):
        self.points, self.log = [], []

    @property
    def last_cycle(self):
        return self.points[-1].cycle if self.points else None

    def due(self, cycle):
        return not self.points or cycle - self.points[-1].cycle >= self.every

    def take(self, session, parts, log_pos, extra=None):
        snap = session.snapshot()
        if self.points and self.points[-1].cycle == snap.cycle:
            self.points.pop()
        self.points.append(Point(snap.cycle, snap, copy.deepcopy(parts), log_pos, dict(extra or {})))
        if len(self.points) > self.max:
            half = len(self.points) // 2
            keep = [self.points[0]] + [p for k, p in enumerate(self.points[1:half]) if k % 2] + self.points[half:]
            self.points = keep

    def before(self, cycle):
        """Newest point at or before `cycle`."""
        best = None
        for p in self.points:
            if p.cycle <= cycle:
                best = p
            else:
                break
        return best

    def drop_after(self, cycle):
        """A new action at `cycle` starts a new future: forget snapshots and actions after it."""
        self.points = [p for p in self.points if p.cycle <= cycle]
        self.log = [a for a in self.log if a.cycle < cycle]
        if self.log and self.log[-1].kind == "run_to" and self.log[-1].args[0] > cycle:
            self.log[-1] = Action(self.log[-1].cycle, "run_to", (cycle,))

    def record(self, action):
        last = self.log[-1] if self.log else None
        if (last is not None and action.kind == "run_to" and last.kind == "run_to"
                and last.args[0] == action.cycle):
            self.log[-1] = Action(last.cycle, "run_to", action.args)      # merge consecutive runs
        else:
            self.log.append(action)
        return len(self.log) - 1

    def nbytes(self):
        return sum(len(p.session.model) for p in self.points)
