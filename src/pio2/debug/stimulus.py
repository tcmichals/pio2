# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""Stimulus generators ([SPEC-DBG-07]): sources that drive input lines or feed the FIFOs.

Every generator is a `pio2.parts.base.Part`, so the bench runs it with the same cycle-exact timing
as a part, a diagram file can hold it, and reverse step restores it.

Line sources: `Constant`, `SquareWave`, `UartTx` (8N1 bytes at a bit time in clocks),
`DShotReply` (the ESC's GCR eRPM telemetry, helper of the ESC part), `I2CSlave` (ACK, register
pointer, data), `Replay` (a recorded capture). Toggling by hand is `pio2.parts.Button` / `Switch`.

FIFO sources: `FifoFeeder` (TX FIFO pushes from a list or a stateless function, in bursts),
`RxCollector` (RX FIFO pulls). Both use the AXI4-Lite registers, as a host would.

FSTAT in this tile: bits [31:28] RX empty, [27:24] TX full per SM (no TX-empty or RX-full bits).
"""
# @impl [SPEC-DBG-07] docs/DEBUGGER_SPECIFICATION.md

from pio2 import dshot_ref
from pio2.debug.part import Part

FSTAT = 0x08


def _sm(n, off):
    return 0x100 + 0x20 * n + off


# ---------------------------------------------------------------------------- line sources
class Constant(Part):
    """Drives one line to a fixed level (None: released, the pull decides)."""
    kind = "constant"

    def __init__(self, name, pin, level=0):
        super().__init__(name, pin=pin)
        self.level = level
        self.params = {"level": level}

    def attach(self, session):
        super().attach(session)
        session.drive(self.pins["pin"], self.level)

    def set(self, session, level):
        self.level = level
        self.params["level"] = level
        session.drive(self.pins["pin"], level)

    def drives(self):
        return [self.pins["pin"]]


class SquareWave(Part):
    """Square wave: `period` clocks, high for `high` clocks (default half), first rising edge at
    `phase` clocks after attach. Edges are exact to the clock."""
    kind = "square"

    def __init__(self, name, pin, period, high=None, phase=0):
        super().__init__(name, pin=pin)
        self.period, self.high, self.phase = int(period), int(high if high is not None else period // 2), int(phase)
        self.params = {"period": self.period, "high": self.high, "phase": self.phase}
        self.next_edge = None
        self.level = 0
        self.edges = 0

    def attach(self, session):
        super().attach(session)
        self.t0 = session.cycle + self.phase
        self.level = 0
        session.drive(self.pins["pin"], 0)
        self.next_edge = self.t0

    def wake(self, cycle):
        return self.next_edge

    def apply(self, session):
        while self.next_edge is not None and self.next_edge <= session.cycle:
            self.level ^= 1
            session.drive(self.pins["pin"], self.level)
            self.edges += 1
            self.next_edge += self.high if self.level else self.period - self.high

    def drives(self):
        return [self.pins["pin"]]


class UartTx(Part):
    """Sends bytes 8N1 (LSB first, idle high) on `pin`, `clocks_per_bit` clocks per bit.
    `send(data)` queues more bytes; `gap` idle clocks between bytes; `start` clocks after attach."""
    kind = "uart_tx"

    def __init__(self, name, pin, clocks_per_bit, data=b"", gap=0, start=0, release=False):
        super().__init__(name, pin=pin)
        self.cpb = clocks_per_bit
        self.queue = bytearray(data if isinstance(data, (bytes, bytearray)) else bytes(data))
        self.gap, self.start, self.release = int(gap), int(start), release
        self.params = {"clocks_per_bit": clocks_per_bit, "data": list(self.queue), "gap": self.gap,
                       "start": self.start, "release": release}
        self.free_at = None                # cycle the line is free for the next byte
        self.sent = bytearray()

    def attach(self, session):
        super().attach(session)
        if not self.release:
            session.drive(self.pins["pin"], 1)
        self.free_at = session.cycle + self.start

    def send(self, data):
        self.queue.extend(data)

    def wake(self, cycle):
        w = super().wake(cycle)
        if self.queue and self.free_at is not None:
            w = self.free_at if w is None else min(w, self.free_at)
        return w

    def apply(self, session):
        while self.queue and self.free_at is not None and self.free_at <= session.cycle:
            t = max(self.free_at, session.cycle)
            b = self.queue.pop(0)
            self.schedule_byte(t, b)
            self.sent.append(b)
            self.free_at = t + round(10 * self.cpb) + self.gap
            if self.release and not self.queue:
                self.drive_at(t + round(10 * self.cpb), self.pins["pin"], None)
        super().apply(session)

    def schedule_byte(self, t, b):
        pin = self.pins["pin"]
        for k, level in enumerate([0] + [(b >> i) & 1 for i in range(8)] + [1]):
            self.drive_at(t + round(k * self.cpb), pin, level)

    def drives(self):
        return [self.pins["pin"]]


def schedule_dshot_reply(part, t0, pin, payload16, bit_clocks):
    """The ESC's bidirectional-DShot answer: 21 GCR line bits (dshot_ref.reply_line_bits), the
    first starting at cycle t0, `bit_clocks` clocks each (5/4 of the DShot bit rate), then the line
    is released. Returns the cycle the reply ends."""
    for k, level in enumerate(dshot_ref.reply_line_bits(payload16)):
        part.drive_at(t0 + round(k * bit_clocks), pin, level)
    end = t0 + round(21 * bit_clocks)
    part.drive_at(end, pin, None)
    return end


class DShotReply(Part):
    """Answers every bidirectional DShot frame the tile sends on `pin` with a fixed eRPM period
    (`period_us`, at `f_sys`). The ESC part (`pio2.parts.Esc`) adds the motor model."""
    kind = "dshot_reply"

    def __init__(self, name, pin, dshot_bit_clocks, period_us=2000, reply_delay_clocks=None, f_sys=125e6):
        super().__init__(name, pin=pin)
        self.decoder = DShotDecoder(dshot_bit_clocks)
        self.period_us = period_us
        self.delay = int(reply_delay_clocks if reply_delay_clocks is not None else 30e-6 * f_sys)
        self.params = {"dshot_bit_clocks": dshot_bit_clocks, "period_us": period_us,
                       "reply_delay_clocks": self.delay, "f_sys": f_sys}
        self.frames = []
        self.replies = 0

    def wake(self, cycle):
        w = super().wake(cycle)
        h = cycle + max(1, self.delay // 2)
        return h if w is None else min(w, h)

    def consume(self, session, trace, start, end):
        for frame, t_end in self.decoder.feed(trace, start, end, self.pins["pin"]):
            self.frames.append(frame)
            if frame["crc_ok"]:
                schedule_dshot_reply(self, t_end + self.delay, self.pins["pin"],
                                     dshot_ref.erpm_payload(self.period_us), self.decoder.bit * 4 / 5)
                self.replies += 1

    def drives(self):
        return [self.pins["pin"]]


class DShotDecoder:
    """Decodes DShot frames from the line while the tile drives it: each bit starts with a falling
    edge; low for 3/4 of the bit is a 1, 3/8 a 0 (the line is inverted for bidirectional DShot)."""

    def __init__(self, bit_clocks):
        self.bit = bit_clocks
        self.prev = 1
        self.fall = None                   # cycle of the last falling edge
        self.bits = []
        self.last_fall = None

    def feed(self, trace, start, end, pin):
        out = []
        for i in range(start, end):
            driven = (trace.dir[i] >> pin) & 1
            v = (trace.inp[i] >> pin) & 1
            c = trace.start + i + 1
            if not driven:
                self.prev = v
                continue
            if self.prev == 1 and v == 0:
                if self.last_fall is not None and c - self.last_fall > 2 * self.bit:
                    self.bits = []
                self.fall = self.last_fall = c
            elif self.prev == 0 and v == 1 and self.fall is not None:
                low = c - self.fall
                self.bits.append(1 if low > 0.5625 * self.bit else 0)
                if len(self.bits) == 16:
                    out.append((decode_frame(self.bits), self.last_fall + round(self.bit)))
                    self.bits = []
            self.prev = v
        return out


def decode_frame(bits):
    """16 line bits (MSB first) -> value, telemetry bit and the CRC check (normal or inverted)."""
    w = 0
    for b in bits:
        w = (w << 1) | b
    v = w >> 4
    crc = (v ^ (v >> 4) ^ (v >> 8)) & 0xF
    return {"word": w, "value": v >> 1, "telemetry": v & 1, "crc_ok": (w & 0xF) == (~crc & 0xF),
            "crc_normal": (w & 0xF) == crc}


class I2CSlave(Part):
    """I2C slave on open-drain SDA / SCL (it only pulls SDA low or releases it): 7-bit `address`,
    a register pointer set by the first written byte, auto-increment, `regs` a 256-byte image.

    Timing is exact and independent of how the run is batched: every SDA change is scheduled
    `latency` clocks after the SCL falling edge that calls for it (ACK, read data, release), and the
    part looks at the lines at least every `latency // 2` clocks during a transfer (every
    `idle_horizon` clocks between transfers; the first SDA change comes 8 SCL bits after a START).
    `latency` must be shorter than SCL's low time. `late` counts changes that could not be made in
    time (should stay 0)."""
    kind = "i2c_slave"

    def __init__(self, name, sda, scl, address=0x68, regs=None, latency=16, idle_horizon=256):
        super().__init__(name, sda=sda, scl=scl)
        self.address = address
        self.regs = bytearray(256)
        for k, v in (regs or {}).items():
            self.regs[int(k)] = v
        self.latency, self.idle_horizon = latency, idle_horizon
        self.params = {"address": address, "regs": {str(k): v for k, v in (regs or {}).items()},
                       "latency": latency, "idle_horizon": idle_horizon}
        self.state = "idle"                # idle, addr, write, read, nak
        self.prev_sda, self.prev_scl = 1, 1
        self.bit, self.shift, self.ptr = 0, 0, 0
        self.first = True
        self.rw = 0
        self.out_byte = 0
        self.sda_drive = None              # what this slave puts on SDA (0 or None), as scheduled
        self.late = 0
        self.log = []                      # (cycle, event, value)
        self.writes = []

    def drives(self):
        return [self.pins["sda"]]

    def wake(self, cycle):
        w = super().wake(cycle)
        h = cycle + (max(1, self.latency // 2) if self.state != "idle" else self.idle_horizon)
        return h if w is None else min(w, h)

    def consume(self, session, trace, start, end):
        sda_p, scl_p = self.pins["sda"], self.pins["scl"]
        self._now = session.cycle
        for i in range(start, end):
            sda = (trace.inp[i] >> sda_p) & 1
            scl = (trace.inp[i] >> scl_p) & 1
            c = trace.start + i + 1
            if scl and self.prev_scl and sda != self.prev_sda and self.sda_drive is None:
                if not sda:                                    # START (or repeated START)
                    self.state, self.bit, self.shift = "addr", 0, 0
                    self.log.append((c, "start", None))
                else:                                          # STOP
                    self.state = "idle"
                    self.log.append((c, "stop", None))
            elif scl and not self.prev_scl:                    # rising: sample
                self.on_rise(sda, c)
            elif not scl and self.prev_scl:                    # falling: drive
                self.on_fall(c)
            self.prev_sda, self.prev_scl = sda, scl

    def set_sda(self, level, c):
        if level != self.sda_drive:
            self.sda_drive = level
            t = c + self.latency
            if t < self._now:
                self.late += 1
            self.drive_at(t, self.pins["sda"], level)

    def on_rise(self, sda, c):
        if self.state in ("addr", "write"):
            if self.bit < 8:
                self.shift = (self.shift << 1) | sda
            self.bit += 1
        elif self.state == "read":
            if self.bit == 8 and sda:                          # master's NAK: last byte
                self.state = "nak"
                self.log.append((c, "nak", None))
            self.bit += 1

    def on_fall(self, c):
        if self.state in ("addr", "write"):
            if self.bit == 8:                                  # byte complete: ACK or not
                byte = self.shift & 0xFF
                if self.state == "addr":
                    self.log.append((c, "addr", byte))
                    if (byte >> 1) != self.address:
                        self.state = "idle"
                        return
                    self.rw = byte & 1
                    self.first = True
                else:
                    self.log.append((c, "write", byte))
                    if self.first:
                        self.ptr = byte
                        self.first = False
                    else:
                        self.regs[self.ptr] = byte
                        self.writes.append((self.ptr, byte))
                        self.ptr = (self.ptr + 1) & 0xFF
                self.set_sda(0, c)                             # ACK
            elif self.bit == 9:                                # ACK clock over
                self.set_sda(None, c)
                self.bit, self.shift = 0, 0
                if self.state == "addr" and self.rw:
                    self.state = "read"
                    self.load_read(c)
                elif self.state == "addr":
                    self.state = "write"
        elif self.state == "read":
            if self.bit == 8:                                  # release for the master's ACK
                self.set_sda(None, c)
            elif self.bit == 9:
                self.bit = 0
                self.load_read(c)
            elif self.bit < 8:
                self.set_sda(None if (self.out_byte >> (7 - self.bit)) & 1 else 0, c)
        elif self.state == "nak":
            self.set_sda(None, c)

    def load_read(self, c):
        self.out_byte = self.regs[self.ptr]
        self.log.append((c, "read", self.out_byte))
        self.ptr = (self.ptr + 1) & 0xFF
        self.set_sda(None if self.out_byte >> 7 & 1 else 0, c)


class Replay(Part):
    """Drives the lines in `mask` from a recorded capture: [(clocks after attach, levels)], levels
    a 32-bit word. `Replay.from_trace` records one from a session trace (the pad levels)."""
    kind = "replay"

    def __init__(self, name, mask, capture, loop=False):
        super().__init__(name)
        self.mask = mask
        self.capture = [(int(t), int(v)) for t, v in capture]
        self.loop = loop
        self.params = {"mask": mask, "capture": self.capture, "loop": loop}
        self.k = 0
        self.t0 = None

    @staticmethod
    def from_trace(name, trace, mask, start=None, end=None):
        start = trace.start if start is None else start
        end = trace.start + len(trace) if end is None else end
        cap, prev = [], None
        for c in range(start, end):
            v = trace.inp[c - trace.start] & mask
            if v != prev:
                cap.append((c - start, v))
                prev = v
        return Replay(name, mask, cap)

    def attach(self, session):
        super().attach(session)
        self.t0, self.k = session.cycle, 0

    def wake(self, cycle):
        if self.k < len(self.capture):
            return self.t0 + self.capture[self.k][0]
        return None

    def apply(self, session):
        while self.k < len(self.capture) and self.t0 + self.capture[self.k][0] <= session.cycle:
            v = self.capture[self.k][1]
            for pin in range(32):
                if self.mask >> pin & 1:
                    session.drive(pin, v >> pin & 1)
            self.k += 1
            if self.k == len(self.capture) and self.loop and self.capture:
                self.t0 += self.capture[-1][0] + 1
                self.k = 0

    def drives(self):
        return [p for p in range(32) if self.mask >> p & 1]


# ---------------------------------------------------------------------------- FIFO sources
class FifoFeeder(Part):
    """Keeps SM `sm`'s TX FIFO filled, checking every `every` clocks: words from `words` (a list,
    repeated when `repeat`) or `fn(i)` (a stateless function of the word index). With `burst`,
    `burst` words go out together and the next burst waits `gap` clocks after the previous one
    finished (FIFO empty), e.g. a WS2812 frame and its reset time."""
    kind = "fifo_feeder"

    def __init__(self, name, sm=0, words=(), fn=None, every=64, repeat=True, burst=0, gap=0, limit=None):
        super().__init__(name)
        self.sm, self.words, self.fn = sm, list(words), fn
        self.every, self.repeat, self.burst, self.gap, self.limit = every, repeat, burst, gap, limit
        self.params = {"sm": sm, "words": self.words, "every": every, "repeat": repeat, "burst": burst,
                       "gap": gap, "limit": limit}
        self.i = 0                         # words pushed
        self.next = None
        self.burst_left = 0
        self.empty_since = None

    def attach(self, session):
        super().attach(session)
        self.next = session.cycle

    def wake(self, cycle):
        return self.next

    def word(self, i):
        if self.fn is not None:
            return self.fn(i)
        if not self.words:
            return None
        if i >= len(self.words) and not self.repeat:
            return None
        return self.words[i % len(self.words)]

    def apply(self, session):
        if self.next is None or session.cycle < self.next:
            return
        if self.limit is not None and self.i >= self.limit:
            self.next = None
            return
        fstat = session.read(FSTAT)
        if self.burst:
            if self.burst_left == 0:
                # FSTAT has no TX-empty bit in this tile: the level is read through VPI (no clocks)
                if session.sm_state(self.sm)["tx_level"]:
                    self.empty_since = None            # previous burst still going out
                else:
                    if self.empty_since is None:
                        self.empty_since = session.cycle
                    if session.cycle - self.empty_since >= self.gap:
                        self.burst_left = self.burst
                        self.empty_since = None
            while self.burst_left and not (fstat >> (24 + self.sm)) & 1:
                w = self.word(self.i)
                if w is None:
                    break
                session.push(self.sm, w)
                self.i += 1
                self.burst_left -= 1
                fstat = session.read(FSTAT)
        else:
            while not (fstat >> (24 + self.sm)) & 1:
                w = self.word(self.i)
                if w is None:
                    break
                session.push(self.sm, w)
                self.i += 1
                if self.limit is not None and self.i >= self.limit:
                    break
                fstat = session.read(FSTAT)
        self.next = session.cycle + self.every


class RxCollector(Part):
    """Pulls SM `sm`'s RX FIFO every `every` clocks and keeps the words (last `keep`)."""
    kind = "rx_collector"

    def __init__(self, name, sm=0, every=64, keep=4096):
        super().__init__(name)
        self.sm, self.every, self.keep = sm, every, keep
        self.params = {"sm": sm, "every": every, "keep": keep}
        self.words = []
        self.cycles = []
        self.total = 0
        self.next = None

    def attach(self, session):
        super().attach(session)
        self.next = session.cycle

    def wake(self, cycle):
        return self.next

    def apply(self, session):
        if self.next is None or session.cycle < self.next:
            return
        while True:
            w = session.pull(self.sm)
            if w is None:
                break
            self.words.append(w)
            self.cycles.append(session.cycle)
            self.total += 1
        if len(self.words) > self.keep:
            del self.words[:-self.keep], self.cycles[:-self.keep]
        self.next = session.cycle + self.every


class UartRxHost(RxCollector):
    """RX FIFO reader for a UART receiver program that leaves each byte in bits [31:24]."""
    kind = "uart_rx_host"

    @property
    def data(self):
        return bytes((w >> 24) & 0xFF for w in self.words)

    @property
    def text(self):
        return self.data.decode("latin-1")


# ---------------------------------------------------------------------------- host scripts
# A host script is what the CPU on the other side of the FIFOs does (the pico-examples C code, a
# flight controller). It acts at its own wake cycles only, so a run gives the same register
# accesses at the same cycles however it is batched.
class DShotHost(Part):
    """Flight-controller side of `dshot_bidir.pio`: one frame (`throttle`, inverted CRC) every
    `period_clocks`, and the two capture words of each reply decoded with `pio2.dshot_ref`
    (`samples_per_bit` = reply bit time / sample time)."""
    kind = "dshot_host"

    def __init__(self, name, sm=0, throttle=1046, period_clocks=20_000, samples_per_bit=2.56, poles=14):
        super().__init__(name)
        self.sm, self.throttle, self.period, self.spb, self.poles = sm, throttle, period_clocks, samples_per_bit, poles
        self.params = {"sm": sm, "throttle": throttle, "period_clocks": period_clocks,
                       "samples_per_bit": samples_per_bit, "poles": poles}
        self.next = None
        self.frames = 0
        self.words = []
        self.replies = []                  # decoded eee:mmmmmmmmm values (None: no / bad reply)
        self.rpm = 0.0

    def attach(self, session):
        super().attach(session)
        self.next = session.cycle

    def wake(self, cycle):
        return self.next

    def set_throttle(self, session, value):
        self.throttle = value
        self.params["throttle"] = value

    def apply(self, session):
        if self.next is None or session.cycle < self.next:
            return
        while True:
            w = session.pull(self.sm)
            if w is None:
                break
            self.words.append(w)
            if len(self.words) == 2:
                if self.words == [0, 0]:
                    v = None
                else:
                    v = dshot_ref.decode_samples(dshot_ref.samples_from_words(self.words), self.spb)
                self.replies.append(v)
                if len(self.replies) > 1000:
                    del self.replies[:-1000]
                if v is not None:
                    p = dshot_ref.erpm_period_us(v)
                    self.rpm = 0.0 if p >= 65408 else 60e6 / p / (self.poles / 2)
                self.words = []
        if not (session.read(FSTAT) >> (24 + self.sm)) & 1:
            session.push(self.sm, dshot_ref.dshot_frame(self.throttle, telemetry=False, bidir=True))
            self.frames += 1
        self.next += self.period


class EscSerialHost(Part):
    """Host side of `esc_serial.pio` (4-way passthrough): sends each message (hex string), then
    collects the ESC's answer until the line has been quiet for `quiet_clocks`, then the next
    message `gap_clocks` later. Bytes arrive in RX bits [31:24]."""
    kind = "esc_serial_host"

    def __init__(self, name, sm=0, messages=(), poll_clocks=2000, quiet_clocks=60_000, gap_clocks=20_000):
        super().__init__(name)
        self.sm, self.messages = sm, list(messages)
        self.poll, self.quiet, self.gap = poll_clocks, quiet_clocks, gap_clocks
        self.params = {"sm": sm, "messages": self.messages, "poll_clocks": poll_clocks,
                       "quiet_clocks": quiet_clocks, "gap_clocks": gap_clocks}
        self.k = 0
        self.pending = bytearray()
        self.phase = "send"
        self.reply = bytearray()
        self.last_rx = None
        self.t_sent = None
        self.exchanges = []                # (request hex, reply hex)
        self.next = None

    def attach(self, session):
        super().attach(session)
        self.next = session.cycle
        self.start_message()

    def start_message(self):
        if self.k < len(self.messages):
            self.pending = bytearray.fromhex(self.messages[self.k])
            self.phase = "send"
        else:
            self.phase = "done"

    def wake(self, cycle):
        return self.next

    def apply(self, session):
        if self.next is None or session.cycle < self.next:
            return
        nxt = self.next + self.poll
        if self.phase == "send":
            while self.pending and not (session.read(FSTAT) >> (24 + self.sm)) & 1:
                session.push(self.sm, self.pending.pop(0))
            if not self.pending:
                self.phase, self.reply, self.t_sent = "wait", bytearray(), session.cycle
        elif self.phase == "wait":
            while True:
                w = session.pull(self.sm)
                if w is None:
                    break
                self.reply.append(w >> 24 & 0xFF)
                self.last_rx = session.cycle
            t_last = self.last_rx if self.reply else self.t_sent
            if session.cycle - t_last >= self.quiet:
                self.exchanges.append((self.messages[self.k], bytes(self.reply).hex()))
                self.k += 1
                self.start_message()
                nxt = self.next + self.gap
        self.next = None if self.phase == "done" else max(nxt, session.cycle)


class ImuHost(Part):
    """Host side of the pico-examples `spi_cpha0_cs` program reading an SPI IMU: reads WHO_AM_I,
    then on each DRDY reads `count` data bytes from `data_reg` as transactions of at most 3 data
    bytes (a whole transaction fits the 4-word FIFOs, so the host never touches the bus while SCK
    runs) and decodes big-endian int16 values. Every check happens at its own poll cycles; the RX
    level is read through VPI (no clocks), as a CPU polls FSTAT."""
    kind = "imu_host"

    def __init__(self, name, sm=0, drdy=None, data_reg=0x1F, count=12, poll_clocks=32, who_am_i_reg=0x75):
        super().__init__(name)
        self.sm, self.drdy_pin, self.data_reg, self.count = sm, drdy, data_reg, count
        self.poll, self.who_reg = poll_clocks, who_am_i_reg
        self.params = {"sm": sm, "drdy": drdy, "data_reg": data_reg, "count": count,
                       "poll_clocks": poll_clocks, "who_am_i_reg": who_am_i_reg}
        self.txns = []                     # queue of (register, n)
        self.cur = None
        self.sample = bytearray()
        self.samples = []                  # decoded value lists
        self.sample_cycles = []
        self.who_am_i = None
        self.next = None
        self.state = "who"

    def attach(self, session):
        super().attach(session)
        self.next = session.cycle
        self.txns = [(self.who_reg, 1)]

    def wake(self, cycle):
        return self.next

    def apply(self, session):
        if self.next is None or session.cycle < self.next:
            return
        if self.cur is None:
            if not self.txns and self.state == "idle" and self.drdy_pin is not None:
                if session.pins()[2] >> self.drdy_pin & 1:
                    self.txns = [(self.data_reg + k, min(3, self.count - k)) for k in range(0, self.count, 3)]
                    self.sample = bytearray()
                    self.state = "sample"
            if self.txns:
                reg, n = self.cur = self.txns.pop(0)
                session.push(self.sm, (0x80 | reg) << 24)
                for _ in range(n):
                    session.push(self.sm, 0)
        elif session.sm_state(self.sm)["rx_level"] >= self.cur[1] + 1:
            words = [session.pull(self.sm) for _ in range(self.cur[1] + 1)]
            data = bytes(w & 0xFF for w in words[1:])
            if self.state == "who":
                self.who_am_i = data[0]
                self.state = "idle"
            else:
                self.sample += data
                if not self.txns:
                    vals = []
                    for a in range(0, len(self.sample) - 1, 2):
                        v = self.sample[a] << 8 | self.sample[a + 1]
                        vals.append(v - 0x10000 if v & 0x8000 else v)
                    self.samples.append(vals)
                    self.sample_cycles.append(session.cycle)
                    if len(self.samples) > 1000:
                        del self.samples[:-1000], self.sample_cycles[:-1000]
                    self.state = "idle"
            self.cur = None
        self.next += self.poll


# pico-examples pio_i2c.c encoding of TX words (16 bits, replicated as a halfword write)
I2C_ICOUNT_LSB, I2C_FINAL_LSB, I2C_DATA_LSB, I2C_NAK_LSB = 10, 9, 1, 0
SC0_SD0, SC0_SD1, SC1_SD0, SC1_SD1 = 0xF780, 0xF781, 0xFF80, 0xFF81    # set_scl_sda image


def i2c_half(v):
    v &= 0xFFFF
    return v << 16 | v


class I2cHost(Part):
    """Host side of the pico-examples `i2c` program (pio_i2c.c): every `period_clocks` reads
    `count` bytes from register `reg` of the device at `address`: a write of the register (start,
    address, register, stop) then a read (start, address + R, data with ACK and a final NAK, stop),
    RX off during the write, as pio_i2c_write_blocking / pio_i2c_read_blocking do. A NAK (the SM
    raises its IRQ flag) is counted in `errors` and the SM is resumed as
    pio_i2c_resume_after_error does."""
    kind = "i2c_host"

    def __init__(self, name, sm=0, address=0x48, reg=0, count=2, period_clocks=200_000, poll_clocks=256,
                 entry_point=12):
        super().__init__(name)
        self.sm, self.address, self.reg, self.count = sm, address, reg, count
        self.period, self.poll, self.entry = period_clocks, poll_clocks, entry_point
        self.params = {"sm": sm, "address": address, "reg": reg, "count": count,
                       "period_clocks": period_clocks, "poll_clocks": poll_clocks, "entry_point": entry_point}
        self.ops = []
        self.results = []                  # bytes read per transaction
        self.errors = 0
        self.next = None
        self.t_start = None
        self.cur = None

    def attach(self, session):
        super().attach(session)
        self.next = session.cycle
        self.t_start = session.cycle

    def wake(self, cycle):
        return self.next

    def program(self):
        a = self.address
        start = [1 << I2C_ICOUNT_LSB, SC1_SD0, SC0_SD0]
        stop = [2 << I2C_ICOUNT_LSB, SC0_SD0, SC1_SD0, SC1_SD1]
        return [("rx", False), ("put", list(start)), ("put", [(a << 2) | 1]),
                ("put", [(self.reg << I2C_DATA_LSB) | (1 << I2C_FINAL_LSB) | 1]), ("put", list(stop)), ("idle",),
                ("rx", True), ("put", list(start)), ("put", [(a << 2) | 3]), ("read", self.count),
                ("put", list(stop)), ("idle",), ("done",)]

    def apply(self, session):
        if self.next is None or session.cycle < self.next:
            return
        s, sm = session, self.sm
        shiftctrl = 0x108 + 0x20 * sm
        if not self.ops and session.cycle >= self.t_start:
            self.ops = self.program()
            self.cur = {"got": bytearray(), "tx": 0, "first": True}
        if self.ops and s.irq_flags() >> sm & 1:           # NAK: pio_i2c_resume_after_error
            self.errors += 1
            sh = s.read(shiftctrl)
            s.write(shiftctrl, sh ^ (1 << 30))             # an FJOIN change flushes the FIFOs
            s.write(shiftctrl, sh)
            s.exec(sm, self.entry)                         # jmp entry_point
            s.write(0x04, 1 << sm)                         # clear the IRQ flag
            self.ops = [("put", [2 << I2C_ICOUNT_LSB, SC0_SD0, SC1_SD0, SC1_SD1]), ("idle",), ("done",)]
            self.cur["got"] = None
        while self.ops:
            op = self.ops[0]
            if op[0] == "rx":
                sh = s.read(shiftctrl)
                s.write(shiftctrl, (sh | 1 << 16) if op[1] else (sh & ~(1 << 16)))
                if op[1]:
                    while s.pull(sm) is not None:
                        pass
                self.ops.pop(0)
            elif op[0] == "put":
                words = op[1]
                while words and not (s.read(FSTAT) >> (24 + sm)) & 1:
                    s.push(sm, i2c_half(words.pop(0)))
                if words:
                    break
                self.ops.pop(0)
            elif op[0] == "read":
                n = op[1]
                while self.cur["tx"] < n and not (s.read(FSTAT) >> (24 + sm)) & 1:
                    self.cur["tx"] += 1
                    last = self.cur["tx"] == n
                    s.push(sm, i2c_half((0xFF << 1) | ((1 << I2C_FINAL_LSB) | (1 << I2C_NAK_LSB) if last else 0)))
                while True:
                    w = s.pull(sm)
                    if w is None:
                        break
                    if self.cur["first"]:
                        self.cur["first"] = False          # the address byte comes back too
                    else:
                        self.cur["got"].append(w & 0xFF)
                if len(self.cur["got"]) < n:
                    break
                self.ops.pop(0)
            elif op[0] == "idle":
                st = s.sm_state(sm)                        # TX stall at the entry point (VPI)
                if st["tx_level"] or not st["stalled"]:
                    break
                self.ops.pop(0)
            elif op[0] == "done":
                if self.cur["got"] is not None:
                    self.results.append(bytes(self.cur["got"]))
                self.ops = []
                self.t_start += self.period
                break
        self.next += self.poll
