# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""Stimulus, host scripts and device parts checked against each other, end to end on the Verilator
model ([SPEC-DBG-07], [SPEC-DBG-11]); watches of every kind and reverse step across a part action
([SPEC-DBG-04], [SPEC-DBG-06]).

Each protocol test runs an example session and compares what the host script received with what
the device model sent (or the reverse), so a wrong program configuration, host script or device
model shows up as a mismatch."""
# @verifies [SPEC-DBG-04] [SPEC-DBG-06] [SPEC-DBG-07] [SPEC-DBG-11] docs/DEBUGGER_SPECIFICATION.md

import pytest

from pio2.asm import assemble
from pio2.debug.bench import Bench, Watch
from pio2.debug.examples import Example, rainbow_word
from pio2.debug.session import Session


@pytest.fixture(scope="module")
def b():
    s = Session()
    bench = Bench(session=s)
    yield bench
    s.close()


def of_kind(b, kind):
    return next(p for p in b.parts if p.kind == kind)


def signed(w):
    return w - (1 << 32) if w & 0x8000_0000 else w


# ---------------------------------------------------------------------------- protocols
def test_uart_rx_receives_what_the_source_sent(b):
    b.load("UART RX")
    b.run(300_000)
    got = b.part("getc").data
    sent = bytes(of_kind(b, "uart_tx").params["data"])
    assert len(got) >= 20
    assert got == sent[:len(got)]


def test_dshot_frames_and_telemetry_agree_with_the_esc(b):
    b.load("DShot")
    b.run(600_000)
    esc, fc = of_kind(b, "esc"), b.part("fc")
    assert esc.frames >= 25 and esc.bad_crc == 0
    assert esc.throttle == 1046
    assert fc.replies and None not in fc.replies          # every reply decoded
    assert fc.rpm == pytest.approx(esc.telemetry_rpm, rel=0.01)


def test_esc_serial_exchanges_follow_the_bootloader_script(b):
    """BLHeli init and a second command at 19200 baud, 27 MHz: each answer is the scripted one.
    Before the fix the host's quiet timer started when the last byte was queued, not sent, and
    recorded an empty answer."""
    b.load("ESC serial")
    b.run(700_000)
    assert b.part("host").exchanges == [
        ("0000000000000000000d424c48656c69f47d", "343731e8b2060430"), ("fd00", "30")]


def test_quadrature_count_follows_the_encoder(b):
    """The SM's count (Y) equals the steps made (negative: the program counts this Gray order
    down); the host's get_count reading lags at most its poll interval. Before the fix the example
    left OUT shifting left (SDK default is right) and Y jumped by dozens per step."""
    b.load("Quadrature encoder")
    b.run(100_000)
    enc, host = b.part("Encoder"), b.part("count")
    assert enc.position > 300
    assert signed(b.s.sm_state(0)["reg_y"]) == -enc.position
    steps_per_poll = host.every // enc.step_clocks + 1
    assert -enc.position <= host.count <= -enc.position + steps_per_poll


def test_spi_imu_samples_match_the_imu(b):
    b.load("SPI IMU")
    b.run(200_000)
    imu, host = of_kind(b, "imu"), b.part("host")
    assert host.who_am_i == imu.params["who_am_i"]
    assert len(host.samples) >= 5
    for s in host.samples:
        assert s in imu.values
    assert imu.late == 0, f"{imu.late} MISO changes came too late"


def test_i2c_reads_the_sensor_value(b):
    """Every read returns the sensor's value at that time (it counts up 0x10 per odr_clocks).
    Before the fix every byte came back one bit out of step (0x2232 for 0x1900)."""
    b.load("I2C sensor")
    b.run(700_000)
    sensor, host = of_kind(b, "i2c_sensor"), b.part("host")
    assert host.errors == 0 and len(host.results) >= 4
    vals = [r[0] << 8 | r[1] for r in host.results]
    assert all((v - 0x1900) % 0x10 == 0 and 0x1900 <= v <= sensor.value for v in vals)
    assert vals == sorted(vals) and len(set(vals)) == len(vals)


def test_ws2812_strip_shows_the_last_frame_sent(b):
    b.load("WS2812")
    b.run(200_000)
    strip = of_kind(b, "ws2812")
    assert strip.frames >= 3
    f = strip.frames - 1

    def rgb(w):
        return (w >> 16 & 255, w >> 24 & 255, w >> 8 & 255)

    assert strip.colors == [rgb(rainbow_word(8 * f + j)) for j in range(8)]


# ---------------------------------------------------------------------------- several programs at once
def test_fc_board_imu_auto_dma_is_exact_while_three_other_programs_run(b):
    """Four programs in one tile (29 of 32 words, at offsets 0 / 13 / 17 / 21). The DRDY-triggered DMA
    ring holds exactly the IMU's samples, in order, none lost or extra, and the host made no
    register access for them ([SPEC-PIO-06]); the strip, console and status LED run alongside.
    Before the fixes: a junk first burst (DRDY floating high before the IMU ran) and bit errors
    (the IMU model froze during the other host scripts' register accesses: 414 late MISO changes)."""
    b.load("FC board (IMU auto-DMA)")
    b.run(400_000)
    imu, ring = of_kind(b, "imu"), b.part("imu_ring")
    assert [b.s.sm_state(sm)["enabled"] for sm in range(4)] == [True] * 4
    n = len(ring.samples)
    assert n >= 20 and ring.samples == imu.values[:n]
    assert imu.late == 0 and ring.register_accesses == 0
    assert of_kind(b, "ws2812").frames >= 3
    assert of_kind(b, "terminal").text.startswith("pio2 fc: imu ok, leds ok\r\n")
    assert of_kind(b, "led").toggles >= 4


def test_four_motors_share_one_program_and_each_follows_its_throttle(b):
    b.load("Four motors")
    b.run(800_000)
    rpms = []
    for i, throttle in enumerate((300, 800, 1200, 1800)):
        esc, fc = b.part(f"ESC{i + 1}"), b.part(f"fc{i + 1}")
        assert esc.throttle == throttle and esc.bad_crc == 0 and esc.frames >= 35
        assert None not in fc.replies
        assert fc.rpm == pytest.approx(esc.telemetry_rpm, rel=0.01)
        rpms.append(fc.rpm)
    assert rpms == sorted(rpms) and rpms[0] > 0


# ---------------------------------------------------------------------------- watches
def stopped_on_change(b, stop, now_true, before_true):
    """A watch stops on the clock its condition becomes true: true now, false one clock earlier."""
    assert stop is not None, "watch never fired"
    assert b.s.cycle == stop.cycle
    assert now_true()
    b.back(1)
    assert not before_true()


def test_pin_watch_stops_on_the_rising_edge(b):
    b.load("Blink")
    b.watches = [Watch.pin(25, "rise")]
    stop = b.run(50_000)
    led = lambda: (b.s.pins()[0] >> 25) & 1 == 1  # noqa: E731
    stopped_on_change(b, stop, led, led)
    b.watches = []


def stall_example(s):
    """SM0 counts down for ~260 clocks, then stalls on a pull that never gets data."""
    prog = assemble(".program t\nset x, 31\nloop:\njmp x-- loop [7]\npull block\n")["programs"][0]
    s.load(prog, sm=0)
    return Example(title="Stall", about="", parts=[], probes=[])


def test_stall_watch_stops_when_the_sm_starts_stalling(b):
    b.load(stall_example)
    b.watches = [Watch.stall(0)]
    stop = b.run(10_000)
    stalled = lambda: bool(b.s.sm_state(0)["stalled"])  # noqa: E731
    stopped_on_change(b, stop, stalled, stalled)
    b.watches = []


def test_stop_inside_a_register_access_is_reported_where_the_run_ended(b):
    """SPI IMU's host polls with register accesses (a few clocks each); a stall that begins inside
    one is reported at the end of the access, and the reported cycle is where the bench is.
    Before the fix the bench ended up to an access earlier than the cycle it reported."""
    b.load("SPI IMU")
    b.run(5_000)
    b.watches = [Watch.stall(0)]
    for _ in range(5):
        stop = b.run(100_000)
        assert stop is not None and b.s.cycle == stop.cycle
        assert b.s.sm_state(0)["stalled"]
        while b.s.sm_state(0)["stalled"]:
            b.watches[0].enabled = False
            b.step_clock()
            b.watches[0].enabled = True
    b.watches = []


def test_register_watch_stops_when_x_takes_the_value(b):
    b.load("Blink")
    b.watches = [Watch.reg(0, "x", 100)]
    stop = b.run(50_000)
    x_is_100 = lambda: b.s.sm_state(0)["reg_x"] == 100  # noqa: E731
    stopped_on_change(b, stop, x_is_100, x_is_100)
    b.watches = []


def test_fifo_watch_stops_when_the_tx_fifo_fills(b):
    b.load("UART TX")
    b.watches = [Watch.fifo(0, "tx", ">=", 4)]
    stop = b.run(50_000)
    full = lambda: b.s.sm_state(0)["tx_level"] >= 4  # noqa: E731
    stopped_on_change(b, stop, full, full)
    b.watches = []


def irq_example(s):
    """SM0 sets IRQ flag 0 and clears it three clocks later, forever."""
    prog = assemble(".program t\n.wrap_target\nirq set 0\nirq clear 0 [2]\nnop [20]\n.wrap")["programs"][0]
    s.load(prog, sm=0)
    return Example(title="IRQ toggle", about="", parts=[], probes=[])


def test_irq_watch_stops_when_the_flag_is_set(b):
    b.load(irq_example)
    b.run(10)
    b.watches = [Watch.irq(0, "set")]
    stop = b.run(1_000)
    flag = lambda: b.s.irq_flags() & 1 == 1  # noqa: E731
    stopped_on_change(b, stop, flag, flag)
    b.watches = []


# ---------------------------------------------------------------------------- reverse step
def test_back_across_a_button_press_replays_it(b):
    """Press the button (a logged part action), step back to before the press: the button is up and
    the LED off; run forward again: the press is replayed at the same clock and the LED comes on."""
    b.load("Button to LED")
    b.run(200)
    t_before = b.s.cycle
    b.part_call("Button", "set", True)
    t_press = b.s.cycle
    b.run(300)
    led_on = lambda: (b.s.pins()[0] >> 15) & 1 == 1  # noqa: E731
    assert led_on()
    t_end = b.s.cycle
    b.back_to(t_before)
    assert not b.part("Button").pressed and not led_on()
    b.run_to(t_end)
    assert b.part("Button").pressed and led_on()
    assert t_press <= t_end
