# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""Example sessions ([SPEC-DBG-11]): program + pins + parts + stimulus, usable from the GUI and headless.

Each example is a function (session) -> Example. `feed(session)` is called between batches of clocks
(the stimulus side: e.g. keep the UART's TX FIFO filled).
"""
# @impl [SPEC-DBG-11] [SPEC-DBG-15] docs/DEBUGGER_SPECIFICATION.md

from dataclasses import dataclass, field

from pio2.debug.session import Pins
from pio2.parts import Button, Led, UartTerminal


@dataclass
class Example:
    title: str
    about: str
    parts: list
    probes: list                           # GPIO lines shown in the logic analyzer
    feed: object = None                    # callable(session) or None
    labels: dict = field(default_factory=dict)   # GPIO line -> name on the canvas
    speed: int = 200                       # GUI: clocks per frame when running
    window: int = 2000                     # GUI: clocks shown in the logic analyzer


def blink(s):
    led = 25
    s.load("blink", sm=0, pinctrl=Pins(set_base=led, set_count=1), enable=False)
    s.set_pindirs(0, 1 << led, 1 << led)
    s.enable(0)
    s.push(0, 3000)                        # blink's delay (x + 3 clocks per half period)
    return Example("Blink: LED on GPIO 25", "pico-examples pio_blink: SET pins drives the LED; "
                   "the TX FIFO word is the delay.", [Led("LED", led)], [led], labels={led: "LED"},
                   speed=100, window=15000)


def uart_hello(s):
    tx = 0
    div = 2.0                              # 8 SM clocks per bit -> 16 system clocks per bit
    s.load("uart_tx", sm=0, pinctrl=Pins(out_base=tx, out_count=1, sideset_base=tx), clkdiv=div,
           shiftctrl=1 << 19, enable=False)    # OUT shifts right, as uart_tx_program_init
    s.set_pindirs(0, 1 << tx, 1 << tx)
    s.enable(0)
    msg = b"Hello from the pio2 RTL!\r\n"
    pos = {"i": 0}

    def feed(sess):
        # push while the TX FIFO has room (FSTAT TX full = bit 24)
        while not (sess.read(0x08) >> 24) & 1:
            sess.push(0, msg[pos["i"] % len(msg)])
            pos["i"] += 1

    return Example("UART TX: terminal on GPIO 0", "pico-examples uart_tx: OUT pins and an optional "
                   "side-set on the same line, 8 SM clocks per bit.",
                   [UartTerminal("Terminal", rx=tx, clocks_per_bit=int(8 * div))], [tx], feed,
                   labels={tx: "TX"}, speed=160, window=1800)


# `mov pins, ~pins`: the OUT window copies the inverted IN window (button to ground, pull-up).
BUTTON_LED = {"name": "button_led", "instructions": [0xA008], "wrap_target": 0, "wrap": 0,
              "origin": -1, "sideset": {"size": 0, "optional": False, "pindirs": False}, "labels": {}}


def button_led(s):
    btn, led = 14, 15
    s.load(BUTTON_LED, sm=0, pinctrl=Pins(out_base=led, out_count=1, in_base=btn), enable=False)
    s.set_pindirs(0, 1 << led, 1 << led)
    s.enable(0)
    return Example("Button to LED: GPIO 14 to GPIO 15", "One instruction, `mov pins, ~pins`: the IN "
                   "window starts at the button, the OUT window at the LED. Hold the button.",
                   [Button("Button", btn), Led("LED", led, color=(0.2, 0.9, 0.4))], [btn, led],
                   labels={btn: "BTN", led: "LED"}, speed=50, window=3000)


def dshot(s):
    m = 4
    s.load("dshot_bidir", sm=0, pinctrl=Pins(out_base=m, out_count=1, set_base=m, set_count=1,
                                             in_base=m), clkdiv=1.0, shiftctrl=(1 << 16), enable=False,
           execctrl=m << 24)                # JMP_PIN = motor line
    s.enable(0)

    def feed(sess):
        if not (sess.read(0x08) >> 24) & 1:
            from pio2.dshot_ref import dshot_frame
            sess.push(0, dshot_frame(1046, telemetry=False, bidir=True))
        while sess.pull(0) is not None:      # no ESC attached: two zero words per frame
            pass

    return Example("Bidirectional DShot on GPIO 4", "dshot_bidir.pio: drive 16 bits, release the line, "
                   "listen for the ESC's reply (none here: the line idles high).", [], [m], feed,
                   labels={m: "MOTOR"}, speed=300, window=4000)


EXAMPLES = {"Blink": blink, "UART TX": uart_hello, "Button to LED": button_led, "DShot": dshot}
