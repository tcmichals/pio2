# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""
pio2_core through its boundary ([SPEC-ICN-11]): AXI4-Lite slave, AXI-Stream in / out with the
channel in TDEST, interrupt, pins.

* Registers in every window ([SPEC-AXI-03]): ID / VERSION, DMA_CAPS, tile, trace, core; DECERR
  outside the map; SLVERR for a partial write; back-to-back writes through the crossbar.
* Input words for no enabled IN channel are dropped and counted, and never block the stream.
* Interrupt (pio2_core_regs: ID, ISR, IMR, STREAM_PEND, DMA_DONE, IN_DROPPED): PIO IRQ, DMA done and
  stream threshold set ISR; IMR masks; W1C of a cause that is still true sets it again.
* The three [SPEC-DMA-02] examples end to end at 27 MHz: bidirectional DShot frames in on a RING
  channel with the ESC's eRPM reply out; ESC serial bytes both ways; IMU auto-DMA: DRDY triggers
  the SPI burst program ([SPEC-PIO-06]), a TRIGGERED channel moves each sample block, the host is
  interrupted at the threshold and collects the batch.
"""
# cocotb-toplevel: pio2_core
# @verifies [SPEC-ICN-11] [SPEC-AXI-03] [SPEC-DMA-02] [SPEC-LINK-05] docs/INTERCONNECT_SPECIFICATION.md
# @verifies [SPEC-PIO-06] docs/SPECIFICATION.md

import cocotb
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge
from cocotb.utils import get_sim_time

from pio2_core_tb import ECHO, Board, Core
from pio2 import dshot_ref
from pio2.asm import assemble
from pio2.programs import load_program
from pio2.regs import (CORE_ID, CORE_VERSION, DMA_CAPS, DMA_CFG, DMA_CH, DMA_COUNT, DMA_DONE,
                       DMA_FLUSH, DMA_THR, IMR, IN, IN_DROPPED, INSTR, ISR, ISR_DMA_DONE, ISR_PIO_IRQ,
                       ISR_STREAM, ONE_SHOT, OUT, PINCTRL, REG_IRQ, RING, SM, STREAM_PEND, TRACE_MASK,
                       TRIGGERED, TXF, ID_VALUE, VERSION_VALUE, dma_cfg)

FABRIC_HZ = 27_000_000


@cocotb.test(timeout_time=50, timeout_unit="ms")
async def test_registers_in_every_window(dut):
    core = await Core.start(dut)
    assert await core.read(CORE_ID) == ID_VALUE
    assert await core.read(CORE_VERSION) == VERSION_VALUE
    assert await core.read(DMA_CAPS) == (28 << 16) | 256
    for addr, val in [(SM(2, PINCTRL), 0x1234_5678), (TRACE_MASK, 0x0000_0F0F), (IMR, 0x1FC),
                      (DMA_CH(9, DMA_THR), 0x55AA), (DMA_CH(1, DMA_FLUSH), 0x00AB_CDEF)]:
        await core.write(addr, val)
        got = await core.read(addr)
        assert got == val & (0x1FF if addr == IMR else 0xFFFF_FFFF), f"0x{addr:04X}: {got:#x}"
    for addr in (0x4000, 0x8000, 0x8000_0000, 0xFFFF_FFFC):
        await core.read(addr, expect=3)                         # DECERR
        await core.write(addr, 1, expect=3)
    r = await core.axil.write(DMA_CH(2, DMA_COUNT), b"\x05\x00")  # WSTRB 0x3
    assert r.resp == 2, f"partial write answered {r.resp}, expected SLVERR"
    assert await core.read(DMA_CH(2, DMA_COUNT)) == 1, "partial write changed the register"
    # Back to back through the crossbar to pio2_axil_regs (the DMA window): 64 writes, then 64 reads
    # in flight. (The tile's own front end takes one request at a time by design, [SPEC-AXI-04].)
    t0 = get_sim_time("ns")
    ws = [cocotb.start_soon(core.write(DMA_CH(2 + n % 8, DMA_THR), n)) for n in range(64)]
    for w in ws:
        await w
    rs = [cocotb.start_soon(core.read(DMA_CH(2 + n % 8, DMA_THR))) for n in range(64)]
    vals = [await r for r in rs]
    assert vals == list(range(56, 64)) * 8, "a pipelined write or read went to the wrong register"
    clocks = (get_sim_time("ns") - t0) / 10
    dut._log.info(f"128 register transactions in {clocks:.0f} clocks")
    # Forencich's axil_crossbar decodes one address per direction at a time (about 4 clocks per
    # transaction here); the slave behind it takes one per clock (test_pio2_axil_regs_cocotb).
    assert clocks < 128 * 4.5, f"{clocks} clocks for 128 transactions: slower than the crossbar's pace"


@cocotb.test(timeout_time=50, timeout_unit="ms")
async def test_input_for_no_in_channel_is_dropped_and_counted(dut):
    core = await Core.start(dut)
    await core.load(ECHO, sm=0)
    await core.write(DMA_CH(3, DMA_FLUSH), 4)
    await core.write(DMA_CH(3, DMA_CFG), dma_cfg(direction=OUT, mode=RING, sm=0))
    await core.write(DMA_CH(2, DMA_CFG), dma_cfg(direction=IN, mode=RING, sm=0))
    for chan, n in [(0, 3), (1, 2), (10, 4), (15, 1), (4, 5), (3, 6)]:   # 4: off, 3: an OUT channel
        await core.send(chan, list(range(n)))
    await core.send(2, [7, 8, 9])
    await core.wait_for(lambda: core.words(3) == [7, 8, 9], what="words after the dropped ones")
    assert await core.read(IN_DROPPED) == 3 + 2 + 4 + 1 + 5 + 6
    await core.write(IN_DROPPED, 0)
    assert await core.read(IN_DROPPED) == 0


@cocotb.test(timeout_time=50, timeout_unit="ms")
async def test_interrupt_causes_mask_and_w1c(dut):
    core = await Core.start(dut)
    assert int(dut.o_irq.value) == 0
    await core.load(".program i\nirq set 0\nhalt:\njmp halt\n", sm=0)
    await ClockCycles(dut.clk, 10)
    assert await core.read(ISR) & ISR_PIO_IRQ
    assert int(dut.o_irq.value) == 0, "masked cause raised o_irq"
    await core.write(IMR, ISR_PIO_IRQ)
    await ClockCycles(dut.clk, 2)
    assert int(dut.o_irq.value) == 1
    await core.write(ISR, ISR_PIO_IRQ)                        # W1C while the flag is still set
    assert await core.read(ISR) & ISR_PIO_IRQ, "a still-true cause was lost by W1C"
    await core.write(REG_IRQ, 1)                              # clear the PIO flag, then the bit
    await core.write(ISR, ISR_PIO_IRQ)
    await ClockCycles(dut.clk, 2)
    assert not await core.read(ISR) & ISR_PIO_IRQ and int(dut.o_irq.value) == 0

    # DMA done: a ONE_SHOT of 3 words
    await core.load(".program c\nin x, 32\npush block\n", sm=1, offset=4)
    await core.write(DMA_CH(4, DMA_COUNT), 3)
    await core.write(IMR, ISR_DMA_DONE)
    await core.write(DMA_CH(4, DMA_CFG), dma_cfg(direction=OUT, mode=ONE_SHOT, sm=1))
    await core.wait_for(lambda: int(dut.o_irq.value) == 1, what="DMA done interrupt")
    assert await core.read(DMA_DONE) == 1 << 3
    await core.write(DMA_DONE, 1 << 3)
    await core.write(ISR, ISR_DMA_DONE)
    await ClockCycles(dut.clk, 2)
    assert int(dut.o_irq.value) == 0

    # Stream threshold: the ONE_SHOT's 3 words wait in ring 4 (FLUSH 0); THR 3 -> pending
    core.sink.pause = True
    await core.write(DMA_CH(4, DMA_THR), 3)
    await core.write(IMR, ISR_STREAM)
    await ClockCycles(dut.clk, 3)
    assert await core.read(STREAM_PEND) == 1 << 3
    assert int(dut.o_irq.value) == 1


async def start_board(dut, pin):
    core = await Core.start(dut, gpio=0xFFFF_FFFF, hz=FABRIC_HZ)
    board = Board(dut, pin=pin)
    cocotb.start_soon(board.run())
    return core, board


@cocotb.test(timeout_time=20, timeout_unit="ms")
async def test_dshot_frames_in_on_a_ring_with_replies_out(dut):
    from test_asp_pio_dshot_cocotb import REPLY_BIT_NS, SAMPLE_NS, SM_HZ, capture_frame, esc_reply
    pin = 0
    core, board = await start_board(dut, pin)
    clk_ns = 1e9 / FABRIC_HZ
    await core.load(load_program("dshot_bidir"), sm=0, clkdiv=FABRIC_HZ / SM_HZ, execctrl=pin << 24,
                    shiftctrl=1 << 16, pinctrl=(pin << 15) | (1 << 26) | (pin << 5) | (1 << 20) | pin)
    await core.write(DMA_CH(3, DMA_FLUSH), 100)
    await core.write(DMA_CH(3, DMA_CFG), dma_cfg(direction=OUT, mode=RING, sm=0))
    await core.write(DMA_CH(2, DMA_CFG), dma_cfg(direction=IN, mode=RING, sm=0))
    cases = [(1046, 2000), (48, 40000), (2047, 150)]
    frames = [dshot_ref.dshot_frame(v, telemetry=False, bidir=True) for v, _ in cases]
    await core.send(2, frames)                                 # all three at once: the ring paces them
    for (value, period_us), frame in zip(cases, frames):
        bits = await capture_frame(dut, board, clk_ns)
        assert bits == frame, f"frame 0x{bits:04X} != 0x{frame:04X}"
        await esc_reply(board, dshot_ref.erpm_payload(period_us))
    await core.wait_for(lambda: len(core.words(3)) >= 6, limit=200_000, what="six reply words")
    words = core.words(3)
    for k, (_, period_us) in enumerate(cases):
        got = dshot_ref.decode_samples(dshot_ref.samples_from_words(words[2 * k:2 * k + 2]),
                                       REPLY_BIT_NS / SAMPLE_NS)
        assert got == dshot_ref.erpm_payload(period_us) >> 4, f"reply {k}: {got}"


@cocotb.test(timeout_time=60, timeout_unit="ms")
async def test_esc_serial_bytes_both_ways(dut):
    from test_asp_pio_esc_serial_cocotb import BAUD, BIT_NS, esc_send, uart_rx_line
    pin = 2
    core, board = await start_board(dut, pin)
    await core.load(load_program("esc_serial"), sm=0, clkdiv=FABRIC_HZ / (BAUD * 32),
                    execctrl=pin << 24, shiftctrl=(1 << 18) | (1 << 19),
                    pinctrl=(pin << 15) | (1 << 26) | (pin << 5) | (1 << 20) | pin)
    await core.write(DMA_CH(3, DMA_FLUSH), int(2 * BIT_NS * 10 * FABRIC_HZ / 1e9))   # 2 byte times
    await core.write(DMA_CH(3, DMA_CFG), dma_cfg(direction=OUT, mode=RING, sm=0))
    await core.write(DMA_CH(2, DMA_CFG), dma_cfg(direction=IN, mode=RING, sm=0))
    sent = [0x2F, 0x00, 0x5A, 0xFF]
    await core.send(2, sent)
    got, _ = await uart_rx_line(dut, board, len(sent))
    assert got == sent, f"line carried {got}"
    await ClockCycles(dut.clk, int(3 * BIT_NS * FABRIC_HZ / 1e9))   # guard bit, line released
    answer = [0x31, 0x32, 0x00, 0xC4]
    await esc_send(board, answer)
    await core.wait_for(lambda: len(core.words(3)) >= len(answer), limit=200_000, what="ESC answer")
    assert [w >> 24 for w in core.words(3)] == answer, "nothing sent may be echoed back"


class Imu:
    """SPI mode 0 IMU with DRDY: DRDY rises every `period` clocks and falls when CS goes low; a burst
    reads 16 bytes: the command byte echoed, then 15 bytes of sample k."""

    MOSI, SCK, CS, MISO, DRDY = 0, 1, 2, 3, 4

    def __init__(self, dut, board, period):
        self.dut, self.board, self.period = dut, board, period
        self.samples = 0
        self.commands = []
        board.ext[self.MISO] = 0         # DRDY low before the program runs: no burst without a sample
        board.ext[self.DRDY] = 0

    @staticmethod
    def image(k, cmd):
        return [cmd] + [(k * 7 + i) & 0xFF for i in range(1, 16)]

    async def run(self, n):
        board = self.board
        cs_q, sck_q, clock = 1, 0, 0
        bits, cmd, data = 0, 0, []
        while True:
            await RisingEdge(self.dut.clk)
            clock += 1
            out = int(self.dut.o_gpio_out.value)
            cs, sck, mosi = (out >> self.CS) & 1, (out >> self.SCK) & 1, (out >> self.MOSI) & 1
            if clock % self.period == 0 and self.samples < n:
                board.ext[self.DRDY] = 1
            if cs_q and not cs:                               # burst starts
                board.ext[self.DRDY] = 0
                bits, cmd, data = 0, 0, []
            if not cs and sck and not sck_q:                  # rising edge: IMU samples MOSI
                if bits < 8:
                    cmd = (cmd << 1) | mosi
                bits += 1
                if bits == 8:
                    self.commands.append(cmd)
                    stream = self.image(self.samples, cmd)
                    data = [(b >> (7 - i)) & 1 for b in stream for i in range(8)]
                    self.samples += 1
            if not cs and not sck and sck_q and bits >= 8 and data:   # falling edge: next bit out
                board.ext[self.MISO] = data.pop(0)
            cs_q, sck_q = cs, sck


@cocotb.test(timeout_time=100, timeout_unit="ms")
async def test_imu_auto_dma_triggered_by_drdy(dut):
    n, period = 20, FABRIC_HZ // 8000                  # 8 kHz samples at 27 MHz: 3375 clocks
    core, board = await start_board(dut, pin=0)
    await core.trace_on(mask=(1 << 0x0C) | (1 << 0x09))
    imu = Imu(dut, board, period)
    p = load_program("imu_spi_burst")
    pinctrl = (Imu.SCK << 10) | (Imu.MISO << 15) | (1 << 20) | (1 << 26) | (1 << 29) | (Imu.CS << 5) | Imu.MOSI
    await core.load(p, sm=0, shiftctrl=1 << 16, pinctrl=pinctrl, enable=False)
    # Pin directions and idle levels: MOSI, SCK, CS outputs, CS high (SET base 0, count 3 for now)
    enc = lambda text: assemble(f".program x\n{text}\n")["programs"][0]["instructions"][0]  # noqa: E731
    await core.write(SM(0, PINCTRL), (3 << 26) | (0 << 5))
    await core.write(SM(0, INSTR), enc("set pindirs, 7"))
    await core.write(SM(0, INSTR), enc("set pins, 4"))
    await core.write(SM(0, PINCTRL), pinctrl)
    cmd, nbits = 0x9F, 128
    await core.write(SM(0, TXF), (cmd << 24) | (nbits - 1))
    await core.write(SM(0, INSTR), enc("pull block"))
    await core.write(SM(0, INSTR), enc("mov y, osr"))
    # TRIGGERED OUT channel on the DRDY pin, 4 words per sample; threshold 16 words -> interrupt
    await core.write(DMA_CH(5, DMA_COUNT), nbits // 32)
    await core.write(DMA_CH(5, DMA_FLUSH), 20_000)
    await core.write(DMA_CH(5, DMA_THR), 16)
    await core.write(IMR, ISR_STREAM)
    await core.write(DMA_CH(5, DMA_CFG), dma_cfg(direction=OUT, mode=TRIGGERED, sm=0, trig_pin=True,
                                                 trig_idx=Imu.DRDY))
    core.sink.pause = True                             # the host sleeps until the interrupt
    await core.write(0x00, 1)                          # enable SM0 (CTRL)
    cocotb.start_soon(imu.run(n))
    await core.wait_for(lambda: int(dut.o_irq.value) == 1, limit=10 * period, what="threshold irq")
    t_irq = get_sim_time("ns")
    core.sink.pause = False                            # collect the batch
    try:
        await core.wait_for(lambda: len(core.words(5)) >= 4 * n, limit=(n + 5) * period, what="all samples")
    except AssertionError:
        from pio2.regs import DMA_LEVEL, DMA_MOVED, DMA_STATUS, DMA_TRIGGERS
        dut._log.error(f"words {len(core.words(5))} imu bursts {imu.samples} triggers "
                       f"{await core.read(DMA_CH(5, DMA_TRIGGERS))} moved {await core.read(DMA_CH(5, DMA_MOVED))} "
                       f"level {await core.read(DMA_CH(5, DMA_LEVEL)):#x} status {await core.read(DMA_CH(5, 0x0C)):#x}")
        raise
    assert imu.samples == n and imu.commands == [cmd] * n, f"{imu.samples} bursts, commands {imu.commands}"
    expect = []
    for k in range(n):
        b = Imu.image(k, cmd)
        expect += [int.from_bytes(bytes(b[4 * j:4 * j + 4]), "big") for j in range(4)]
    assert core.words(5) == expect, "a sample block was lost, repeated or out of order"
    trig = [r for r in core.records if r >> 24 & 0xFF == 0x0C]
    done = [r for r in core.records if r >> 24 & 0xFF == 0x09]
    assert len(trig) == n and len(done) == n, f"{len(trig)} trigger and {len(done)} done records"
    gaps = [((d >> 32) - (t >> 32)) for t, d in zip(trig, done)]
    dut._log.info(f"irq at {t_irq:.0f} ns; DRDY -> block done: {min(gaps)}..{max(gaps)} clocks "
                  f"(period {period})")
    assert max(gaps) < period, "a burst took longer than the sample period"
