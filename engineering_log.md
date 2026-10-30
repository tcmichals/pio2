# pio2 Engineering Log

Format and categories as in AbstractX `engineering_log.md`. Earlier PIO history (2026-10-25 to
2026-10-27, EL-1027-NN) is in AbstractX's log.

## 2026-10-29: Split from AbstractX; SpecTrace format; host link and debugger specified

* **Category**: `process-gap` (repository structure)
* **What**: the PIO tile, programs, tests and tools moved from AbstractX `rtl/pio` (`docs/provenance.md`).
  AbstractX uses pio2 as its `rtl/pio` submodule.
* **Decisions (project owner)**: pio2 is the simulation home of the PIO; the debugger runs the RTL
  (Verilator) with a Wokwi-style parts canvas (imgui-bundle); tracing is barectf / CTF; the host link
  is a small NIC (SPI / Dual-SPI, 128-byte CRC32 frames, channels, `ISR` / `IMR`); inside, Wishbone B4
  only; a DMA engine paced by the PIO FIFOs; AXI only as the Zynq host adapter; on the Tang boards the
  PIO replaces the hardwired DShot / ESC / serial cores.
* **Found**: `pio2-cocotb` first put oss-cad-suite's `bin` ahead of the venv, so `make` used
  oss-cad-suite's own cocotb (Python 3.11) against the venv's 2.1.0 and 3 of 5 suites failed with
  `'RegressionManager' object has no attribute '_running_test'`; the venv now comes first (`tool-bug`).
  `test_asp_pio_dshot_cocotb.py` claimed `[SPEC-PIO-12]` but the only `@impl` tag sat on the GCR tables
  it never named; the tag now marks `dshot_frame` and `decode_samples`, which it calls.
* **Evidence**: `uv run pio2-cocotb` 5/5 suites, 39 tests; SpecTrace on pio2 (from AbstractX tools):
  45 requirements, 0 broken, 0 drift, 0 unbacked `@verifies`, 6 skills, 0 skill problems.
* **Not verified**: `pio2-programs --check` (pioasm not installed here); no board run; no evidence
  ledger yet (`Complete 0/45`: most requirements are spec only).

## 2026-10-29: Tile register port moved from Wishbone to AXI4-Lite

* **Category**: `process-gap` (bus change), `false-alarm` (one test timing)
* **What**: `asp_pio_tile` now has an AXI4-Lite slave port ([SPEC-AXI-04]): a front end turns each
  transaction into exactly one request on the existing register logic (unchanged), reads and writes
  alternate, a new request waits for the previous response, partial `WSTRB` answers `SLVERR`. All tile
  tests use cocotbext-axi's `AxiLiteMaster` (`reg_write` / `reg_read`).
* **Found**: `test_tlp_packetizer_and_trace_stream` started watching the TLP output only after the
  trigger write returned; with AXI4-Lite the write returns a few clocks later and the trace packet had
  already gone by. The test now collects from before the trigger (the RTL was right).
* **New suite** `test_asp_pio_axil_cocotb.py`: 1000 random accesses with random stalls on all five
  channels against a model; AW/W in any order; SLVERR on a partial write with no change; exactly-once
  side effects (SM0 echoes 4 TX words into RX, read back in order, RX empty; 200 rounds). Shown to fail
  on two mutants: partial writes applied (1 test fails) and a read re-issued after its answer (all 4
  fail; the 20 ms test timeout turns the resulting hang into a failure).
* **Also**: `pio2-cocotb --timeout` (default 900 s per suite) so a hung suite fails instead of blocking.
* **Not verified**: formal on the port (ZipCPU properties not fetched yet); AbstractX does not build
  against this tile until its wrapper adds a Wishbone-to-AXI4-Lite bridge.

## 2026-10-29: First formal check: ZipCPU faxil_slave on the tile's AXI4-Lite port

* **Category**: `process-gap` (formal added)
* **What**: `tests/formal/pio_tile_axil.sv` binds `faxil_slave` (third_party/wb2axip, Apache-2.0) to
  `asp_pio_tile`'s AXI4-Lite port (async reset option); `pio_tile_axil.sby` has `bmc` and `prove` tasks.
* **Result**: `sby -f pio_tile_axil.sby bmc`: PASS, depth 24, 18 s (smtbmc yices, oss-cad-suite 20260614).
  Mutant (BVALID dropped without BREADY): FAIL at step 21, `faxil_slave.v:340`.
* **Not closed**: `prove` (k-induction) was still trying induction at step 3 after ~25 minutes and was
  stopped. Induction over the whole tile needs invariants that tie faxil_slave's outstanding counters to
  the front end's held / valid flags (and probably a smaller harness that cuts the state machines off).
  Until then the AXI4-Lite port is bounded-checked to 24 clocks, not proved.

## 2026-10-29: Event FIFO built; core boundary and host adapters made explicit; trace through the DMA

* **Category**: `product-bug` (design, caught before merge), `process-gap` (spec)
* **What**: `src/rtl/trace/pio2_event_fifo.sv` ([SPEC-TRC-01]) on Forencich `axis_fifo` (submodule,
  unmodified): one holding slot per producer, fixed priority, drop and count, `DROPPED` record, 32-bit
  type mask, output stage that keeps TVALID / TDATA stable until TREADY.
* **Found (design bug)**: the first rule wrote the `DROPPED` record before any waiting record. Under
  overload a drop happens every clock, so every FIFO write became a `DROPPED` record, the slots starved
  and the losses grew: 396 records delivered for 3544 lost in the random test. `DROPPED` is now the
  lowest priority (written when no slot waits); its timestamp marks the gap and the host sorts by time.
  The random test now asserts DROPPED records cannot crowd out real ones; the old rule fails it
  (2 of 5 tests), as does a mutant that never counts drops (2 of 5).
* **Found (test bug)**: the drop test read `o_dropped_total` one clock before the last drop registered.
* **Found (spec gap, project owner)**: the spec described the Zynq as "a second crossbar master" with
  the AXI DMA "on the ring ports", but never defined one core boundary; the diagram drew the SPI link
  as part of the main path, and it did not say how channels or the trace reach a Zynq. Now
  [SPEC-ICN-11] defines `pio2_core`'s boundary (one AXI4-Lite slave, AXI-Stream in / out with the
  channel in `TDEST`, one IRQ); the SPI link and the Zynq PS (AXI MCDMA) are alternative adapters; the
  trace goes through a DMA trace channel into ring 1 ([SPEC-TRC-02], owner's proposal), so there is no
  separate trace packer. [SPEC-LINK-08] added: outbound frames merged whole, channel-0 replies first.
* **Docs**: `docs/overview.md` with six Mermaid diagrams (system, SPI adapter, Zynq adapter, core, pin
  mapping per state machine, trace path); every ASCII diagram in the repo replaced by Mermaid.
* **Not verified**: the Mermaid diagrams were not rendered here (no Mermaid CLI); check on GitHub.

## 2026-10-29: Debugger M1 / M2: Verilator model, Session, GUI

* **Category**: `product-bug` (debugger, caught before merge), `tool-bug` (environment)
* **What**: `libpio2sim.so` (Verilator `--vpi`, `.vlt` `public_flat_rd`, C ABI in
  `src/pio2/debug/sim/pio2sim.cpp`), `pio2.debug.Session`, `pio2-debug` CLI, `pio2-gui`
  (imgui-bundle, ImPlot), parts (LED, button, UART terminal), four example sessions.
* **Found 1**: register accesses clock the model, but those clocks were not recorded, so the trace had
  gaps and the UART terminal decoded garbage whenever the feed wrote the TX FIFO mid-character. Every
  clock is now queued in the C model and drained into the trace (`test_trace_has_no_gaps`).
* **Found 2**: `step_instr` checked "SM enabled" with an AXI register read, which itself runs 3+
  clocks, so one "instruction step" ran several instructions and a test waiting for a PC could loop
  forever. It now reads `sm_en` through VPI; `test_step_instr_runs_one_clock_at_divider_1` fails on
  the old code (2 tests fail) and passes on the new. The GUI's single-step buttons no longer run the
  stimulus feed, so "Step clock" is exactly one clock.
* **Found 3**: `Session.pull` was both an attribute (pad pull level) and the RX FIFO read method.
* **Found 4 (test)**: the first stalled-`pull` test expected the PC to stay at 0 after `push`; the
  push's own register-write clocks let the pull complete. The test now steps a stalled SM with a
  bounded limit instead.
* **Tool facts**: ImGui 1.92 `add_rect(p_min, p_max, col, rounding, thickness, flags)` (argument order
  changed); `implot.plot_digital` drew nothing in imgui-bundle 1.93 here, so the logic analyzer uses
  stair lanes; hello_imgui writes `<title>.ini` in the working directory and it overrides the coded
  docking layout (now `ini_disable`); a window that is not visible renders at about 1 frame per second
  under this compositor, so screenshots use `--warmup` instead of many frames.
* **Evidence**: `pytest tests/test_debug_session.py` 13 passed; screenshots `docs/images/gui-*.png`.
* **Not verified**: the debugger's per-clock state against the cocotb conformance runs; the GUI on
  another desktop / X11; interactive use (only scripted screenshots were taken here).

## 2026-10-29: Every RP2040 opcode and operand checked on the RTL; built-in assembler

* **Category**: `process-gap` (verification), `false-alarm` (test expectations)
* **What**: `tests/test_isa_every_opcode.py` (project owner: "we need all the opcode checks"): 384 cases
  over JMP (8 conditions), SET (4 destinations), MOV (7 destinations x 3 ops x 7 sources), IN (6
  sources, counts, both shift directions), OUT (8 destinations incl. PC and EXEC), PUSH / PULL (all
  option combinations), WAIT (gpio, pin, irq, both polarities, rel), IRQ (set, nowait, clear, wait,
  rel), side-set (opt, pindirs) and delay, on the Verilator model, each compared with the datasheet
  rule. A last test fails if any combination has no passing case. Instructions are encoded by the new
  `pio2.asm`, which re-assembles all 30 checked-in pioasm images identically.
* **Mutants**: `jmp x!=y` inverted (6 failures), `mov status` off by one (7), `mov ::` not reversing (35).
* **False alarms (test)**: not-taken `jmp` through `SMx_INSTR` leaves the PC alone (the instruction is not
  from memory, as `pio_sm_exec` relies on); `mov exec` / `out exec` run the built instruction on the SM's
  next cycle, so those cases run in an enabled program.
* **Not verified**: the same cases under cocotb / Icarus (a second simulator); RP2350-only features
  (not a goal, project owner 2026-10-29).

## 2026-10-29: PIO tracing in the RTL: observation port, trace tap, pio2_core; Trace window

* **Category**: `process-gap` (feature), `false-alarm` (one test)
* **What**: the tile gained a read-only observation port (per-SM PC, stalled, TX empty, RX full; IRQ
  flags); `pio2_trace_tap` turns its edges into [SPEC-TRC-01] records; `pio2_core` = tile + tap + event
  FIFO. The debugger now simulates `pio2_core` and shows the records (Trace window, stall markers on
  the PC plot). Records are checked against the per-clock trace: 1000 blink rounds, every stall begin /
  end at the same clock and PC; IRQ set / clear; FIFO edges; type mask; back-pressure drops counted
  and reported by a DROPPED record.
* **Found**: stall-end records first carried the next PC; spec now says the PC of the stalled
  instruction. The record timestamp is a constant 2 clocks after the per-clock state (edge detect +
  counter start), corrected once in `Session` (`TAP_LATENCY`) and checked by the tests.
* **False alarm (test)**: the drop test read the drop counter while the program still ran.
* **Evidence**: pytest 441 passed; `pio2-cocotb` 7/7 (48 tests) with the new tile ports; tap mutant
  (no RX-full edges) fails `test_fifo_edges`.
* **Not verified**: the tap and core under formal; the records through the DMA trace channel (no DMA
  yet); AbstractX lint with the new unconnected tile outputs (checked in the AbstractX commit).

## 2026-10-30: Tile split: TLP path out, AXI-Stream FIFO ports in [SPEC-ICN-09]

* **Category**: `product-bug` (found by the new test), `hardware-fact`, `tool-bug`, `process-gap`
* **What**: `asp_pio_tile` lost `asp_pio_tlp_ingress` / `asp_pio_tlp_shim`, their registers (`0x0C`,
  `0x90..0x98`), the `SMx + 0x1C` trigger and `i_sys_timestamp`; they go to AbstractX's wrapper. It
  gained per-SM AXI-Stream TX / RX ports on the FIFOs and `o_obs_active`. SPEC-PIO-04/07/10 left
  `docs/SPECIFICATION.md` (AbstractX owns them now); SPEC-PIO-06 (IMU auto-DMA) now runs on the DMA
  engine.
* **Product bug (new code path, caught before commit)**: a register `RXFx` read captured the head word
  in its decode clock but popped it one clock later. With the RX stream port running, the stream
  could take that head word in between: the register read returned a word the stream also got, and
  the late pop dropped the next word unseen. Now the register pop is combinational, in the clock the
  data is captured, and the stream is held off in exactly that clock.
  `test_register_and_stream_share_fifo_exactly_once` failed on the registered pop ("a register read
  lost or reordered its word") and passes now.
* **Hardware fact**: the SM's TX FIFO takes a write while full if the SM pulls in the same clock
  (`asp_pio_sm.sv`, level after the pull), but `o_tx_ready` is the level before the pull. So the stream
  `TREADY` is conservative, and a register write to a "full" FIFO may still land. A monitor that counts
  pushes with `ready && valid` undercounts; the test watches the write pointer instead.
* **Tool bug**: `build/sim/obj` kept Verilator make dependencies with absolute paths from an older
  checkout location (`/home/...`), so the first rebuild after an RTL change failed ("No rule to make
  target"). `pio2.debug.build` now deletes `obj/` before each rebuild.
* **Process gap**: `git stash` (like `git status` without `--ignore-submodules=all`) hangs on this disk
  because of the submodules; a hung stash had to be killed. Do not use it here.
