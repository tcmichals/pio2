# pio2 Visual Debugger: Design

Status: design, nothing implemented yet (2026-10-29). Requirements carry `[SPEC-DBG-*]` tags; code
that implements one carries `@impl [SPEC-DBG-nn] docs/debugger.md`.

## 1. Goal

Write a PIO program, load it into the **real RTL** of the tile, and watch it run one clock or one
instruction at a time: every state machine's registers, FIFOs, clock divider and pins, with
breakpoints, a logic analyzer, stimulus on the input pins, and the ability to step **backwards**.

## 2. What exists, and what this does differently

Prior art (links in [references.md](references.md); check current state before relying on this):

| Tool | Model | What it is |
| :--- | :--- | :--- |
| Wokwi | software model of the RP2040, in the browser | full-chip simulation incl. PIO, logic analyzer |
| rp2040pio (soundpaint) | Java software emulator | PIO emulator with a GUI and register views |
| rp2040-pio-emulator (NathanY3G) | Python software emulator | library for testing PIO programs |

All of them run a **software model** of the PIO. pio2 has a hardware implementation, so the
debugger runs the RTL itself (Verilator), which gives what a software emulator cannot:

1. **What you debug is what the FPGA runs**: no second model that can disagree with the hardware.
2. **Two models side by side** (`[SPEC-DBG-08]`): the same program on the RTL and on an independent
   reference emulator, stopping at the first cycle where pins, PC or registers differ. That checks
   the RTL against the RP2040 behaviour and checks the program against both.
3. **Reverse step** (`[SPEC-DBG-06]`): Verilator model snapshots every N cycles, replay forward to
   any earlier cycle.
4. **Clock-exact multi-SM view**: one timeline per state machine (instruction, stall reason, delay,
   side-set) on the same cycle axis as the pins, including the clock divider's fractional phase.
5. **Protocol stimulus** (`[SPEC-DBG-07]`): input pins driven by generators (UART bytes, DShot ESC
   reply, I2C slave, square wave, recorded capture), not only by toggling a pin by hand.
6. **Headless and scriptable** (`[SPEC-DBG-09]`): the same engine runs from Python and the CLI, so a
   debugging session becomes a regression test (recorded golden trace).

## 3. Architecture

```
 .pio source ──pioasm──► image (.json) ─┐
                                        ▼
 ┌───────────────────── pio2.debug (Python) ─────────────────────┐
 │  Session: load, step, run, breakpoints, snapshots, stimulus   │
 │  Trace store (per-cycle pins, per-SM state)                    │
 └──────────────┬──────────────────────────────┬─────────────────┘
                │ ctypes (C ABI)               │
 ┌──────────────▼──────────────┐   ┌───────────▼───────────┐
 │ libpio2sim.so (Verilator)   │   │ reference emulator     │ (optional, [SPEC-DBG-08])
 │ asp_pio_tile + C wrapper    │   └────────────────────────┘
 └─────────────────────────────┘
                ▲
 GUI: imgui-bundle (ImGui + ImPlot)      CLI: pio2-debug run/step/trace
```

## 4. Requirements

### `[SPEC-DBG-01]` The RTL Is the Model
* **Requirement**: the debugger simulates `src/rtl/asp_pio_tile.sv` with Verilator, unmodified. Internal
  state is made readable with a Verilator configuration file (`public_flat_rd` per signal), never by
  editing the RTL for the debugger.
* **Requirement**: `libpio2sim.so` exports a C ABI: create/destroy, reset, Wishbone read/write, set input
  pins, read pin outputs and directions, step N clocks, read per-SM state, save/restore a snapshot,
  open/close an FST trace.

### `[SPEC-DBG-02]` Visible State per State Machine
* **Requirement**: per SM and per cycle: PC, the instruction at PC (disassembled), X, Y, ISR and OSR with
  their shift counts, TX/RX FIFO levels and contents (with FJOIN), stall flag and its reason (wait
  condition, FIFO full/empty, IRQ wait, EXEC latched), remaining delay cycles, side-set value, clock
  divider integer count and fractional accumulator, enable and wrap bounds.
* **Requirement**: tile level: 8 IRQ flags, pin outputs and directions, input pins, the shared
  instruction memory.

### `[SPEC-DBG-03]` Stepping
* **Requirement**: step one system clock; step one instruction of a chosen SM (runs until that SM
  retires an instruction, i.e. its clock-divider enable fires and it is not stalled); step any SM;
  run N cycles; run until a breakpoint; pause.
* **Requirement**: "instruction retired" is derived from the exposed state (PC change, or same PC with
  the delay counter and stall cleared), and the rule is tested against the conformance programs.

### `[SPEC-DBG-04]` Breakpoints and Watchpoints
* **Requirement**: breakpoints on PC per SM; watchpoints on a pin edge, an IRQ flag, a FIFO level, X/Y
  value, or a stall reason; each stops at the cycle it becomes true.

### `[SPEC-DBG-05]` Logic Analyzer and Timeline
* **Requirement**: ImPlot view of selected pins and per-SM tracks (instruction, stall, side-set) over
  cycles, with cursors, zoom, cycle and time readout (system clock in Hz is configurable), and a
  measurement between two cursors.
* **Requirement**: export the session to FST (GTKWave) and to a JSON trace.

### `[SPEC-DBG-06]` Reverse Step
* **Requirement**: snapshots every N cycles (default 1024, bounded memory); stepping back restores the
  nearest earlier snapshot and runs forward to the target cycle; stimulus is replayed from the
  recorded input trace so the result is identical.

### `[SPEC-DBG-07]` Stimulus
* **Requirement**: input pins come from a list of sources: constant, toggle by hand, square wave,
  UART 8N1 bytes at a baud rate, DShot ESC reply (eRPM telemetry), I2C slave (ACK/data), replay of a
  recorded capture. TX FIFO pushes and RX FIFO pulls by hand or from a script.

### `[SPEC-DBG-08]` Second Model (Diff Mode)
* **Requirement**: optionally run the same program and stimulus on an independent reference emulator
  and stop at the first cycle where pins, PC, X/Y, ISR/OSR or FIFO levels differ, showing both. The
  reference is an external package used through a thin adapter (license checked before use).

### `[SPEC-DBG-09]` Headless, Scriptable, Testable
* **Requirement**: `pio2.debug.Session` is the one engine behind GUI, CLI and tests. `pio2-debug` runs a
  program for N cycles or to a condition and writes the trace; a test can compare a run with a
  recorded golden trace.
* **Requirement**: the debugger has its own tests (pytest): every conformance program stepped by
  instruction gives the same final state as the cocotb run; reverse step returns bit-identical state;
  a 100,000-cycle run keeps memory bounded.

### `[SPEC-DBG-10]` GUI Layout
* **Requirement**: dockable ImGui windows: Source (pio listing, PC markers per SM, breakpoint gutter),
  State machines (the `[SPEC-DBG-02]` fields), FIFOs, Pins / Logic analyzer, Timeline, Stimulus,
  Controls (step clock / step instr / run / back / reset, cycle counter), Console (Python).
  The layout is saved per project.

## 5. Milestones

| M | Content | Done when |
| :--- | :--- | :--- |
| M0 | split from AbstractX: RTL, programs, tests, uv project | cocotb 39/39 in this repo (done 2026-10-29) |
| M1 | `libpio2sim.so` + `.vlt` public signals + ctypes `Session`; CLI step/run | pytest: conformance programs stepped by clock match cocotb |
| M2 | GUI: controls, source, SM state, FIFOs | load blink/uart_tx, step, see PC/registers change |
| M3 | logic analyzer, timeline, FST/JSON export | DShot frame measured between cursors |
| M4 | breakpoints/watchpoints, reverse step | reverse-step test bit-identical |
| M5 | stimulus generators | ESC serial and DShot reply driven from the GUI |
| M6 | diff mode against a reference emulator | first divergence shown on a seeded bug |
