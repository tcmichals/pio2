# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""The one `Part` interface every virtual part and stimulus source implements ([SPEC-DBG-15]).

A part is a simulation model, independent of the GUI and of the run loop:

* `update(session)` is called by the bench (`pio2.debug.bench.Bench`) after each batch of clocks.
  The part reads the clocks added to the session trace since its last call (`consume`) and applies
  the drive changes it scheduled for this cycle or earlier (`Session.drive`).
* `wake(cycle)` says when the bench must stop next for this part: the cycle of its next scheduled
  drive change, the latest cycle by which it must have looked at the lines (its reaction horizon),
  or None. A part that must answer within one clock (an SPI slave) sets `reactive = True` and is
  updated every clock. So the timing a part produces does not depend on how the run is cut into
  batches, which reverse step ([SPEC-DBG-06]) relies on.
* `params` holds the constructor arguments; a diagram file ([SPEC-DBG-15]) stores them.

Trace positions: element i of `session.trace` is the line state after clock `trace.start + i + 1`,
i.e. what the pins read when `session.cycle == trace.start + i + 1`. Parts keep absolute cycles
(`trace.start + i`), never raw indices, because the trace drops its oldest clocks.
"""
# @impl [SPEC-DBG-15] docs/DEBUGGER_SPECIFICATION.md

import bisect


class Part:
    kind = "part"
    reactive = False                       # True: needs update() after every clock

    def __init__(self, name, **pins):
        self.name = name
        self.pins = pins                   # role -> GPIO line
        self.params = {}                   # constructor arguments other than name and pins
        self.seen = None                   # absolute trace position processed so far
        self.schedule = []                 # sorted [(cycle, seq, pin, level or None)]
        self._seq = 0

    # ------------------------------------------------------------ run-loop interface
    def attach(self, session):
        tr = session.trace
        self.seen = tr.start + len(tr)

    def wake(self, cycle):
        """Next cycle the bench must stop at for this part, or None."""
        return self.schedule[0][0] if self.schedule else None

    def update(self, session):
        """Process the clocks added since the last call, then apply due drive changes."""
        tr = session.trace
        end = tr.start + len(tr)
        start = tr.start if self.seen is None else max(self.seen, tr.start)
        if start > end:                    # the session went back (reverse step) or was reset
            start = end
        if end > start:
            self.consume(session, tr, start - tr.start, end - tr.start)
        self.seen = end
        self.apply(session)

    def consume(self, session, trace, start, end):
        """Trace elements [start, end) are new (indices into the trace arrays)."""

    # ------------------------------------------------------------ driving lines
    def drive_at(self, cycle, pin, level):
        """Drive `pin` to `level` (0 / 1, None releases it) from session cycle `cycle` on."""
        self._seq += 1
        bisect.insort(self.schedule, (cycle, self._seq, pin, level))

    def apply(self, session):
        while self.schedule and self.schedule[0][0] <= session.cycle:
            _, _, pin, level = self.schedule.pop(0)
            session.drive(pin, level)

    def drives(self):
        """Lines this part may drive (for the diagram and for releasing them on detach)."""
        return []

    # ------------------------------------------------------------ diagram
    @property
    def wired(self):
        """True for parts on GPIO lines (they go in the diagram); host scripts have no pins."""
        return bool(self.pins)

    def to_dict(self):
        return {"type": self.kind, "name": self.name, "pins": dict(self.pins), "attrs": dict(self.params)}


def line(trace, i, pin):
    """Level of `pin` at trace element i (what the pad reads)."""
    return (trace.inp[i] >> pin) & 1
