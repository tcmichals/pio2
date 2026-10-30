# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""Example sessions ([SPEC-DBG-11]): program + pins + parts + stimulus, usable from the GUI and headless.

Each example is a function (session) -> Example. The function loads and configures the program the
way the original C code does (the pico-examples `*_program_init`, Betaflight, our ESC programs); the
wiring and the parts come from the session's diagram file (`sessions/<name>.diagram.json`,
[SPEC-DBG-15]); host scripts (what the CPU does on the other side of the FIFOs) are added in code.
`pio2.debug.bench.Bench` runs it.
"""
# @impl [SPEC-DBG-11] [SPEC-DBG-15] docs/DEBUGGER_SPECIFICATION.md

import colorsys
from dataclasses import dataclass, field

from pio2.debug import diagram
from pio2.debug.session import Pins
from pio2.debug.stimulus import (DShotHost, EscSerialHost, FifoFeeder, I2cHost, ImuHost, RxCollector,
                                 UartRxHost)

F_SYS = 125e6

# SHIFTCTRL fields (RP2040 layout)
AUTOPUSH, AUTOPULL, IN_RIGHT, OUT_RIGHT = 1 << 16, 1 << 17, 1 << 18, 1 << 19
FJOIN_TX, FJOIN_RX = 1 << 30, 1 << 31


def push_thresh(n):
    return (n & 31) << 20


def pull_thresh(n):
    return (n & 31) << 25


@dataclass
class Example:
    title: str
    about: str
    parts: list
    probes: list                           # GPIO lines shown in the logic analyzer
    labels: dict = field(default_factory=dict)   # GPIO line -> name on the canvas
    speed: int = 200                       # GUI: clocks per frame when running
    window: int = 2000                     # GUI: clocks shown in the logic analyzer
    f_sys: float = F_SYS                   # system clock the timing was set up for
    reference: str = ""                    # how to compare with the Wokwi / original version
    diagram: str = ""                      # diagram file name

    def part(self, name):
        return next(p for p in self.parts if p.name == name)


def from_diagram(name, *hosts, **over):
    d = diagram.load(name)
    v = d["view"]
    kw = dict(title=d["title"], about=d["about"], parts=d["parts"] + list(hosts), probes=d["probes"],
              labels=d["labels"], speed=v.get("speed", 200), window=v.get("window", 2000),
              f_sys=v.get("f_sys", F_SYS), reference=d.get("reference", ""), diagram=name)
    kw.update(over)
    return Example(**kw)


def clkdiv_for(f_sys, f_sm):
    """The divider the SDK would compute (8-bit fraction), as a float."""
    d = f_sys / f_sm
    i, frac = int(d), int(round((d - int(d)) * 256))
    if frac == 256:
        i, frac = i + 1, 0
    return i + frac / 256


# ---------------------------------------------------------------------------- pico-examples
def blink(s):
    led = 25
    s.load("blink", sm=0, pinctrl=Pins(set_base=led, set_count=1), enable=False)
    s.set_pindirs(0, 1 << led, 1 << led)
    s.enable(0)
    s.push(0, 3000)                        # blink's delay (x + 3 clocks per half period)
    return from_diagram("blink")


def uart_hello(s):
    tx = 0
    div = 2.0                              # 8 SM clocks per bit -> 16 system clocks per bit
    s.load("uart_tx", sm=0, pinctrl=Pins(out_base=tx, out_count=1, sideset_base=tx), clkdiv=div,
           shiftctrl=OUT_RIGHT, enable=False)    # OUT shifts right, as uart_tx_program_init
    s.set_pindirs(0, 1 << tx, 1 << tx)
    s.enable(0)
    msg = b"Hello from the pio2 RTL!\r\n"
    return from_diagram("uart_tx", FifoFeeder("printf", sm=0, words=list(msg), every=160))


# `mov pins, ~pins`: the OUT window copies the inverted IN window (button to ground, pull-up).
BUTTON_LED = {"name": "button_led", "instructions": [0xA008], "wrap_target": 0, "wrap": 0,
              "origin": -1, "sideset": {"size": 0, "optional": False, "pindirs": False}, "labels": {}}


def button_led(s):
    btn, led = 14, 15
    s.load(BUTTON_LED, sm=0, pinctrl=Pins(out_base=led, out_count=1, in_base=btn), enable=False)
    s.set_pindirs(0, 1 << led, 1 << led)
    s.enable(0)
    return from_diagram("button_led")


def squarewave(s):
    """pico-examples hello squarewave: SET pins on GPIO 0, clock divider 2.5."""
    s.load("squarewave", sm=0, pinctrl=Pins(set_base=0, set_count=1), clkdiv=2.5)
    return from_diagram("squarewave")


def pwm(s):
    """pico-examples pio/pwm: period into the ISR by SMx_INSTR, level through the TX FIFO."""
    led, period, level = 25, 99, 25
    s.load("pwm", sm=0, pinctrl=Pins(sideset_base=led), enable=False)
    s.set_pindirs(0, 1 << led, 1 << led)
    s.push(0, period)                      # pio_pwm_set_period: put, exec pull, exec out isr 32
    s.exec(0, 0x8080)
    s.exec(0, 0x60C0)
    s.enable(0)
    return from_diagram("pwm", FifoFeeder("level", sm=0, words=[level], repeat=False, every=1000))


def rainbow_word(i, n=8):
    """WS2812 word (GRB in bits 31:8) for LED i % n of frame i // n: a moving rainbow."""
    frame, led = divmod(i, n)
    r, g, b = (int(255 * c) for c in colorsys.hsv_to_rgb(((led + frame) % n) / n, 1.0, 0.6))
    return (g << 24) | (r << 16) | (b << 8)


def _ws2812_setup(s, program, pin):
    div = clkdiv_for(F_SYS, 800_000 * 10)  # ws2812_program_init: 800 kHz, T1 + T2 + T3 = 10
    s.load(program, sm=0, pinctrl=Pins(sideset_base=pin), clkdiv=div,
           shiftctrl=AUTOPULL | pull_thresh(24) | FJOIN_TX, enable=False)
    s.set_pindirs(0, 1 << pin, 1 << pin)
    s.enable(0)
    bit = 10 * div
    return FifoFeeder("frames", sm=0, fn=rainbow_word, burst=8, gap=int(24 * bit + 6250 + 500), every=500)


def ws2812(s):
    feeder = _ws2812_setup(s, "ws2812", 2)
    return from_diagram("ws2812", feeder)


def uart_rx(s):
    """pico-examples uart_rx at 115200 baud: a UART source on GPIO 3 sends text."""
    rx = 3
    div = clkdiv_for(F_SYS, 8 * 115_200)
    s.load("uart_rx", sm=0, pinctrl=Pins(in_base=rx), clkdiv=div, shiftctrl=IN_RIGHT | FJOIN_RX,
           execctrl=rx << 24)
    return from_diagram("uart_rx", UartRxHost("getc", sm=0, every=4000))


def quadrature(s):
    """pico-examples quadrature_encoder on GPIO 10 / 11 (program at address 0, clock divider 1)."""
    a = 10
    s.load("quadrature_encoder", sm=0, offset=0, pinctrl=Pins(in_base=a), execctrl=a << 24)
    return from_diagram("quadrature", RxCollector("count", sm=0, every=2000, keep=8))


def spi_imu(s):
    """pico-examples spi_cpha0_cs (SCK / CSn by side-set) reading an SPI IMU with DRDY."""
    miso, mosi, sck, drdy = 16, 17, 18, 20
    p = s.load("spi_cpha0_cs", sm=0, pinctrl=Pins(out_base=mosi, out_count=1, sideset_base=sck, in_base=miso),
               clkdiv=8.0, shiftctrl=AUTOPULL | pull_thresh(8) | AUTOPUSH | push_thresh(8), enable=False)
    s.set_pins(0, (3 << sck) | (1 << mosi), 2 << sck)             # CSn high, SCK low
    s.set_pindirs(0, (3 << sck) | (1 << mosi) | (1 << miso), (3 << sck) | (1 << mosi))
    s.exec(0, 0xE026)                      # set x, 6  (n_bits - 2)
    s.exec(0, 0xE046)                      # set y, 6
    s.exec(0, p["labels"]["entry_point"])  # jmp entry_point
    s.enable(0)
    return from_diagram("spi_imu", ImuHost("host", sm=0, drdy=drdy, data_reg=0x1F, count=12))


def i2c_sensor(s):
    """pico-examples i2c (open drain through the pad's inverted output enable) reading a sensor."""
    sda, scl = 4, 5
    both = (1 << sda) | (1 << scl)
    p = s.load("i2c", sm=0, pinctrl=Pins(out_base=sda, out_count=1, set_base=sda, set_count=1, in_base=sda,
                                         sideset_base=scl),
               clkdiv=clkdiv_for(F_SYS, 32 * 100_000), execctrl=sda << 24,
               shiftctrl=AUTOPULL | pull_thresh(16) | AUTOPUSH | push_thresh(8), enable=False)
    s.set_pins(0, both, both)
    s.set_pindirs(0, both, both)
    s.set_oe_invert(both)                  # gpio_set_oeover(..., GPIO_OVERRIDE_INVERT)
    s.set_pins(0, both, 0)
    s.write(0x04, 1)                       # pio_interrupt_clear
    s.exec(0, p["labels"]["entry_point"])
    s.enable(0)
    return from_diagram("i2c_sensor", I2cHost("host", sm=0, address=0x48, reg=0x00, count=2,
                                              period_clocks=150_000, entry_point=p["labels"]["entry_point"]))


# ---------------------------------------------------------------------------- our ESC programs
def dshot(s):
    """src/programs/dshot_bidir.pio at DShot600 with an ESC that answers with eRPM telemetry."""
    m = 4
    div = clkdiv_for(F_SYS, 600_000 * 16)
    s.load("dshot_bidir", sm=0, pinctrl=Pins(out_base=m, out_count=1, set_base=m, set_count=1, in_base=m),
           clkdiv=div, shiftctrl=AUTOPUSH, execctrl=m << 24)       # JMP_PIN = motor line
    return from_diagram("dshot", DShotHost("fc", sm=0, throttle=1046, period_clocks=20_000))


ESC_INIT = "0000000000000000000d424c48656c69f47d"     # BLHeli bootloader init: 8 x 0, 0x0D "BLHeli", CRC


def esc_serial(s):
    """src/programs/esc_serial.pio: the 4-way passthrough wire at 19200 8N1, half duplex, with a
    scripted ESC bootloader answering (27 MHz system clock, as on the Tang boards)."""
    pin, f_sys = 2, 27e6
    div = clkdiv_for(f_sys, 19_200 * 32)
    s.load("esc_serial", sm=0, pinctrl=Pins(out_base=pin, out_count=1, set_base=pin, set_count=1, in_base=pin),
           clkdiv=div, shiftctrl=IN_RIGHT | OUT_RIGHT, execctrl=pin << 24)
    return from_diagram("esc_serial", EscSerialHost("host", sm=0, messages=[ESC_INIT, "fd00"],
                                                    quiet_clocks=60_000, gap_clocks=20_000))


# ---------------------------------------------------------------------------- Betaflight
def bf_ws2812(s):
    """Betaflight's WS2812 program (light_ws2811strip_pico.c), read from $BETAFLIGHT_DIR."""
    from pio2.examples import load_betaflight
    feeder = _ws2812_setup(s, load_betaflight("bf_ws2812"), 2)
    return from_diagram("ws2812", feeder, title="Betaflight WS2812 strip on GPIO 2",
                        about="Betaflight light_ws2811strip_pico.c program, read from the Betaflight checkout "
                              "($BETAFLIGHT_DIR); same timing and strip as the pico-examples session.")


EXAMPLES = {"Blink": blink, "UART TX": uart_hello, "Button to LED": button_led, "DShot": dshot,
            "ESC serial": esc_serial, "WS2812": ws2812, "Square wave": squarewave, "PWM": pwm,
            "UART RX": uart_rx, "Quadrature encoder": quadrature, "SPI IMU": spi_imu,
            "I2C sensor": i2c_sensor, "Betaflight WS2812": bf_ws2812}
