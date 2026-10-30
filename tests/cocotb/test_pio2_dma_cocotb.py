# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""
DMA engine ([SPEC-DMA-01], [SPEC-DMA-02]) through pio2_core's boundary, small rings (16 words) so
the full / empty cases come quickly.

* RING in and out: SM0 echoes, words go in on channel 2 and come back on channel 3 in blocks of at
  most FRAME_WORDS, TDEST = channel, with random back-pressure on both streams; then 1000 blocks
  in steady state, every level back to 0 and MOVED exact.
* ONE_SHOT: exactly N words, then DONE, the DMA_DONE bit, and the 0x08 / 0x09 trace records.
* TRIGGERED: software trigger and input-pin trigger each move one block of N words; triggers counted.
* DIRECT: each word is its own block, offered at once.
* Pacing: an OUT ring that is full stops the PIO (RX full, SM stalls), counts one overrun per start,
  and loses nothing; an IN ring that runs dry while the SM drains its TX FIFO counts one underrun.
* FLUSH: a partial block leaves after FLUSH clocks and not before; FLUSH 0 sends whole blocks only.
* Register FIFO access while DMA owns the FIFO: every word exactly once.
"""
# cocotb-toplevel: pio2_core
# @verifies [SPEC-DMA-01] [SPEC-DMA-02] docs/INTERCONNECT_SPECIFICATION.md

import cocotb
from cocotb.triggers import ClockCycles

from pio2_core_tb import COUNTER, ECHO, SINK, Core, counter_words
from pio2.regs import (CTRL_RESET_RING, CTRL_START, CTRL_SW_TRIGGER, DIRECT, DMA_CFG, DMA_CH,
                       DMA_COUNT, DMA_CTRL, DMA_DONE, DMA_FLUSH, DMA_LEVEL, DMA_MOVED,
                       DMA_OVERRUNS, DMA_STATUS, DMA_TRIGGERS, DMA_UNDERRUNS, IN, ONE_SHOT, OUT, RING,
                       RXF, SM, TRIGGERED, dma_cfg)

RING_WORDS = 16      # SIM_PARAMS in the Makefile for this suite
FRAME = 28


async def level(core, chan):
    return await core.read(DMA_CH(chan, DMA_LEVEL)) & 0xFFFF


@cocotb.test(timeout_time=200, timeout_unit="ms")
async def test_ring_in_and_out_with_backpressure(dut):
    core = await Core.start(dut)
    core.stall(seed=1)
    await core.load(ECHO, sm=0)
    await core.write(DMA_CH(3, DMA_FLUSH), 20)
    await core.write(DMA_CH(2, DMA_CFG), dma_cfg(direction=IN, mode=RING, sm=0))
    await core.write(DMA_CH(3, DMA_CFG), dma_cfg(direction=OUT, mode=RING, sm=0))
    sent = [(0xA000_0000 | i) for i in range(500)]
    for i in range(0, len(sent), 10):
        await core.send(2, sent[i:i + 10])
    await core.wait_for(lambda: len(core.words(3)) >= len(sent), what="echoed words")
    assert core.words(3) == sent
    assert all(1 <= len(f) <= FRAME for f in core.frames[3]), [len(f) for f in core.frames[3]]
    assert set(core.frames) == {3}, f"blocks on other channels: {sorted(core.frames)}"
    assert await core.read(DMA_CH(2, DMA_MOVED)) == len(sent)
    assert await core.read(DMA_CH(3, DMA_MOVED)) == len(sent)


@cocotb.test(timeout_time=2000, timeout_unit="ms")
async def test_steady_state_1000_blocks(dut):
    core = await Core.start(dut)
    core.stall(seed=2, p_in=0.1, p_out=0.1)
    await core.load(ECHO, sm=0)
    await core.write(DMA_CH(3, DMA_FLUSH), 50)
    await core.write(DMA_CH(2, DMA_CFG), dma_cfg(direction=IN, mode=RING, sm=0))
    await core.write(DMA_CH(3, DMA_CFG), dma_cfg(direction=OUT, mode=RING, sm=0))
    n = 0
    for blk in range(1000):
        words = [(blk << 16) | k for k in range(1 + blk % 12)]
        n += len(words)
        await core.send(2, words)
    await core.wait_for(lambda: len(core.words(3)) >= n, limit=10**6, what="1000 blocks")
    assert core.words(3) == [(b << 16) | k for b in range(1000) for k in range(1 + b % 12)]
    await ClockCycles(dut.clk, 100)
    assert await level(core, 2) == 0 and await level(core, 3) == 0, "a ring did not drain"
    assert await core.read(DMA_CH(2, DMA_MOVED)) == n
    assert await core.read(DMA_CH(3, DMA_MOVED)) == n


@cocotb.test(timeout_time=200, timeout_unit="ms")
async def test_one_shot_moves_exactly_n_and_reports_done(dut):
    core = await Core.start(dut)
    await core.trace_on()
    await core.load(COUNTER, sm=1)
    await core.write(DMA_CH(4, DMA_COUNT), 10)
    await core.write(DMA_CH(4, DMA_FLUSH), 5)
    await core.write(DMA_CH(4, DMA_CFG), dma_cfg(direction=OUT, mode=ONE_SHOT, sm=1))
    await core.wait_for(lambda: len(core.words(4)) >= 10, what="10 words")
    await ClockCycles(dut.clk, 200)
    assert core.words(4) == counter_words(10), "ONE_SHOT moved a different count or order"
    st = await core.read(DMA_CH(4, DMA_STATUS))
    assert st & 1 and not st & 2 and st >> 16 == 0, f"STATUS {st:#x}: expected DONE, not BUSY, 0 left"
    assert await core.read(DMA_DONE) & (1 << 3), "DMA_DONE bit of channel 4"
    await core.write(DMA_DONE, 1 << 3)
    assert not await core.read(DMA_CH(4, DMA_STATUS)) & 1, "DONE did not clear"
    await core.write(DMA_CH(4, DMA_CTRL), CTRL_START)                 # N more words
    await core.wait_for(lambda: len(core.words(4)) >= 20, what="second ONE_SHOT")
    await ClockCycles(dut.clk, 200)
    assert core.words(4) == counter_words(20)
    dma = [(r >> 24 & 0xFF, r >> 16 & 0xFF, r & 0xFFFF) for r in core.records if 0x08 <= (r >> 24 & 0xFF) <= 0x0C]
    assert dma == [(0x08, 4, 10), (0x09, 4, 10)] * 2, dma


@cocotb.test(timeout_time=200, timeout_unit="ms")
async def test_triggered_by_software_and_by_pin(dut):
    core = await Core.start(dut)
    await core.trace_on()
    await core.load(COUNTER, sm=2)
    await core.write(DMA_CH(5, DMA_COUNT), 4)
    await core.write(DMA_CH(5, DMA_FLUSH), 3)
    await core.write(DMA_CH(5, DMA_CFG), dma_cfg(direction=OUT, mode=TRIGGERED, sm=2, trig_pin=True,
                                                 trig_idx=7))
    await ClockCycles(dut.clk, 300)
    assert core.words(5) == [], "TRIGGERED moved words without a trigger"
    await core.write(DMA_CH(5, DMA_CTRL), CTRL_SW_TRIGGER)
    await core.wait_for(lambda: len(core.words(5)) >= 4, what="first block")
    await ClockCycles(dut.clk, 200)
    assert core.words(5) == counter_words(4)
    for k in range(3):                         # DRDY-style rising edges on pin 7
        dut.i_gpio.value = 1 << 7
        await ClockCycles(dut.clk, 3)
        dut.i_gpio.value = 0
        await core.wait_for(lambda: len(core.words(5)) >= 8 + 4 * k, what=f"pin block {k}")
        await ClockCycles(dut.clk, 200)
    assert core.words(5) == counter_words(16)
    assert await core.read(DMA_CH(5, DMA_TRIGGERS)) == 4
    trig = [r & 0xFFFF for r in core.records if r >> 24 & 0xFF == 0x0C]
    assert trig == [1, 2, 3, 4], f"trigger records {trig}"


@cocotb.test(timeout_time=200, timeout_unit="ms")
async def test_direct_mode_one_word_blocks(dut):
    core = await Core.start(dut)
    await core.load(COUNTER, sm=3)
    await core.write(DMA_CH(6, DMA_CFG), dma_cfg(direction=OUT, mode=DIRECT, sm=3))
    await core.wait_for(lambda: len(core.words(6)) >= 50, what="50 words")
    await core.write(DMA_CH(6, DMA_CFG), dma_cfg(en=False, direction=OUT, mode=DIRECT, sm=3))
    await ClockCycles(dut.clk, 100)
    got = core.words(6)
    assert got == counter_words(len(got))
    assert all(len(f) == 1 for f in core.frames[6]), "DIRECT block longer than one word"


@cocotb.test(timeout_time=200, timeout_unit="ms")
async def test_overrun_paces_the_pio_and_loses_nothing(dut):
    core = await Core.start(dut)
    await core.trace_on(mask=1 << 0x0B)
    await core.load(COUNTER, sm=0)
    core.sink.pause = True                     # the host does not read: the ring fills
    await core.write(DMA_CH(2, DMA_FLUSH), 1)
    await core.write(DMA_CH(2, DMA_CFG), dma_cfg(direction=OUT, mode=RING, sm=0))
    await ClockCycles(dut.clk, 400)
    assert await level(core, 2) == RING_WORDS, "ring not full"
    assert await core.read(DMA_CH(2, DMA_OVERRUNS)) == 1, "one overrun per start"
    core.sink.pause = False
    await core.wait_for(lambda: len(core.words(2)) >= 200, what="drained words")
    core.sink.pause = True
    before = await core.read(DMA_CH(2, DMA_OVERRUNS))
    await ClockCycles(dut.clk, 400)
    after = await core.read(DMA_CH(2, DMA_OVERRUNS))
    assert after == before + 1 or (before >= 2 and after == before), \
        f"overruns {before} -> {after}: a stopped reader must start exactly one more"
    await ClockCycles(dut.clk, 400)
    assert await core.read(DMA_CH(2, DMA_OVERRUNS)) == after, "a lasting overrun counted again"
    core.sink.pause = False
    await core.write(DMA_CH(2, DMA_CFG), dma_cfg(en=False, direction=OUT, mode=RING, sm=0))
    await ClockCycles(dut.clk, 300)
    got = core.words(2)
    assert got == counter_words(len(got)), "a word was lost or repeated around the overruns"
    final = await core.read(DMA_CH(2, DMA_OVERRUNS))       # the reader's restart may start one more
    over = [r & 0xFFFF for r in core.records if r >> 24 & 0xFF == 0x0B]
    assert over == list(range(1, final + 1)), f"overrun records {over}, counter {final}"


@cocotb.test(timeout_time=200, timeout_unit="ms")
async def test_underrun_counted_once_per_start(dut):
    core = await Core.start(dut)
    await core.trace_on(mask=1 << 0x0A)
    await core.load(SINK, sm=1)
    await core.write(DMA_CH(7, DMA_CFG), dma_cfg(direction=IN, mode=RING, sm=1))
    await ClockCycles(dut.clk, 200)
    assert await core.read(DMA_CH(7, DMA_UNDERRUNS)) == 0, "underrun before any word moved"
    for k in range(3):
        await core.send(7, list(range(20)))
        await ClockCycles(dut.clk, 400)
        assert await core.read(DMA_CH(7, DMA_UNDERRUNS)) == k + 1
    assert await core.read(DMA_CH(7, DMA_MOVED)) == 60
    under = [r & 0xFFFF for r in core.records if r >> 24 & 0xFF == 0x0A]
    assert under == [1, 2, 3], under
    await core.write(DMA_CH(7, DMA_UNDERRUNS), 0)
    assert await core.read(DMA_CH(7, DMA_UNDERRUNS)) == 0, "write did not clear"


@cocotb.test(timeout_time=200, timeout_unit="ms")
async def test_flush_time(dut):
    core = await Core.start(dut)
    await core.load(ECHO, sm=0)
    await core.write(DMA_CH(2, DMA_CFG), dma_cfg(direction=IN, mode=RING, sm=0))
    await core.write(DMA_CH(3, DMA_CFG), dma_cfg(direction=OUT, mode=RING, sm=0))   # FLUSH 0
    await core.send(2, [1, 2, 3])
    await ClockCycles(dut.clk, 2000)
    assert core.words(3) == [], "FLUSH 0 sent a partial block"
    # The 16-word ring is smaller than a frame: a full ring is a whole block.
    await core.send(2, list(range(4, 17)))
    await core.wait_for(lambda: len(core.words(3)) == RING_WORDS, what="whole block")
    assert core.frames[3] == [list(range(1, 17))]
    await core.write(DMA_CH(3, DMA_FLUSH), 300)
    t0 = cocotb.utils.get_sim_time("ns")
    await core.send(2, [7, 8])
    await core.wait_for(lambda: len(core.frames[3]) == 2, what="flushed block")
    waited = (cocotb.utils.get_sim_time("ns") - t0) / 10
    assert core.frames[3][1] == [7, 8]
    assert 300 <= waited <= 340, f"partial block left after {waited} clocks, FLUSH 300"


@cocotb.test(timeout_time=200, timeout_unit="ms")
async def test_ring_reset_and_register_access_beside_dma(dut):
    core = await Core.start(dut)
    await core.load(ECHO, sm=0)
    await core.write(DMA_CH(2, DMA_CFG), dma_cfg(direction=IN, mode=RING, sm=1))   # SM1 is idle
    await core.send(2, list(range(10)))
    await ClockCycles(dut.clk, 50)
    assert await level(core, 2) == 6, "4 words go to SM1's TX FIFO, 6 stay in the ring"
    await core.write(DMA_CH(2, DMA_CTRL), CTRL_RESET_RING)
    await ClockCycles(dut.clk, 5)
    assert await level(core, 2) == 0, "RESET_RING left words"
    # DMA owns SM0's RX FIFO while the host also reads RXF0: every word exactly once.
    await core.write(DMA_CH(3, DMA_FLUSH), 2)
    await core.write(DMA_CH(3, DMA_CFG), dma_cfg(direction=OUT, mode=RING, sm=0))
    await core.write(DMA_CH(2, DMA_CFG), dma_cfg(direction=IN, mode=RING, sm=0))
    sent = list(range(0x100, 0x100 + 300))
    reg = []
    send = cocotb.start_soon(core.send(2, sent))
    for _ in range(150):
        reg.append(await core.read(SM(0, RXF)))
    await send
    await ClockCycles(dut.clk, 500)
    # An RXF read of an empty FIFO pops nothing and returns a stale word, so register results are
    # matched by what is left: the stream got MOVED words, each once and in order, and every other
    # word was returned by a register read.
    stream = core.words(3)
    assert len(stream) == await core.read(DMA_CH(3, DMA_MOVED))
    assert len(set(stream)) == len(stream), "the stream got a word twice"
    assert stream == [w for w in sent if w in set(stream)], "stream order"
    rest = [w for w in sent if w not in set(stream)]
    assert rest and set(rest) <= set(reg), "a word reached neither the stream nor a register read"
