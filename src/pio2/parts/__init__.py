# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""Virtual parts wired to the tile's pins ([SPEC-DBG-15]).

A part is a simulation model, independent of the GUI: after each batch of clocks it reads the new
part of the session trace (`update`), and it may drive input lines (`Session.drive`). The GUI only
draws `part.state`.
"""


# @impl [SPEC-DBG-15] docs/DEBUGGER_SPECIFICATION.md
class Part:
    kind = "part"

    def __init__(self, name, **pins):
        self.name = name
        self.pins = pins                   # role -> GPIO line
        self.seen = None                   # trace index processed so far

    def attach(self, session):
        self.seen = len(session.trace)

    def update(self, session):
        """Process trace entries added since the last call."""
        start = self.seen if self.seen is not None else 0
        tr = session.trace
        if start > len(tr):                # trace was trimmed or reset
            start = 0
        self.consume(session, tr, start, len(tr))
        self.seen = len(tr)

    def consume(self, session, trace, start, end):
        pass


class Led(Part):
    """Lights when its line is driven high (anode to the pin, as on a Pico board)."""
    kind = "led"

    def __init__(self, name, pin, color=(1.0, 0.25, 0.2)):
        super().__init__(name, pin=pin)
        self.color = color
        self.on = False
        self.toggles = 0

    def consume(self, session, trace, start, end):
        p = self.pins["pin"]
        for i in range(start, end):
            v = bool((trace.out[i] & trace.dir[i]) >> p & 1)
            if v != self.on:
                self.toggles += 1
                self.on = v


class Button(Part):
    """Push button to ground with the line's pull-up: pressed reads 0."""
    kind = "button"

    def __init__(self, name, pin):
        super().__init__(name, pin=pin)
        self.pressed = False

    def attach(self, session):
        super().attach(session)
        self.set(session, False)

    def set(self, session, pressed):
        self.pressed = pressed
        session.drive(self.pins["pin"], 0 if pressed else None)


class UartTerminal(Part):
    """Receives 8N1 from its RX line (the tile's TX), clocks_per_bit system clocks per bit."""
    kind = "terminal"

    def __init__(self, name, rx, clocks_per_bit):
        super().__init__(name, rx=rx)
        self.cpb = clocks_per_bit
        self.text = ""
        self.framing_errors = 0
        self._state = None                 # None idle, else (index of start edge)
        self._prev = 1

    def consume(self, session, trace, start, end):
        p = self.pins["rx"]
        line = [(trace.inp[i] >> p) & 1 for i in range(start, end)]
        i = 0
        # Keep a partially received byte across batches by working in absolute indices.
        while i < len(line):
            if self._state is None:
                if self._prev == 1 and line[i] == 0:
                    self._state = start + i
                self._prev = line[i]
                i += 1
                continue
            t0 = self._state
            last_sample = t0 + int(9.5 * self.cpb)
            if last_sample >= end:
                break                      # wait for more clocks
            def at(k):
                idx = t0 + int((k + 0.5) * self.cpb)
                return (trace.inp[idx] >> p) & 1
            if at(0) != 0:
                self._state = None        # glitch, not a start bit
            else:
                byte = sum(at(1 + b) << b for b in range(8))
                if at(9) != 1:
                    self.framing_errors += 1
                self.text += chr(byte) if 32 <= byte < 127 or byte in (10, 13) else f"\\x{byte:02x}"
                self._state = None
            i = last_sample - start + 1
            self._prev = 1
        if self._state is None and line:
            self._prev = line[-1] if i >= len(line) else self._prev
