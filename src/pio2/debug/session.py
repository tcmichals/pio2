# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""pio2.debug.Session: the one engine behind the GUI, the CLI and the tests ([SPEC-DBG-09]).

It runs the unmodified tile RTL (libpio2sim.so, Verilator) and reads its state through VPI by name.

    s = Session()
    s.load("blink", sm=0, pinctrl=Pins(set_base=25, set_count=1))
    s.push(0, 100)            # blink's delay
    s.step(1000)
    s.sm_state(0)["pc"], s.pin(25)
"""
# @impl [SPEC-DBG-01] [SPEC-DBG-02] [SPEC-DBG-03] [SPEC-DBG-09] docs/DEBUGGER_SPECIFICATION.md

import ctypes as C
from array import array
from dataclasses import dataclass

from pio2.debug.build import build
from pio2.debug.disasm import listing
from pio2.programs import load_program

REG_CTRL = 0x00
REG_IRQ = 0x04
REG_FSTAT = 0x08
IRAM = 0x10


def SM(n, off):
    return 0x100 + 0x20 * n + off


CLKDIV, EXECCTRL, SHIFTCTRL, PINCTRL, TXF, RXF, INSTR = 0x00, 0x04, 0x08, 0x0C, 0x10, 0x14, 0x18

# Per-SM signals the debugger shows; a missing one fails at session start ([SPEC-DBG-01]).
SM_SIGNALS = ["pc", "reg_x", "reg_y", "isr", "osr", "isr_count", "osr_count", "delay_cycles",
              "stalled", "irq_waiting", "exec_pending", "push_pending", "tx_level", "rx_level",
              "tx_rd_ptr", "rx_rd_ptr", "clkdiv_int_cnt", "clkdiv_frac_acc", "sm_clk_enable"]
TILE_SIGNALS = ["irq_flags", "sm_en"]


@dataclass
class Pins:
    """PINCTRL fields (RP2040 layout)."""
    out_base: int = 0
    out_count: int = 0
    set_base: int = 0
    set_count: int = 0
    sideset_base: int = 0
    in_base: int = 0

    def word(self, sideset_count=0):
        return ((self.out_base & 31) | (self.set_base & 31) << 5 | (self.sideset_base & 31) << 10
                | (self.in_base & 31) << 15 | (self.out_count & 63) << 20 | (self.set_count & 7) << 26
                | (sideset_count & 7) << 29)


class SimError(RuntimeError):
    pass


def _lib():
    lib = C.CDLL(str(build()))
    vp, u32, u64, i32 = C.c_void_p, C.c_uint32, C.c_uint64, C.c_int
    sig = {
        "create": ([], vp), "destroy": ([vp], None), "reset": ([vp, i32], None), "cycle": ([vp], u64),
        "set_ext": ([vp, u32, u32, u32], None), "gpio_out": ([vp], u32), "gpio_dir": ([vp], u32),
        "gpio_in": ([vp], u32),
        "run": ([vp, u32], None), "record": ([vp, i32], None), "pending": ([vp], u32),
        "drain": ([vp, u32, C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p], None),
        "write": ([vp, u32, u32], i32), "read": ([vp, u32, C.POINTER(u32)], i32),
        "get": ([vp, C.c_char_p, C.POINTER(u32), i32], i32),
        "get_index": ([vp, C.c_char_p, i32, C.POINTER(u32), i32], i32),
    }
    for name, (args, res) in sig.items():
        f = getattr(lib, "pio2sim_" + name)
        f.argtypes, f.restype = args, res
    return lib


_LIB = None


class Trace:
    """Per-clock history of the last `depth` clocks: pins out / dir / in, PC and stall per SM."""

    def __init__(self, depth=200_000):
        self.depth = depth
        self.start = 0                     # cycle of element 0
        self.out = array("I")
        self.dir = array("I")
        self.inp = array("I")
        self.pcs = array("B")              # 4 per clock
        self.stalls = array("B")

    def extend(self, out, dir_, inp, pcs, stalls):
        self.out.extend(out)
        self.dir.extend(dir_)
        self.inp.extend(inp)
        self.pcs.extend(pcs)
        self.stalls.extend(stalls)
        extra = len(self.out) - self.depth
        if extra > 0:
            del self.out[:extra], self.dir[:extra], self.inp[:extra]
            del self.pcs[:4 * extra], self.stalls[:4 * extra]
            self.start += extra

    def __len__(self):
        return len(self.out)


class Session:
    def __init__(self, trace_depth=200_000):
        global _LIB
        if _LIB is None:
            _LIB = _lib()
        self.lib = _LIB
        h = self.lib.pio2sim_create()
        if not h:
            raise SimError("a Session already exists in this process (one Verilator model per process)")
        self.h = C.c_void_p(h)
        self.trace = Trace(trace_depth)
        self.programs = {}                 # sm -> (program, offset)
        self.ext = 0
        self.ext_mask = 0
        self.pull_level = 0xFFFF_FFFF
        for name in TILE_SIGNALS + [f"gen_sm[0].u_sm.{s}" for s in SM_SIGNALS]:
            if self._get(name) is None:
                raise SimError(f"RTL signal {name!r} not found (renamed?): update SM_SIGNALS")
        self.reset()

    def close(self):
        if self.h:
            self.lib.pio2sim_destroy(self.h)
            self.h = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------------------------------------------------------------- registers and pins
    def reset(self):
        self.lib.pio2sim_record(self.h, 0)
        self.lib.pio2sim_reset(self.h, 5)
        self.programs.clear()
        self.trace = Trace(self.trace.depth)
        self.trace.start = self.cycle
        self.lib.pio2sim_record(self.h, 1)

    def _drain(self):
        """Move every clock the model ran (stepping or register access) into the trace."""
        n = self.lib.pio2sim_pending(self.h)
        if not n:
            return
        out, dir_, inp = array("I", bytes(4 * n)), array("I", bytes(4 * n)), array("I", bytes(4 * n))
        pcs, stalls = array("B", bytes(4 * n)), array("B", bytes(4 * n))
        addr = lambda a: a.buffer_info()[0]  # noqa: E731
        self.lib.pio2sim_drain(self.h, n, addr(out), addr(dir_), addr(inp), addr(pcs), addr(stalls))
        self.trace.extend(out, dir_, inp, pcs, stalls)

    @property
    def cycle(self):
        return self.lib.pio2sim_cycle(self.h)

    def write(self, off, value):
        r = self.lib.pio2sim_write(self.h, off, value & 0xFFFF_FFFF)
        self._drain()
        if r != 0:
            raise SimError(f"AXI4-Lite write 0x{off:03X} answered {r}")

    def read(self, off):
        v = C.c_uint32()
        r = self.lib.pio2sim_read(self.h, off, C.byref(v))
        self._drain()
        if r != 0:
            raise SimError(f"AXI4-Lite read 0x{off:03X} answered {r}")
        return v.value

    def drive(self, pin, level):
        """A part drives `pin` (level 0/1), or releases it with level None."""
        if level is None:
            self.ext_mask &= ~(1 << pin)
        else:
            self.ext_mask |= 1 << pin
            self.ext = (self.ext & ~(1 << pin)) | ((level & 1) << pin)
        self.lib.pio2sim_set_ext(self.h, self.ext, self.ext_mask, self.pull_level)

    def pins(self):
        """(out, dir, in) of the 32 lines now."""
        return (self.lib.pio2sim_gpio_out(self.h), self.lib.pio2sim_gpio_dir(self.h),
                self.lib.pio2sim_gpio_in(self.h))

    def pin(self, n):
        return (self.lib.pio2sim_gpio_in(self.h) >> n) & 1

    # ---------------------------------------------------------------- programs and FIFOs
    def load(self, program, sm=0, offset=0, pinctrl=None, clkdiv=1.0, shiftctrl=0, execctrl=0,
             enable=True):
        """Load a program image (name or dict) at `offset` and configure SM `sm` from it, as the Pico
        SDK's pio_sm_init does: wrap and side-set from the image, restart, jump to the start."""
        p = load_program(program) if isinstance(program, str) else program
        for i, w in enumerate(p["instructions"]):
            self.write(IRAM + 4 * (offset + i), w)
        ss = p["sideset"]
        execctrl |= ((p["wrap"] + offset) << 12) | ((p["wrap_target"] + offset) << 7)
        if ss["size"] and ss["optional"]:
            execctrl |= 1 << 30
        if ss["size"] and ss["pindirs"]:
            execctrl |= 1 << 29
        pc = (pinctrl or Pins()).word(ss["size"])
        div_int = int(clkdiv)
        div_frac = int(round((clkdiv - div_int) * 256)) & 0xFF
        self.write(SM(sm, CLKDIV), (div_int << 16) | (div_frac << 8))
        self.write(SM(sm, EXECCTRL), execctrl)
        self.write(SM(sm, SHIFTCTRL), shiftctrl)
        self.write(SM(sm, PINCTRL), pc)
        ctrl = self.read(REG_CTRL) & 0xF
        self.write(REG_CTRL, (ctrl & ~(1 << sm)) | (1 << (sm + 4)))     # disable + restart
        self.write(SM(sm, INSTR), offset & 0x1F)                          # jmp offset
        self.programs[sm] = (p, offset)
        if enable:
            self.enable(sm)
        return p

    def enable(self, sm, on=True):
        ctrl = self.read(REG_CTRL) & 0xF
        self.write(REG_CTRL, (ctrl | (1 << sm)) if on else (ctrl & ~(1 << sm)))

    def set_pindirs(self, sm, pins_mask, dirs):
        """Set pin directions with `set pindirs` through SMx_INSTR, one pin at a time (as
        pio_sm_set_pindirs_with_mask does)."""
        old = self.read(SM(sm, PINCTRL))
        for pin in range(32):
            if pins_mask >> pin & 1:
                self.write(SM(sm, PINCTRL), (pin << 5) | (1 << 26))
                self.write(SM(sm, INSTR), 0xE080 | ((dirs >> pin) & 1))
        self.write(SM(sm, PINCTRL), old)

    def push(self, sm, word):
        self.write(SM(sm, TXF), word)

    def rx_empty(self, sm):
        return bool((self.read(REG_FSTAT) >> (28 + sm)) & 1)

    def pull(self, sm):
        return None if self.rx_empty(sm) else self.read(SM(sm, RXF))

    # ---------------------------------------------------------------- running
    def step(self, n=1):
        """Run n system clocks; they and every clock before them are in the trace."""
        if n > 0:
            self.lib.pio2sim_run(self.h, n)
        self._drain()

    def step_instr(self, sm, limit=1_000_000):
        """Run until SM `sm` retires one instruction ([SPEC-DBG-03]) and return the clocks run.
        An instruction retires on a clock where the SM's clock enable fires with no delay pending
        and the SM is not stalled afterwards; a stalled instruction keeps running (and does not
        retire) until it can complete."""
        if not (self._get("sm_en") >> sm) & 1:      # VPI: a register read would run clocks
            raise SimError(f"SM{sm} is disabled")
        base = f"gen_sm[{sm}].u_sm."
        for k in range(1, limit + 1):
            fire = self._get(base + "sm_clk_enable") and self._get(base + "delay_cycles") == 0
            pc0 = self._get(base + "pc")
            self.step(1)
            if fire and not self._get(base + "stalled"):
                return k
            if self._get(base + "pc") != pc0:
                return k
        return limit

    # ---------------------------------------------------------------- state
    def _get(self, name, index=None, nwords=1):
        buf = (C.c_uint32 * nwords)()
        if index is None:
            w = self.lib.pio2sim_get(self.h, name.encode(), buf, nwords)
        else:
            w = self.lib.pio2sim_get_index(self.h, name.encode(), index, buf, nwords)
        if w < 0:
            return None
        v = 0
        for i in range(nwords):
            v |= buf[i] << (32 * i)
        return v

    # @impl [SPEC-DBG-02] docs/DEBUGGER_SPECIFICATION.md
    def sm_state(self, sm):
        """[SPEC-DBG-02] fields of one SM."""
        st = {s: self._get(f"gen_sm[{sm}].u_sm.{s}") for s in SM_SIGNALS}
        shift = self._get("sm_shiftctrl", sm)
        jt = bool(shift >> 30 & 1)
        jr = bool(shift >> 31 & 1) and not jt
        mem = [self._get(f"gen_sm[{sm}].u_sm.fifo_mem", i) for i in range(8)]
        tx_idx = (lambda p: p & 7) if jt else (lambda p: p & 3)
        rx_idx = (lambda p: p & 7) if jr else (lambda p: 4 | (p & 3))
        st["tx_fifo"] = [mem[tx_idx(st["tx_rd_ptr"] + i)] for i in range(st["tx_level"])]
        st["rx_fifo"] = [mem[rx_idx(st["rx_rd_ptr"] + i)] for i in range(st["rx_level"])]
        st["enabled"] = bool(self._get("sm_en") >> sm & 1)
        st["clkdiv"] = self._get("sm_clkdiv", sm)
        st["execctrl"] = self._get("sm_execctrl", sm)
        st["shiftctrl"] = shift
        st["pinctrl"] = self._get("sm_pinctrl", sm)
        st["wrap_bottom"] = st["execctrl"] >> 7 & 31
        st["wrap_top"] = st["execctrl"] >> 12 & 31
        st["instr"] = self._get("iram", st["pc"])
        return st

    def irq_flags(self):
        return self._get("irq_flags")

    def iram(self):
        return [self._get("iram", i) for i in range(32)]

    def listing(self, sm):
        """Disassembly of the program loaded on `sm` as [(address, word, text)]."""
        if sm not in self.programs:
            return []
        p, off = self.programs[sm]
        return [(off + a, w, t) for a, w, t in listing(p)]
