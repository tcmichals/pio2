# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""pio2_core register map ([SPEC-AXI-03]): one place for the debugger, the host driver and the tests.

Windows: 0x0000 PIO tile ([SPEC-PIO-05]), 0x1000 DMA ([SPEC-DMA-01]), 0x2000 trace ([SPEC-TRC-02]),
0x3000 core ([SPEC-ICN-11]), 0x3100 link (served by the SPI adapter, [SPEC-LINK-07]).
"""
# @impl [SPEC-AXI-03] [SPEC-DMA-01] [SPEC-ICN-11] docs/INTERCONNECT_SPECIFICATION.md

# ---------------------------------------------------------------- tile (0x0000)
REG_CTRL, REG_IRQ, REG_FSTAT, IRAM = 0x00, 0x04, 0x08, 0x10
CLKDIV, EXECCTRL, SHIFTCTRL, PINCTRL, TXF, RXF, INSTR = 0x00, 0x04, 0x08, 0x0C, 0x10, 0x14, 0x18


def SM(n, off):
    return 0x100 + 0x20 * n + off


# ---------------------------------------------------------------- DMA (0x1000)
DMA_CAPS = 0x1000
DMA_CFG, DMA_COUNT, DMA_LEVEL, DMA_STATUS = 0x00, 0x04, 0x08, 0x0C
DMA_MOVED, DMA_UNDERRUNS, DMA_OVERRUNS, DMA_TRIGGERS = 0x10, 0x14, 0x18, 0x1C
DMA_FLUSH, DMA_THR, DMA_CTRL = 0x20, 0x24, 0x28
ONE_SHOT, RING, TRIGGERED, DIRECT = 0, 1, 2, 3
OUT, IN = 0, 1
CTRL_RESET_RING, CTRL_START, CTRL_SW_TRIGGER = 1, 2, 4
TRACE_CHAN = 1
PIO_CHANS = range(2, 10)


def DMA_CH(chan, off):
    """Register `off` of DMA channel `chan` (1 = trace, 2..9 = PIO channels)."""
    return 0x1000 + 0x40 * chan + off


def dma_cfg(en=True, direction=OUT, mode=RING, sm=0, trig_pin=False, trig_idx=0):
    return (int(en) | (direction << 1) | (mode << 2) | (sm << 4) | (int(trig_pin) << 6)
            | ((trig_idx & 0x1F) << 8))


# ---------------------------------------------------------------- trace (0x2000)
TRACE_MASK, TRACE_LEVEL, TRACE_DROPPED, TRACE_MERGED, TIMESTAMP = 0x2000, 0x2004, 0x2008, 0x200C, 0x2010

# ---------------------------------------------------------------- core (0x3000)
CORE_ID, CORE_VERSION, ISR, IMR = 0x3000, 0x3004, 0x3008, 0x300C
STREAM_PEND, DMA_DONE, DMA_ERR, IN_DROPPED = 0x3010, 0x3014, 0x3018, 0x301C
ID_VALUE, VERSION_VALUE = 0x5049_4F32, 0x0001_0000
ISR_STREAM, ISR_DMA_DONE, ISR_DMA_ERR, ISR_TRACE_DROPPED, ISR_PIO_IRQ = 1 << 2, 1 << 3, 1 << 4, 1 << 7, 1 << 8
