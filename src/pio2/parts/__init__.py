# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""Virtual parts wired to the tile's pins ([SPEC-DBG-15]).

A part is a simulation model, independent of the GUI: after each batch of clocks it reads the new
part of the session trace (`update`), and it may drive input lines (`Session.drive`). The GUI only
draws the part's state. The interface is `pio2.parts.base.Part`; stimulus sources
(`pio2.debug.stimulus`) implement the same interface and can be placed in a diagram too.

Parts: LED, push button, switch, logic probe, UART terminal, WS2812 strip, ESC + motor (DShot /
bidirectional DShot reply, serial passthrough), SPI IMU with DRDY (register image), I2C sensor,
quadrature encoder, square-wave source.
"""
# @impl [SPEC-DBG-15] docs/DEBUGGER_SPECIFICATION.md

import math

from pio2 import dshot_ref
from pio2.debug.part import Part, line  # noqa: F401  (re-exported)
from pio2.debug.stimulus import DShotDecoder, I2CSlave, schedule_dshot_reply


class Led(Part):
    """Lights when its line is driven high (anode to the pin, as on a Pico board). `duty` is the
    fraction of the last `window` clocks it was on (PWM brightness)."""
    kind = "led"

    def __init__(self, name, pin, color=(1.0, 0.25, 0.2), window=4096):
        super().__init__(name, pin=pin)
        self.color = tuple(color)
        self.window = window
        self.params = {"color": list(self.color), "window": window}
        self.on = False
        self.toggles = 0
        self.duty = 0.0
        self._on_clocks = 0
        self._clocks = 0

    def consume(self, session, trace, start, end):
        p = self.pins["pin"]
        for i in range(start, end):
            v = bool((trace.out[i] & trace.dir[i]) >> p & 1)
            if v != self.on:
                self.toggles += 1
                self.on = v
            self._on_clocks += v
            self._clocks += 1
            if self._clocks >= self.window:
                self.duty = self._on_clocks / self._clocks
                self._on_clocks = self._clocks = 0


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

    def drives(self):
        return [self.pins["pin"]]


class Switch(Button):
    """Toggle switch: like the button, but it stays where it was put."""
    kind = "switch"

    def toggle(self, session):
        self.set(session, not self.pressed)


class LogicProbe(Part):
    """Watches one line: edges, last period and high time (clocks), frequency at `f_sys`."""
    kind = "probe"

    def __init__(self, name, pin, f_sys=125e6):
        super().__init__(name, pin=pin)
        self.f_sys = f_sys
        self.params = {"f_sys": f_sys}
        self.level = None
        self.edges = 0
        self.last_rise = None
        self.last_fall = None
        self.period = None
        self.high = None

    @property
    def freq(self):
        return self.f_sys / self.period if self.period else 0.0

    def consume(self, session, trace, start, end):
        p = self.pins["pin"]
        for i in range(start, end):
            v = (trace.inp[i] >> p) & 1
            c = trace.start + i + 1
            if self.level is not None and v != self.level:
                self.edges += 1
                if v:
                    if self.last_rise is not None:
                        self.period = c - self.last_rise
                    self.last_rise = c
                else:
                    if self.last_rise is not None:
                        self.high = c - self.last_rise
                    self.last_fall = c
            self.level = v


class UartTerminal(Part):
    """Receives 8N1 from its RX line (the tile's TX), clocks_per_bit system clocks per bit."""
    kind = "terminal"

    def __init__(self, name, rx, clocks_per_bit):
        super().__init__(name, rx=rx)
        self.cpb = clocks_per_bit
        self.params = {"clocks_per_bit": clocks_per_bit}
        self.text = ""
        self.data = bytearray()
        self.framing_errors = 0
        self._t0 = None                    # absolute cycle of the start edge being received
        self._prev = 1

    def consume(self, session, trace, start, end):
        p = self.pins["rx"]
        base = trace.start
        i = start
        while i < end:
            if self._t0 is None:
                v = (trace.inp[i] >> p) & 1
                if self._prev == 1 and v == 0:
                    self._t0 = base + i
                self._prev = v
                i += 1
                continue
            t0 = self._t0
            last = t0 + int(9.5 * self.cpb)
            if last >= base + end:
                return                     # wait for more clocks (positions are absolute)
            if t0 < base:                  # the start edge left the trace: give up on it
                self._t0, self._prev = None, 1
                continue

            def at(k):
                return (trace.inp[t0 - base + int((k + 0.5) * self.cpb)] >> p) & 1
            if at(0) != 0:
                self._t0 = None            # glitch, not a start bit
                i = t0 - base + 1
                continue
            byte = sum(at(1 + b) << b for b in range(8))
            if at(9) != 1:
                self.framing_errors += 1
            self.data.append(byte)
            self.text += chr(byte) if 32 <= byte < 127 or byte in (10, 13) else f"\\x{byte:02x}"
            self._t0 = None
            i = last - base + 1
            self._prev = 1


class Ws2812Strip(Part):
    """WS2812 / SK6812 strip on one data line: a high pulse longer than `threshold` x the bit time is
    a 1; 24 bits per LED, GRB, MSB first; the line low for `reset_clocks` latches the frame."""
    kind = "ws2812"

    def __init__(self, name, pin, n_leds=8, bit_clocks=156, reset_clocks=6250, threshold=0.45):
        super().__init__(name, pin=pin)
        self.n, self.bit, self.reset, self.threshold = n_leds, bit_clocks, reset_clocks, threshold
        self.params = {"n_leds": n_leds, "bit_clocks": bit_clocks, "reset_clocks": reset_clocks,
                       "threshold": threshold}
        self.colors = [(0, 0, 0)] * n_leds     # latched (r, g, b)
        self.pending = []
        self.bits = []
        self.frames = 0
        self._prev = 0
        self._rise = None
        self._fall = None

    def consume(self, session, trace, start, end):
        p = self.pins["pin"]
        for i in range(start, end):
            v = (trace.inp[i] >> p) & 1
            c = trace.start + i + 1
            if v and not self._prev:
                if self._fall is not None and c - self._fall >= self.reset:
                    self.latch()
                self._rise = c
            elif not v and self._prev and self._rise is not None:
                self.bits.append(1 if c - self._rise > self.threshold * self.bit else 0)
                self._fall = c
                if len(self.bits) == 24:
                    g, r, b = (int("".join(map(str, self.bits[k:k + 8])), 2) for k in (0, 8, 16))
                    self.pending.append((r, g, b))
                    self.bits = []
            elif not v and self._fall is not None and c - self._fall == self.reset:
                self.latch()
            self._prev = v

    def latch(self):
        if self.pending:
            got = self.pending[:self.n]
            self.colors = got + self.colors[len(got):]
            self.frames += 1
        self.pending, self.bits = [], []


class Esc(Part):
    """ESC with a motor model, on one motor wire.

    mode "dshot": decodes the (bidirectional, inverted) DShot frames the tile sends, runs a
    first-order motor model (`max_rpm` at full throttle, time constant `tau_clocks`) and, for each
    frame with a valid inverted CRC, answers `reply_delay_clocks` later with the GCR eRPM telemetry
    (`pio2.dshot_ref`), as the RTL tests' ESC does.

    mode "serial": the 4-way passthrough wire (8N1, `baud_clocks` per bit, half duplex). Bytes the
    tile sends are collected; once the tile releases the line, the ESC answers after
    `turnaround_clocks` with the reply its `script` gives for the request (hex strings ->
    hex strings; "*" matches anything), or nothing.
    """
    kind = "esc"

    def __init__(self, name, pin, mode="dshot", dshot_bit_clocks=208, reply_delay_clocks=3750,
                 poles=14, max_rpm=30000, tau_clocks=2_000_000, f_sys=125e6, baud_clocks=6510,
                 turnaround_clocks=None, script=None):
        super().__init__(name, pin=pin)
        self.mode = mode
        self.params = {"mode": mode, "dshot_bit_clocks": dshot_bit_clocks,
                       "reply_delay_clocks": reply_delay_clocks, "poles": poles, "max_rpm": max_rpm,
                       "tau_clocks": tau_clocks, "f_sys": f_sys, "baud_clocks": baud_clocks,
                       "turnaround_clocks": turnaround_clocks, "script": dict(script or {})}
        self.decoder = DShotDecoder(dshot_bit_clocks)
        self.reply_delay = reply_delay_clocks
        self.poles, self.max_rpm, self.tau, self.f_sys = poles, max_rpm, tau_clocks, f_sys
        self.rpm = 0.0
        self.throttle = 0
        self.frames = 0
        self.bad_crc = 0
        self.replies = 0
        self.last_payload = None
        self._t_model = None
        # serial
        self.baud = baud_clocks
        self.turnaround = turnaround_clocks if turnaround_clocks is not None else 2 * baud_clocks
        self.script = {k.lower(): v for k, v in (script or {}).items()}
        self.rx = bytearray()              # bytes of the current request
        self.requests = []                 # (cycle, bytes)
        self.answers = []                  # (cycle, bytes)
        self._srx_t0 = None                # absolute cycle of the start edge of the byte in progress
        self._sbits = []
        self._sprev = 1
        self._sdriven_prev = 0
        self._busy_until = 0

    def drives(self):
        return [self.pins["pin"]]

    def wake(self, cycle):
        w = super().wake(cycle)
        horizon = (self.reply_delay if self.mode == "dshot" else self.turnaround) // 2
        h = cycle + max(1, horizon)
        return h if w is None else min(w, h)

    # ------------------------------------------------------------ DShot
    def erpm_period_us(self):
        erpm = self.rpm * self.poles / 2
        if erpm < 1:
            return 65408                   # e=7, m=511: the slowest the format can say
        return min(65408, max(1, int(60e6 / erpm)))

    def model(self, c):
        if self._t_model is not None:
            dt = c - self._t_model
            target = self.max_rpm * max(0, self.throttle - 47) / 2000
            self.rpm += (target - self.rpm) * (1 - math.exp(-dt / self.tau))
        self._t_model = c

    def consume(self, session, trace, start, end):
        if self.mode == "dshot":
            for frame, t_end in self.decoder.feed(trace, start, end, self.pins["pin"]):
                self.model(t_end)
                self.frames += 1
                if not frame["crc_ok"]:
                    self.bad_crc += 1
                    continue
                v = frame["value"]
                self.throttle = v if v >= 48 else 0
                self.last_payload = dshot_ref.erpm_payload(self.erpm_period_us())
                schedule_dshot_reply(self, t_end + self.reply_delay, self.pins["pin"], self.last_payload,
                                     self.decoder.bit * 4 / 5)
                self.replies += 1
        else:
            self.consume_serial(trace, start, end)

    @property
    def telemetry_rpm(self):
        """RPM the last reply says (what the flight controller decodes)."""
        if self.last_payload is None:
            return 0
        p = dshot_ref.erpm_period_us(self.last_payload >> 4)
        return 0 if p >= 65408 else 60e6 / p / (self.poles / 2)

    # ------------------------------------------------------------ serial passthrough
    def consume_serial(self, trace, start, end):
        """Clock by clock: bytes while the tile drives the wire, the request ends when it releases."""
        p = self.pins["pin"]
        for i in range(start, end):
            c = trace.start + i + 1
            driven = (trace.dir[i] >> p) & 1
            v = (trace.inp[i] >> p) & 1
            if self._srx_t0 is not None:
                k = len(self._sbits)
                if c >= self._srx_t0 + int((k + 0.5) * self.baud):
                    self._sbits.append(v)
                    if len(self._sbits) == 10:
                        if self._sbits[0] == 0:
                            self.rx.append(sum(b << j for j, b in enumerate(self._sbits[1:9])))
                        self._srx_t0, self._sbits = None, []
            elif driven and self._sprev == 1 and v == 0:
                self._srx_t0, self._sbits = c, []
            if self._sdriven_prev and not driven and self.rx and self._srx_t0 is None:
                self.requests.append((c, bytes(self.rx)))
                self.answer(c + self.turnaround, bytes(self.rx))
                self.rx = bytearray()
            self._sdriven_prev = driven
            self._sprev = v

    def answer(self, t, request):
        reply = self.script.get(request.hex())
        if reply is None:
            reply = self.script.get("*")
        if not reply:
            return
        data = bytes.fromhex(reply)
        t = max(t, self._busy_until)
        pin = self.pins["pin"]
        for n, b in enumerate(data):
            for k, level in enumerate([0] + [(b >> j) & 1 for j in range(8)] + [1]):
                self.drive_at(t + round((10 * n + k) * self.baud), pin, level)
        end = t + round(10 * len(data) * self.baud)
        self.drive_at(end, pin, None)
        self._busy_until = end
        self.answers.append((t, data))


class SpiImu(Part):
    """SPI IMU (mode 0, MSB first, active-low CS) with a 128-byte register image and data ready.

    First byte: bit 7 = read, bits 6:0 = register; then data, the address auto-increments. Every
    `odr_clocks` the sample registers (`data_reg`.. +11: accel X/Y/Z, gyro X/Y/Z, big-endian int16,
    a slow sine per axis) update and DRDY goes high; reading `data_reg` (or `drdy_clocks` later)
    clears it. Each MISO change is scheduled `latency` clocks after the SCK falling edge that calls
    for it, so the timing does not depend on how the run is batched; while CS is low the part looks
    at the lines every `latency // 2` clocks (SCK low time must exceed `latency`). `late` counts
    changes made too late (should stay 0).
    """
    kind = "imu"

    def __init__(self, name, sck, mosi, miso, cs, drdy=None, regs=None, who_am_i_reg=0x75,
                 who_am_i=0x47, data_reg=0x1F, odr_clocks=12500, drdy_clocks=None, latency=4,
                 idle_horizon=64):
        pins = {"sck": sck, "mosi": mosi, "miso": miso, "cs": cs}
        if drdy is not None:
            pins["drdy"] = drdy
        super().__init__(name, **pins)
        self.regs = bytearray(128)
        self.regs[who_am_i_reg] = who_am_i
        for k, v in (regs or {}).items():
            self.regs[int(k)] = v
        self.data_reg, self.odr, self.drdy_clocks = data_reg, odr_clocks, drdy_clocks
        self.latency, self.idle_horizon = latency, idle_horizon
        self.params = {"regs": {str(k): v for k, v in (regs or {}).items()}, "who_am_i_reg": who_am_i_reg,
                       "who_am_i": who_am_i, "data_reg": data_reg, "odr_clocks": odr_clocks,
                       "drdy_clocks": drdy_clocks, "latency": latency, "idle_horizon": idle_horizon}
        self.samples = 0
        self.sample_cycles = []
        self.values = []                   # the 6 values of each sample written, newest last
        self.frames = []                   # [(cycle, bytes in, bytes out)]
        self.late = 0
        self.drdy = 0
        self._next_sample = None
        self._prev_sck = 0
        self._prev_cs = 1
        self._active = False
        self._bit = 0
        self._shift = 0
        self._out = 0
        self._addr = None
        self._read = False
        self._in = bytearray()
        self._sent = bytearray()
        self._now = 0

    def drives(self):
        return [self.pins["miso"]] + ([self.pins["drdy"]] if "drdy" in self.pins else [])

    def attach(self, session):
        super().attach(session)
        session.drive(self.pins["miso"], 0)
        if "drdy" in self.pins:
            session.drive(self.pins["drdy"], 0)
        self._next_sample = session.cycle + self.odr

    def wake(self, cycle):
        w = super().wake(cycle)
        for x in (self._next_sample, cycle + (max(1, self.latency // 2) if self._active else self.idle_horizon)):
            if x is not None:
                w = x if w is None else min(w, x)
        return w

    @staticmethod
    def sample_values(k):
        return [int(8000 * math.sin(2 * math.pi * (k / 50 + a / 6))) for a in range(6)]

    def new_sample(self, session, c):
        self.samples += 1
        self.sample_cycles.append(c)
        vals = self.sample_values(self.samples)
        self.values.append(vals)
        if len(self.values) > 1000:
            del self.values[:-1000], self.sample_cycles[:-1000]
        for a, v in enumerate(vals):
            v &= 0xFFFF
            self.regs[self.data_reg + 2 * a] = v >> 8
            self.regs[self.data_reg + 2 * a + 1] = v & 0xFF
        if "drdy" in self.pins:
            self.drdy = 1
            self.drive_at(c, self.pins["drdy"], 1)
            if self.drdy_clocks:
                self.drive_at(c + self.drdy_clocks, self.pins["drdy"], 0)

    def apply(self, session):
        while self._next_sample is not None and self._next_sample <= session.cycle:
            self.new_sample(session, self._next_sample)
            self._next_sample += self.odr
        super().apply(session)

    def set_miso(self, bit, c):
        t = c + self.latency
        if t < self._now:
            self.late += 1
        self.drive_at(t, self.pins["miso"], bit)

    def consume(self, session, trace, start, end):
        sck_p, mosi_p, cs_p = self.pins["sck"], self.pins["mosi"], self.pins["cs"]
        self._now = session.cycle
        for i in range(start, end):
            v = trace.inp[i]
            sck = (v >> sck_p) & 1
            cs = (v >> cs_p) & 1
            c = trace.start + i + 1
            if self._prev_cs and not cs:
                self._active, self._bit, self._shift, self._addr = True, 0, 0, None
                self._in, self._sent = bytearray(), bytearray()
            elif cs and not self._prev_cs:
                if self._in or self._sent:
                    self.frames.append((c, bytes(self._in), bytes(self._sent)))
                    if len(self.frames) > 1000:
                        del self.frames[:-1000]
                self._active = False
            self._prev_cs = cs
            if cs:
                self._prev_sck = sck
                continue
            if sck and not self._prev_sck:                     # rising: sample MOSI
                self._shift = ((self._shift << 1) | ((v >> mosi_p) & 1)) & 0xFF
                self._bit += 1
                if self._bit == 8:
                    self._in.append(self._shift)
                    if self._addr is None:
                        self._read = bool(self._shift & 0x80)
                        self._addr = self._shift & 0x7F
                    elif not self._read:
                        self.regs[self._addr & 0x7F] = self._shift
                        self._addr += 1
                    self._bit, self._shift = 0, 0
            elif not sck and self._prev_sck:                   # falling: next MISO bit
                if self._addr is not None and self._read:
                    if self._bit == 0:
                        self._out = self.regs[self._addr & 0x7F]
                        self._sent.append(self._out)
                        if (self._addr & 0x7F) == self.data_reg and self.drdy and "drdy" in self.pins:
                            self.drdy = 0
                            self.drive_at(c, self.pins["drdy"], 0)
                        self._addr += 1
                    self.set_miso((self._out >> (7 - self._bit)) & 1, c)
            self._prev_sck = sck


class I2cSensor(I2CSlave):
    """I2C sensor (a TMP102-style temperature sensor): `pio2.debug.stimulus.I2CSlave` with a register
    image whose sample register (`data_reg`, 2 bytes big-endian, 12.4 fixed point) counts up by one
    LSB step every `odr_clocks`, and an identification register."""
    kind = "i2c_sensor"

    def __init__(self, name, sda, scl, address=0x48, who_am_i_reg=0x0F, who_am_i=0xA5, data_reg=0x00,
                 odr_clocks=50_000, latency=16, idle_horizon=256):
        super().__init__(name, sda, scl, address, {who_am_i_reg: who_am_i}, latency, idle_horizon)
        self.params = {"address": address, "who_am_i_reg": who_am_i_reg, "who_am_i": who_am_i,
                       "data_reg": data_reg, "odr_clocks": odr_clocks, "latency": latency,
                       "idle_horizon": idle_horizon}
        self.data_reg, self.odr = data_reg, odr_clocks
        self._next = None
        self.set_value(0x1900)                 # 25.0 C

    def set_value(self, v):
        self.value = v & 0xFFFF
        self.regs[self.data_reg] = self.value >> 8
        self.regs[self.data_reg + 1] = self.value & 0xFF

    @property
    def celsius(self):
        return (self.value >> 4) / 16

    def attach(self, session):
        super().attach(session)
        self._next = session.cycle + self.odr

    def wake(self, cycle):
        w = super().wake(cycle)
        return self._next if w is None or (self._next is not None and self._next < w) else w

    def apply(self, session):
        while self._next is not None and self._next <= session.cycle:
            self.set_value(self.value + 0x10)
            self._next += self.odr
        super().apply(session)


class QuadratureEncoder(Part):
    """Quadrature encoder on two lines (A, B): one step every `step_clocks` clocks in `direction`
    (+1 / -1), Gray sequence 00 01 11 10 (A is bit 0). `position` counts the steps made."""
    kind = "encoder"

    SEQ = [0b00, 0b01, 0b11, 0b10]

    def __init__(self, name, a, b, step_clocks=200, direction=1, steps=None):
        super().__init__(name, a=a, b=b)
        self.step_clocks, self.direction, self.limit = step_clocks, direction, steps
        self.params = {"step_clocks": step_clocks, "direction": direction, "steps": steps}
        self.phase = 0
        self.position = 0
        self._next = None

    def drives(self):
        return [self.pins["a"], self.pins["b"]]

    def attach(self, session):
        super().attach(session)
        self.put(session)
        self._next = session.cycle + self.step_clocks

    def put(self, session):
        s = self.SEQ[self.phase % 4]
        session.drive(self.pins["a"], s & 1)
        session.drive(self.pins["b"], s >> 1 & 1)

    def wake(self, cycle):
        return self._next

    def apply(self, session):
        while self._next is not None and self._next <= session.cycle:
            if self.limit is not None and abs(self.position) >= self.limit or self.direction == 0:
                self._next = None
                break
            self.phase = (self.phase + self.direction) % 4
            self.position += self.direction
            self.put(session)
            self._next += self.step_clocks


def part_types():
    """Diagram type name -> constructor (parts and stimulus sources)."""
    from pio2.debug import stimulus as st
    return {"led": Led, "button": Button, "switch": Switch, "probe": LogicProbe, "terminal": UartTerminal,
            "ws2812": Ws2812Strip, "esc": Esc, "imu": SpiImu, "i2c_sensor": I2cSensor,
            "encoder": QuadratureEncoder, "square": st.SquareWave, "constant": st.Constant,
            "uart_tx": st.UartTx, "dshot_reply": st.DShotReply, "i2c_slave": st.I2CSlave,
            "replay": st.Replay, "fifo_feeder": st.FifoFeeder, "rx_collector": st.RxCollector}


def make_part(d):
    """A part from its diagram entry {"type", "name", "pins", "attrs"}."""
    types = part_types()
    if d["type"] not in types:
        raise ValueError(f"unknown part type {d['type']!r} (known: {', '.join(sorted(types))})")
    return types[d["type"]](d["name"], **d.get("pins", {}), **d.get("attrs", {}))
