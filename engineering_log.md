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

## 2026-10-30: DMA engine, rings, crossbar and core registers; pio2_core boundary [SPEC-DMA-*] [SPEC-ICN-11]

* **Category**: `process-gap` (feature), `false-alarm` (tests), `tool-bug`, `hardware-fact`
* **What**: `pio2_dma` (eight PIO channels + trace channel 1, registers at 0x1000, routing to the tile's
  stream ports, TDEST split in, Forencich `axis_arb_mux` out), `pio2_dma_chan` (ring = Forencich
  `axis_fifo` with an exact occupancy counter, modes, counters, events, out packetizer),
  `pio2_axil_regs` (one AXI4-Lite front end for our register blocks), `pio2_core_regs` (trace and core
  windows, ISR / IMR), Forencich `axil_crossbar` in `pio2_core`. The debugger simulates the new core:
  trace records come from channel 1 of the output stream; Session gained stream_push / stream_out.
* **Design decisions (spec updated first)**: one BRAM FIFO per ring instead of a shared ring RAM (no
  arbitration, exact level); DIRECT still passes through the ring (so the core's output stays
  AXI-Stream legal when the register port reads the same RX FIFO); a full ring is offered as a block
  at once (a ring smaller than a frame otherwise waits for FLUSH and counts overruns); RESET_RING waits
  for an out block in progress; STATUS.DONE is a mirror of DMA_DONE; TRACE_MASK resets to 0.
* **Hardware fact**: Forencich's `axil_crossbar` decodes one address at a time per direction: about 4
  clocks per register transaction through the core; `pio2_axil_regs` alone takes one per clock.
  `axis_fifo`'s `status_depth` leaves out the words in its output stage, so it cannot be the ring level.
* **False alarms (tests)**: cocotbext-axi streams without TKEEP default to byte lanes (words came out
  as bytes; `byte_size=32` fixes it); the IMU model left DRDY at the pull-up level before it started,
  so the PIO ran one burst without a trigger edge (20 bursts, 19 triggers); a model register block
  updated i_rdata one clock late; an overrun count assumed no flicker when the reader resumes.
* **Tool bug**: Verilator in cocotb stops on third-party lint warnings (ASCRANGE, CASEINCOMPLETE in
  `axil_crossbar_*`); `src/rtl/third_party.vlt` turns lint off for `third_party/` only. A single big
  `always_comb` mixing both directions of the routing looked like a combinational loop (UNOPTFLAT);
  split per direction.
* **Evidence**: cocotb 10/10 suites (new: dma 9, core 6, axil_regs 3); pytest 442 passed;
  `sby pio2_axil_regs.sby bmc` and `prove` (k-induction) pass; two mutants (BVALID dropped without
  BREADY, ARREADY while RVALID pending) fail on ZipCPU's own properties with the invariants removed.
* **Not verified**: AXI-Stream ports under faxis properties; Icarus for the new suites; synthesis.

## 2026-10-30: k-induction closes on the tile's AXI4-Lite port [SPEC-AXI-04]

* **Category**: `process-gap` (proof was bounded only), `false-alarm` (the 25-minute "slow" induction)
* **What**: `tests/formal/pio_tile_axil.sv` gained five formal-only assertions (`ifdef FORMAL`), all on
  the tile's ports, none in the RTL: faxil_slave's outstanding counters equal "held + response
  pending", i.e. `f_awr == !AWREADY + BVALID`, `f_wr == !WREADY + BVALID`, `f_rd == !ARREADY + RVALID`
  (the front end's `aw_held` / `w_held` / `ar_held` are exactly the inverted READY outputs), and the
  responses are OKAY / SLVERR (B) and OKAY (R). No invariant on `req_busy` / `wb_ack_o` was needed:
  an unreachable internal state turns into a violation of the port invariants within 3 clocks.
* **Why the old run never finished**: without the invariants the solver keeps finding unreachable
  starting states (e.g. `aw_held` set with no AW counted), so induction fails at every depth and each
  step only gets slower; it was not a size problem. Shown: the same run with the five assertions
  compiled out ends UNKNOWN after 7 min (induction counterexample from an unreachable state:
  `faxil_slave.v:598`, read counter at its limit with ARREADY high). With them: `sby -f pio_tile_axil.sby prove`
  PASS, induction closes at k = 8 (smtbmc yices, "Trying induction in step 32 .. Temporal induction
  successful" at depth 40), whole design (all four SMs), 116 s wall, of which the induction takes ~5 s
  and the base case (BMC 40) the rest.
* **Depth raised 24 -> 40** for both tasks: faxil_slave holds reset for 16 clocks, so depth 24 left 8
  clocks of traffic, too few for two reads in a row. Mutant "read starts while RVALID is pending"
  (`read_ready = ar_held`) PASSED the depth-24 base case and only showed up as an induction
  counterexample (UNKNOWN after 8 min); at depth 40 it FAILs the base case at step 25 in 30 s. Lesson:
  check that the BMC depth minus the reset length covers two back-to-back transactions.
* **Mutants (prove, depth 40)**: BVALID dropped without BREADY (`if (s_axil_bvalid) s_axil_bvalid <=
  0`): FAIL, base case step 20, 42 s. Read issued while RVALID pending: FAIL, base case step 25, 30 s.
  `bmc` depth 40 on the unmodified tile: PASS, 99 s.
* **Not verified**: the AXI-Stream FIFO ports under `faxis_*` (not bound yet); liveness beyond
  faxil_slave's 12-clock MAXDELAY / MAXWAIT bounds.

## 2026-10-30: Icarus as the second simulator; the ISA cases as a cocotb suite [SPEC-PIO-02]

* **Category**: `tool-bug` (2), `product-bug` (portability), `process-gap`
* **What**: the RP2040 opcode / operand cases moved out of `tests/test_isa_every_opcode.py` into
  `src/pio2/isa_cases.py` (case table, datasheet rules, coverage set), written against a small blocking
  `hw` interface. The pytest file runs them on the debugger's Verilator model as before; the new
  `tests/cocotb/test_isa_every_opcode_cocotb.py` runs the same 383 cases + coverage gate through the
  AXI4-Lite port (cocotbext-axi) on Icarus and Verilator, each case in a cocotb `bridge` thread with
  every hardware access `resume`d. All 8 cocotb suites now pass on both simulators.
* **Portability (RTL)**: Icarus rejected `asp_pio_tile.sv` (`Unable to bind wire/reg/memory
  sm_stalled ... Check for declaration after use`): `o_obs_stalled` / `o_obs_active` were assigned
  before `sm_stalled` / `sm_active` were declared. Verilator and Yosys accept that; IEEE 1800 does not.
  The two `assign` lines moved below the declarations (no other change).
* **Tool bug (flags)**: the Makefile passed Verilator's `-Wno-*` options to every simulator; they now go
  to Verilator only, Icarus gets `-I` and `-G` parameters as `-P<top>.<name>`.
* **Tool bug (oss-cad-suite)**: its `bin/vvp` is a wrapper that runs vvp on oss-cad-suite's own libc /
  libm; cocotb's VPI library then loads the system libpython, which needs the system glibc, and vvp
  stops with "GLIBC_2.38 not found". `pio2-cocotb --sim icarus` now points cocotb (`ICARUS_BIN_DIR`)
  at the real binary `libexec/vvp`, which runs on the system libraries.
* **Bench differences found**: Icarus starts with X on the AXI outputs, so cocotbext-axi must be
  created after a clock in reset ("Cannot convert Logic('X') to bool" otherwise); state is read after a
  falling edge, so both simulators see the values after the last rising edge.
* **Case changes (both benches)**: the 4 `out exec` cases with fewer than 16 bits were skips and are
  left out of the table (383 cases, was 387 incl. 4 skips). The delay case now holds the program on
  `wait 1 gpio 0` and counts the clocks X reads 1 (exactly delay + 1), instead of reading the
  debugger's PC trace, which the cocotb bench does not have. `mov` operands come from a fixed CRC
  seed (they came from Python's `hash()` of strings, which changes per process). `reset` also
  releases the externally driven pins (the delay case found a pin left high by the previous case).
* **Evidence**: pytest 441 passed; `pio2-cocotb --sim icarus` 8/8 (ISA 384/384); `pio2-cocotb` 8/8
  (ISA 384/384); mutant `jmp x!=y` inverted fails the cocotb suite on Icarus with 6 failures (5 cases +
  coverage gate), as in pytest.
* **Not verified**: Icarus 12 (Ubuntu's `/usr/bin`) — only oss-cad-suite's Icarus 13 was run; the
  other two pytest mutants (`mov status`, `mov ::`) were not re-run on the cocotb suite.

## 2026-10-30: Link frames on the host; trace records to CTF 1.8

* **Category**: `process-gap` (feature), `tool-bug` (barectf, babeltrace2), `false-alarm` (one test)
* **What**: `src/pio2/host/frame.py` ([SPEC-LINK-02]): frame encode / decode, CRC-32/ISO-HDLC written out
  (equal to `zlib.crc32`), rejects (bad CRC, length, oversize, reserved, unknown op), transfers split
  into STREAM_DATA / REG_WRITE / REG_READ frames. Op codes REG_READ..NAK = 0x01..0x06 chosen here.
  `src/pio2/trace/ctf.py` + `barectf_config.yaml` ([SPEC-TRC-02]): records -> 64-bit timestamps
  (nearest the previous record, +-2^31, so wraps and the event FIFO's priority reordering both work)
  -> sorted -> CTF 1.8 (`metadata` + `stream_0`), read back with babeltrace2 2.0.5, also end to end
  from a debugger Session (blink + IRQ toggler + a back-pressure stretch with a DROPPED record).
* **Tool facts**: barectf 3.1.2 (latest on PyPI) does not install on Python 3.13: it pins PyYAML 5.4.1,
  which has no wheel and no longer builds. So the YAML is checked against `ctf.EVENT_TYPES` by a test,
  not by `barectf generate`. babeltrace2 reports a discarded-events count only as the difference
  between two packets' `events_discarded`; a non-zero value in the first packet prints only "may have
  discarded events", so the writer starts with an empty packet (snapshot 0). The warnings go to stderr.
* **Tool fact (environment)**: a `uv run` killed during its first sync left this worktree's `.venv` with
  a half-installed pygments (pytest failed on `pygments.plugin`); `uv sync --reinstall-package pygments`
  fixed it. With several agents running uv at once, `uv run` waited minutes on the shared lock.
* **False alarm (test)**: `blink` pulls once only (its wrap target is after the `pull`), so pushing
  more words gives no more stall records; the end-to-end test uses an IRQ toggler for volume.
* **Mutants**: decode without the length check (2 failures); `ordered` without sorting (2); timestamp
  extension without wrap handling (2).
* **Evidence**: `pytest tests/test_host_frame.py tests/test_trace_ctf.py` 46 passed.
* **Not verified**: the RTL framer and DMA trace channel (not written); Trace Compass and AbstractX
  Studio opening the CTF; `barectf generate` on the YAML.

## 2026-10-30: Evidence ledger for pio2 (`pio2-trac`), so the matrix can say VERIFIED

* **Category**: `process-gap` (TODO item 7), `false-alarm` (rtl-lint skill command)
* **What**: `src/pio2/trac.py` (`uv run pio2-trac [--record] [--formal] [--lint] [--no-cocotb]`) runs pytest
  (JUnit XML, per file) and `pio2-cocotb` (per suite), optionally `sby ... bmc` per `tests/formal/*.sby`
  and the RTL lint, maps every test file to its `@verifies` tags and writes one AbstractX-format
  ledger record per requirement to `docs/verification/evidence/<YYYY-MM>/<host>.jsonl`; AbstractX's
  `spectrace_matrix.py --matrix` reads them unchanged. A record is `pass` only when every skill on
  the requirement's `Verified by` line ran for it and passed; a skill that did not run (board,
  fpga-fit, a missing cocotb suite) gives `skip` = STALE. `--record` refuses a dirty tree, because
  the record names HEAD and AbstractX's matrix does not look at the record's `dirty` field.
* **Found**: the `rtl-lint` skill command globbed `src/rtl/*.sv` only, so `pio2_core` failed with
  MODMISSING (its tap and FIFO are in `src/rtl/trace/`, `axis_fifo` in `third_party/`); the skill and
  `pio2-trac --lint` now pass `-y` for those directories (7 modules clean).
* **Found**: `[SPEC-TRC-03]` is BROKEN in the matrix: its `Verified by` note has `` `pull` `` in
  backticks outside the parenthesis-stripped part, read as a skill name.
* **Evidence**: `pytest tests/test_trac.py` 5 passed (incl. a throw-away git repo whose records the
  real AbstractX matrix shows as VERIFIED 1 / STALE 1 / FAILING 1, then STALE after an impl change);
  mutant (missing skills judged pass) -> 3 failed.
* **Not verified**: records from a second machine merged with these (file per host, by design).

## 2026-10-31: CTF end-to-end test fails on the merged tree; trace drops under-reported (open)

* **Category**: `product-bug` (suspected, open), `process-gap` (branch test written against the old core)
* **What**: after merging `worktree-agent-a1f543a2c9553e1ee`, `tests/test_trace_ctf.py::test_debugger_session_records_to_ctf`
  fails once babeltrace2 is installed (it skipped before). The test was written against the old
  pio2_core trace record port; main now sends trace on channel 1 of the core's output stream.
  1. `len(evs) > 2000` fails: 1930 records (threshold was tuned to the old core).
  2. With that relaxed, `len(dropped) == 1` fails: 46 DROPPED records. That is allowed by
     [SPEC-TRC-01] (a DROPPED record goes out in any clock with no slot waiting; the IRQ toggler leaves
     idle clocks), so the "exactly one" check is a test assumption, not a requirement.
  3. The requirement that does hold is that DROPPED counts add up to the drop total. They do not:
     sum of DROPPED `data` 1835 vs `trace_counters()` lost 2040; after 500 more clocks with
     `type_mask = 1 << 0x1F`: 1900 vs 2042. The 142..205 missing losses are not pending counts.
     Suspects: a DROPPED record itself lost when the FIFO output / stream is back-pressured
     (`pio2_event_fifo.sv` writes `out_valid` on `take_drop`), or losses counted in
     `o_dropped_total` that never enter `drop_cnt`.
* **Evidence**: `uv run pytest tests -q` 492 passed, 1 failed (this test); probe script in the session
  scratchpad (blink SM 0, `irq set 0 / irq clear 0 [2]` SM 1, 5000 clocks with the reader stalled).
* **Not verified**: root cause; whether the old core had the same gap (the test never checked the
  sum); cocotb coverage of DROPPED accounting under output back-pressure.

## 2026-10-31: Verilator UNOPTFLAT in Forencich priority_encoder stops the pio2_core / pio2_dma builds

* **Category**: `tool-bug` (waiver gap), `false-alarm` (the loop is not real)
* **What**: `uv run pio2-cocotb` failed to build test_pio2_core_cocotb and test_pio2_dma_cocotb:
  `%Warning-UNOPTFLAT` on `priority_encoder.v` `stage_valid` / `stage_enc` (via `pio2_dma.u_out_mux`),
  "Exiting due to 2 warning(s)". `src/rtl/third_party.vlt` had only `lint_off -file "*/third_party/*"`,
  which does not cover UNOPTFLAT. Added `lint_off -rule UNOPTFLAT -file "*/third_party/*"`.
* **Evidence**: before: both suites "did not build or run"; after: pio2_core 6/6, pio2_dma 9/9
  (Verilator 5.049, oss-cad-suite).
* **Not verified**: why the 2026-10-30 handoff recorded 11/11 on the same binary; full Verilator and
  Icarus runs with the fix.

## 2026-10-31: pio2 commit made inside AbstractX's rtl/pio checkout, never pushed; AbstractX pinned it

* **Category**: `process-gap`
* **What**: e856f24 (Zynq adapter = own ring DMA, [SPEC-ICN-11], docs only) was committed in
  `AbstractX/rtl/pio` and AbstractX 9c902a4 pinned the submodule to it; it was never pushed, so a clean
  AbstractX clone failed `git submodule update` ("not our ref"). Recovered by fetching it from the
  AbstractX checkout into pio2, pushing branch `zynq-ring-dma`, merging into main (a3859f2).
* **Rule**: before AbstractX commits a new `rtl/pio` pin, the pinned SHA must be on
  `origin` (`git -C rtl/pio branch -r --contains <sha>` non-empty); pio2 work is committed in the pio2
  repo, not in the submodule checkout.
* **Evidence**: `git ls-remote origin zynq-ring-dma` = e856f246...; `git merge-base --is-ancestor
  e856f24 main` = yes after the merge.
* **Not verified**: a clean AbstractX clone + `git submodule update --init rtl/pio` after the push.

## 2026-10-31: Icarus shows X problems Verilator hides; Betaflight passthrough facts

* **Category**: `product-bug` or test bug (open), `hardware-fact`
* **What (Icarus)**: first full Icarus run of the merged tree: 8 suites pass (ISA 384/384), but
  `test_pio2_axil_regs_cocotb` fails 2 of 3 with `ValueError: Can't convert LogicArray to int: it
  contains non-0/1 values` on `dut.o_raddr` (line 53), `test_pio2_core_cocotb` hangs (900 s timeout),
  `test_pio2_dma_cocotb` did not run. Verilator is 2-state, so it never sees X. Root cause not found.
* **What (Betaflight, d593400a0)**: `esc4wayInit()` disables motors and makes every motor line an input
  held high; one ESC is selected per 4-way command. `MSP_SET_PASSTHROUGH` does not check ARMED; the
  configurator (e301160) does not send `MSP_SET_ARMING_DISABLED` (99), it relies on `motorDisable()`.
  Consistent with `[SPEC-PIO-13]` (disarmed, other wires input, one ESC at a time).
* **Evidence**: `uv run pio2-cocotb --sim icarus` log (session scratchpad); `serial_4way.c:139-158`,
  `msp.c:293-350`, `msp.c:4150-4170`; configurator `ui_main.py:517`, `async_worker.py:297-333`.
* **Not verified**: why `o_raddr` is X; whether pio2_core hangs or is only slow on Icarus;
  rt-fc-offloader reference RTL (not on this PC).

## 2026-10-31: ESC passthrough checked against rt-fc-offloader; MSP sniffer on the Linux host; 5-SM hot switch

* **Category**: `process-gap` (design decisions), `hardware-fact` (reference behaviour)
* **What**: read github.com/tcmichals/rt-fc-offloader `22fce99` (`rtl/io/wb_serial_dshot_mux.sv`,
  `rtl/io/wb_esc_uart.sv`, `python/hw/test_hw_esc_passthrough.py`). It agrees with [SPEC-PIO-13]: only
  the selected motor pad is driven in serial mode, the others released; break = selected pad forced
  low (50 ms in the hardware test); one-clock tristate on any mode or channel change; guard one bit;
  RX ignored while transmitting; start bit rechecked at its middle; 19200 baud default.
* **Differences**: the reference has an MSP sniffer in RTL (`$M<`, size, then command 0xF5 or 0x64)
  and its 5 s watchdog covers only that sniffed entry, not manual mux writes. Project owner: the
  sniffer belongs in the Linux Python app (the MSP stream ends there anyway), added to [SPEC-PIO-13]
  as proposed: entry on MSP 245 (empty or mode 0xFF), refused while armed, the app takes Betaflight's
  4-way interface role, exit on 0x34 or 5 s, MSP_IDENT does not enter.
* **Decision**: ESC / motor testing loads no RC decode or NeoPixel tiles, so passthrough uses five
  SMs: four DShot on the motor tile plus one `esc_serial` on a spare switch tile, kept loaded; the
  hot switch is pad-owner writes ([SPEC-PIO-17]), no program load. Reload stays as a fallback.
* **Process gap (mine)**: in the same session I committed another agent's uncommitted AbstractX
  edits to a branch and switched back, which removed them from its working tree for about a minute;
  restored exactly, branch `wip/e906-rpmsg-after-crash` left for that agent to delete. Rule: a pio2
  session does not change ~/projects/AbstractX's working tree.
* **Process gap (checkouts)**: two pio2 checkouts on two disks. Work before the crash was in the USB
  copy (`/run/media/tcmichals/projects/pio2`); this session opened `~/projects/pio2`, 24 commits behind,
  and nothing said so until `git status -sb`. Both are clones of origin, so nothing was lost; the home
  copy was fast-forwarded. Rule: check `git status -sb` against origin at session start.
* **Tool fact**: bare `pytest` collects `third_party/verilog-axi/tb` and stops on an import error; run
  `pytest tests`.
* **Evidence**: spectrace `--check-drift` 0, `--check-skills` 0 problems; `pytest tests` in
  ~/projects/pio2 492 passed, 1 failed (`test_trace_ctf.py::test_debugger_session_records_to_ctf`,
  1930 events where > 2000 expected; not diagnosed, possibly TODO item 1).
* **Not verified**: none of the new text is implemented (`src/pio2/host/msp_passthrough.py`, the
  pad-owner mux); nothing run on hardware.

## 2026-10-31: The PIO tile does not fit the Tang Nano 9K or 20K; no Fmax yet

* **Category**: `product-bug` (area), `process-gap` (synthesis-only check), `tool-bug` (Vivado on Ubuntu 26.04)
* **What**: AbstractX `main` (c43d37f, rtl/pio at e856f24) built from a scratch clone with oss-cad-suite
  (Yosys `synth_gowin`, nextpnr-himbaechel). Tang Nano 9K (`make -f hw/Makefile build_hw/tang9k_pnr.json`):
  LUT4 22609 / 8640 (261 %), DFF 9170 / 6480 (141 %), placement fails. Tang Nano 20K
  (`tang_nano20k_fit`): LUT4 22970 / 20736 (110 %), DFF 9214 / 15552 (59 %), "Unable to find legal
  placement". The plain `tang_nano20k` target stops earlier on an unconstrained `spi_sclk` IO.
* **Cause (located, not fixed)**: the tile alone (pio2 `main`, `asp_pio_sm.sv` + `asp_pio_tile.sv`,
  `synth_gowin` flattened): LUT1 4410, LUT2 3708, LUT3 7407, LUT4 13711, MUX2_LUT5..8 13348, ALU 1195,
  DFF ~3440, RAM16SDP4 40. With `-noflatten` each `asp_pio_sm` is ~5600 - 6000 LUTs. Which construct
  costs it is not yet known (suspects: the single always_ff with blocking locals duplicating the
  shifters / rotators per instruction path, the 8-word FIFO kept in flip-flops).
* **Process gap**: the 2026-10-29 note "Tang Nano 20K synthesis ok" was synthesis only; place and route
  was never run, so the overflow went unseen. `fpga-fit` must run place and route, not only Yosys.
* **Tool fact**: Vivado 2025.2 (`~/hdisk/tools/AMD/2025.2`) hangs at start on Ubuntu 26.04
  (`libncurses.so.5` missing). Works with a private dir of symlinks `libncurses.so.5 ->
  /usr/lib/x86_64-linux-gnu/libncurses.so.6`, `libtinfo.so.5 -> libtinfo.so.6` on `LD_LIBRARY_PATH`.
  The Zynq-7020 out-of-context run of `pio2_core` (`xc7z020clg400-2`, 4 ns) was stopped during
  synthesis for low memory (~10 GB RAM, ~150 MB free); no Zynq result.
* **Not verified**: Fmax on any board; the per-construct cost inside `asp_pio_sm`.
