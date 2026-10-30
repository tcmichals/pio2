# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""
The tile's AXI-Stream FIFO ports ([SPEC-ICN-09]).

* All four SMs echo TX into RX (PULL; MOV ISR, OSR; PUSH); 1000 words per SM go in through the TX
  stream ports with random TVALID gaps and come back through the RX stream ports with random
  TREADY: every word exactly once, in order.
* Register and stream access on the same FIFO at full rate: the stream is held off in every clock
  where the register port writes TXF0 or reads RXF0 (checked each clock), that case happens many
  times, and the words that enter TX FIFO 0 leave RX FIFO 0 in the same order, each delivered once,
  to the stream or to a register read.
"""
# @verifies [SPEC-ICN-09] docs/INTERCONNECT_SPECIFICATION.md

import random

import cocotb
from cocotb.triggers import ClockCycles, RisingEdge

from test_asp_pio_cocotb import (IRAM_BASE, PIO_MOV, PIO_PULL, PIO_PUSH, REG_CTRL, SM_CLKDIV,
                                 SM_EXECCTRL, SM_RXF, SM_TXF, init_tile, reg_read, reg_write)


async def load_echo(dut, sms):
    prog = [PIO_PULL(block=1), PIO_MOV(dest=6, op=0, src=7), PIO_PUSH(block=1)]
    for i, inst in enumerate(prog):
        await reg_write(dut, IRAM_BASE + 4 * i, inst)
    en = 0
    for n in sms:
        await reg_write(dut, SM_EXECCTRL(n), 2 << 12)      # wrap 0..2
        await reg_write(dut, SM_CLKDIV(n), 1 << 16)
        en |= 1 << n
    await reg_write(dut, REG_CTRL, en | (en << 4))


def lane(value, n):
    return (value >> (32 * n)) & 0xFFFF_FFFF


class Streams:
    """Clocked driver and monitor for the four TX and four RX stream ports. At each rising edge the
    handshakes are decided from the values settled before the edge; then the next values are driven
    (TVALID / TDATA held until taken)."""

    def __init__(self, dut, rng, p_valid, p_ready):
        self.dut, self.rng, self.p_valid, self.p_ready = dut, rng, p_valid, p_ready
        self.queues = [[] for _ in range(4)]   # words still to send per SM
        self.got = [[] for _ in range(4)]
        self.tdata = [0] * 4
        self.tvalid = 0
        self.ready = 0
        self.taken = 0                         # TX handshakes, all SMs
        dut.s_axis_tx_tvalid.value = 0
        dut.m_axis_rx_tready.value = 0

    def drive(self):
        for n in range(4):
            if not (self.tvalid >> n) & 1 and self.queues[n] and self.rng.random() < self.p_valid:
                self.tdata[n] = self.queues[n].pop(0)
                self.tvalid |= 1 << n
        self.ready = sum((self.rng.random() < self.p_ready) << n for n in range(4))
        self.dut.s_axis_tx_tdata.value = sum(d << (32 * n) for n, d in enumerate(self.tdata))
        self.dut.s_axis_tx_tvalid.value = self.tvalid
        self.dut.m_axis_rx_tready.value = self.ready

    def sample(self):
        """Call right after a rising edge: returns (tx handshakes, rx handshakes) as bit masks."""
        tx = self.tvalid & int(self.dut.s_axis_tx_tready.value)
        rx_valid = int(self.dut.m_axis_rx_tvalid.value)
        rx = self.ready & rx_valid
        rx_data = int(self.dut.m_axis_rx_tdata.value)
        for n in range(4):
            if (rx >> n) & 1:
                self.got[n].append(lane(rx_data, n))
        self.tvalid &= ~tx
        self.taken += bin(tx).count("1")
        return tx, rx

    async def run(self, until, max_clocks):
        self.drive()
        for _ in range(max_clocks):
            await RisingEdge(self.dut.clk)
            self.sample()
            if until():
                return
            self.drive()
        raise AssertionError("streams did not finish")


@cocotb.test(timeout_time=100, timeout_unit="ms")
async def test_stream_echo_all_sms_random_backpressure(dut):
    await init_tile(dut)
    await load_echo(dut, range(4))
    n_words = 1000
    sent = [[(n << 28) | ((i * 7919) & 0x0FFF_FFFF) for i in range(n_words)] for n in range(4)]
    st = Streams(dut, random.Random(11), p_valid=0.6, p_ready=0.5)
    st.queues = [list(s) for s in sent]
    await st.run(lambda: all(len(g) == n_words for g in st.got), 40 * n_words)
    await ClockCycles(dut.clk, 20)
    for n in range(4):
        assert st.got[n] == sent[n], f"SM{n}: echoed stream differs from the sent one"
    assert int(dut.m_axis_rx_tvalid.value) == 0, "a word was left over (duplicated)"


@cocotb.test(timeout_time=100, timeout_unit="ms")
async def test_register_and_stream_share_fifo_exactly_once(dut):
    await init_tile(dut)
    await load_echo(dut, [0])
    st = Streams(dut, random.Random(3), p_valid=1.0, p_ready=1.0)
    st.queues[0] = [0x1000_0000 | i for i in range(3000)]

    pushed, popped = [], []              # words entering TX FIFO 0 / leaving RX FIFO 0, per clock
    order = []                           # who took each popped word: "s" stream, "r" register
    reg_pops = 0
    collisions = {"tx": 0, "rx": 0}
    done = False

    async def monitor():
        nonlocal reg_pops
        prev_wp, prev_wdata = 0, None
        while not done:
            await RisingEdge(dut.clk)
            wb_w = int(dut.wb_tx_wvalid.value) & 1
            wb_r = int(dut.wb_rx_ren.value) & 1
            tx_ready = int(dut.s_axis_tx_tready.value) & 1
            rx_valid = int(dut.m_axis_rx_tvalid.value) & 1
            fifo_valid = int(dut.sm_rx_rvalid.value) & 1
            if wb_w:
                assert not tx_ready, "TX stream ready in a clock where the register port writes TXF0"
                collisions["tx"] += int(st.tvalid & 1)
            if wb_r:
                assert not rx_valid, "RX stream valid in a clock where the register port reads RXF0"
                collisions["rx"] += int(st.ready & 1 and fifo_valid)
            # A push is seen as the write pointer moving (a full FIFO also takes a word when the
            # SM pulls in the same clock, so TX ready alone undercounts).
            wp = int(dut.gen_sm[0].u_sm.tx_wr_ptr.value)
            if prev_wdata is not None and wp != prev_wp:
                pushed.append(prev_wdata)
            prev_wp = wp
            prev_wdata = int(dut.sm_tx_wdata[0].value) if int(dut.sm_tx_wvalid.value) & 1 else None
            if int(dut.sm_rx_ren.value) & 1 and fifo_valid:
                popped.append(int(dut.sm_rx_rdata[0].value))
                order.append("r" if wb_r else "s")
                reg_pops += wb_r

    mon = cocotb.start_soon(monitor())
    drv = cocotb.start_soon(st.run(lambda: done, 10**6))
    rng = random.Random(9)
    reg_values = []
    for i in range(300):                 # register traffic on the same FIFO at the same time
        await reg_write(dut, SM_TXF(0), 0x2000_0000 | i)    # may be dropped when TX is full
        reg_values.append(await reg_read(dut, SM_RXF(0)))  # junk when RX was empty: no pop
        await ClockCycles(dut.clk, rng.randrange(3))
    st.queues[0].clear()
    await ClockCycles(dut.clk, 50)
    done = True
    await ClockCycles(dut.clk, 2)
    drv.cancel()
    mon.cancel()

    assert collisions["tx"] > 20 and collisions["rx"] > 20, f"collision case too rare: {collisions}"
    diff = next((i for i, (a, b) in enumerate(zip(popped, pushed)) if a != b), None)
    assert diff is None, (f"RX FIFO 0 order differs from TX FIFO 0 order at {diff}: popped "
                          f"{[hex(w) for w in popped[diff - 2:diff + 4]]} pushed "
                          f"{[hex(w) for w in pushed[diff - 2:diff + 4]]}")
    assert len(pushed) - len(popped) <= 10, "words stuck in the SM"
    stream_words = [w for w, who in zip(popped, order) if who == "s"]
    assert st.got[0] == stream_words, "the stream received a word twice, or one popped for a register read"
    reg_words = [w for w, who in zip(popped, order) if who == "r"]
    assert reg_pops == len(reg_words)
    # Every register pop's value is among the register read results, in order.
    it = iter(reg_values)
    assert all(any(v == w for v in it) for w in reg_words), "a register read lost or reordered its word"
