# pio2 TODO and Status

Updated 2026-10-31. Specs: `docs/SPECIFICATION.md` (tile), `docs/INTERCONNECT_SPECIFICATION.md`,
`docs/DEBUGGER_SPECIFICATION.md`. Lessons: `engineering_log.md`.

## Where we are

| Area | State |
| :--- | :--- |
| PIO tile RTL (`src/rtl/`) | moved from AbstractX; 39 cocotb tests pass (`uv run pio2-cocotb`) |
| Program library (`src/programs/`) | DShot bidir, ESC serial, conformance, pico-examples; images checked in |
| Python package (uv) | `pio2-programs`, `pio2-examples`, `pio2-setup-tools`, `pio2-cocotb` |
| SpecTrace format | done: 45 requirements, 0 broken / drift / unbacked; skills in `.agents/skills` |
| Interconnect (link, AXI4-Lite / AXI-Stream, DMA, trace, host adapters) | core built: tile, crossbar, DMA + rings, trace, core registers; SPI link and Zynq adapter next |
| Debugger / simulator / GUI | M1 and M2 working (`pio2-gui`, `pio2-debug`); M3-M7 specified |
| Tang Nano 9K / 20K tops | still in AbstractX (they build AbstractX's whole `asp_top`) |
| AbstractX integration | submodule `rtl/pio`; AbstractX 16/16 cocotb, Tang Nano 20K synthesis ok |

## After the crash (2026-10-31)

* **Work in `~/projects/pio2` and `~/projects/AbstractX`** (internal disk). The copies under
  `/run/media/tcmichals/projects/` (USB drive) were used before the crash; `~/projects/pio2` was 24
  commits behind until pulled (now level with origin). The USB AbstractX copy is 2 commits behind.
* **AbstractX has its own agent** (session `abstractx-38`): a pio2 session does not commit, stash or
  switch branches there. Its 10 uncommitted E906 RPMsg / tracer / UART files are backed up on
  `wip/e906-rpmsg-after-crash` (7347c34, on origin); that agent was asked to finish or discard them
  and delete the branch.
* **Run pytest as `uv run pytest tests`**: a bare `pytest` also collects
  `third_party/verilog-axi/tb` and stops on an import error.
* Evidence after the pull: `uv sync` ok; `pytest tests` 492 passed, 1 failed (item 2 below).

## Monday (handoff 2026-10-31)

Done 2026-10-31: `main` and both worktree branches pushed to origin; branch 1
(`worktree-agent-a1f543a2c9553e1ee`: host frames, CTF, pio2-trac) merged as db40f9d; uv reinstalled and
`.venv` rebuilt after the Ubuntu 26.04 upgrade; babeltrace2 installed; Verilator build fix
(`src/rtl/third_party.vlt`: `lint_off -rule UNOPTFLAT` for third_party; the rule-less line does not
cover it, Forencich `priority_encoder.v` false loop stopped the pio2_core and pio2_dma builds).

Evidence: pytest 492 passed, 1 failed; Verilator cocotb: 9 suites passed in the full run, pio2_core
6/6 and pio2_dma 9/9 after the waiver fix (full Verilator run with the fix not yet done).

To do, in order:
1. **Trace DROPPED under-count (suspected product bug, [SPEC-TRC-01])**. DROPPED records sum to fewer
   losses than the drop counter: 1835 vs 2040 (1900 vs 2042 after 500 more clocks with only DROPPED
   kept), so it is not just pending counts. Suspects in `src/rtl/trace/pio2_event_fifo.sv`: a DROPPED
   record lost when the output is back-pressured (`out_valid` on `take_drop`), or drops counted in
   `o_dropped_total` but not in `drop_cnt`. Write a cocotb test on pio2_event_fifo with output
   back-pressure that checks sum(DROPPED.data) == total and fails on the current code, then fix.
   Details: engineering_log.md 2026-10-31.
2. **Fix `tests/test_trace_ctf.py::test_debugger_session_records_to_ctf`** after 1: it was written
   against the old trace port. `len(evs) > 2000` (now 1930) and "exactly one DROPPED record" (now 46,
   allowed by [SPEC-TRC-01]) are stale assumptions; check sum(DROPPED) == lost and the CTF
   discarded-events count == lost instead. Do not relax it before 1 is understood.
3. **Icarus cocotb on the merged tree** (run 2026-10-31, oss-cad-suite Icarus 14.0 devel): 8 suites
   PASS (incl. ISA 384/384); `test_pio2_axil_regs_cocotb` 1/3 (`random_traffic_with_stalls_1000`,
   `partial_write_is_slverr_without_effect`: `int(dut.o_raddr.value)` on X; test samples outside a
   valid read, or the RTL leaves `o_raddr` X when idle: decide, fix, test fails on old code);
   `test_pio2_core_cocotb` hung (no result after 900 s, `--timeout`); `test_pio2_dma_cocotb` not run.
   Verilator is 2-state, so X problems only show here. Then a full `uv run pio2-cocotb` with the waiver.
4. **Unanswered**: why the 2026-10-30 handoff recorded Verilator 11/11 with the same binary
   (oss-cad-suite Verilator 5.049, installed 2026-06-14) that now needs the UNOPTFLAT waiver.
5. **Branch 1 follow-ups**: add [SPEC-VER-02] "evidence ledger", point [SPEC-TRC-02] Verified by at
   tests/test_trace_ctf.py, align [SPEC-DBG-13]; then `uv run pio2-trac --record --formal --lint` on a
   clean tree and commit the ledger; SpecTrace `--check-drift/--check-skills/--check-verifies`.
6. **Merge the debugger branch** `worktree-agent-aa5ce2bdeac89fae2` (hand-merge `session.py`,
   `pio2sim.cpp`; 99d587f untested), then the SPI link RTL review (below).
7. **Diff-mode reference emulator** `[SPEC-DBG-08]`: survey done 2026-10-31 (table in
   `docs/DEBUGGER_SPECIFICATION.md` §2, links in `docs/references.md`; READMEs only, no tool run).
   Decide: headblockhead/pio (Go, AGPL-3.0, proposed) or picoem (Rust); run the candidate on the
   conformance set before choosing; record under Decisions.
8. **Automated hardware testing** `[SPEC-VER-03]`: runner in `tools/hil/` that builds, loads (Tang:
   openFPGALoader to SRAM; QMTech Zynq: SSH + FPGA Manager + overlay, as AbstractX
   `hw/zynq7000/qmtech_zynq7020/tools/load_bitstream.py`; Pico: picotool), checks ID / VERSION /
   hash, runs the per-board list, captures pins (second Pico as PIO sampler, or sigrok), writes JSON
   evidence + pio2-trac records, NOT RUN for a missing board. Blocked on: pin mux `[SPEC-PIO-09]`
   (the tile cannot drive a pin yet, docs/boards.md), pio2 Tang / Zynq tops (still in AbstractX), SPI
   link RTL (untested). Decide the rig: which capture, which host for the Tang (A5E or Pico).
9. **Full cycle against RP2040 silicon** `[SPEC-VER-04]`: Pico test firmware that loads the same
   program words + config and replays stimulus; compare Pico / Verilator / Tang / Zynq pin traces at SM
   clock resolution; every difference becomes a failing cocotb test first.
10. **Reload time DShot <-> ESC serial on Tang Nano 20K + A5E E906** `[SPEC-VER-05]`: both programs are
   27 instructions (54 > 32 words), so switching on one tile is a reload; now the fallback only (item 12). Host is the E906 (AbstractX
   offload, SPI DMA, Dual SPI), not Linux spidev: preload both programs' REG_WRITE frames (CRC done) in
   SRAM, DMA them back to back, check ACKs after. Estimate (not measured): ~200 bytes, ~80 us at 10 MHz
   Dual SPI, ~20 us at 50 MHz, so tens of microseconds. Next: E906 side of the preload in AbstractX,
   then measure on the board (pin edge vs DMA start). Alternative with no reload: two tiles, pin mux
   switch (check the 20K fits two tiles, `fpga-fit`). Required switch time still to be stated.
11. **Tile program preload** `[SPEC-PIO-16]` (proposed 2026-10-31, project owner likes it): staging
   instruction bank + staging SM config + `PRELOAD_SWAP` one-write swap in one clock. Review the spec
   (register offsets 0x200..0x2FC, key 0xA5, whole-tile swap), then RTL in `asp_pio_tile.sv` with a
   failing cocotb test first, `fpga-fit` cost on 9K / 20K, host API in `src/pio2/host/`, E906 side in
   AbstractX. Makes item 10 a one-word write. Optional now that passthrough uses pad lending (item 12).
12. **Passthrough hot switch by pad lending** `[SPEC-PIO-17]` (decided 2026-10-31, project owner; spec
   `a0d4be0`, not implemented). ESC / motor testing loads no RC decode or NeoPixel tiles, so it uses
   5 SMs: 4 DShot on the motor tile + 1 `esc_serial` on a spare **switch tile**, loaded once and kept
   loaded. Enter: disarmed check, stop DShot, release all motor pads, write `OWNER` of the chosen pad
   to the switch tile, break 50 ms; leave: pad back, re-enable DShot. No program load on either tile.
   Reload (item 10) and preload (item 11) are fallbacks.
   **Next step (project owner, 2026-10-31)**: the pad-owner mux in RTL with its cocotb test (lend
   motor pad 2, exchange bytes, give it back, DShot resumes).
   Next, in order: (a) per-board tile allocation for 9K / 20K written down (which tile is motor,
   switch, LED, PWM, IMU); (b) `OWNER` mux built from `pins.toml` alternates, RTL in
   `pio2_core_regs.sv` + board tops, with a failing cocotb test first (lend pad 2, bytes both ways,
   refused while either side drives, no clock with two drivers, DShot resumes with no reload);
   (c) `fpga-fit` mux cost; (d) Tang Nano 9K one-motor bench (switch time, no glitch, ESC answers,
   DShot resumes), part of `[SPEC-VER-03]` / `[SPEC-VER-05]`.
   Reference checked 2026-10-31: github.com/tcmichals/rt-fc-offloader `22fce99`
   (`wb_serial_dshot_mux.sv`, `wb_esc_uart.sv`) agrees with `[SPEC-PIO-13]` (details in
   engineering_log.md). Betaflight `d593400a0`: `esc4wayInit()` stops all motors and makes every motor
   line an input; `MSP_SET_PASSTHROUGH` has no ARMED check.
13. **MSP sniffer + 4-way interface in the Linux Python app** `[SPEC-PIO-13]` (decided 2026-10-31,
   project owner; not implemented): `src/pio2/host/msp_passthrough.py`. MSPv1 245 (empty or mode
   0xFF) enters, refused while armed, reply ESC count; the app takes Betaflight's `serial_4way.c` role
   (frames `0x2F`, answers `0x2E`); exit on `0x34` or 5 s; `MSP_IDENT` does not enter; motor commands
   refused in between. host-tests first (each rule a test), then compare frame by frame with the
   configurator's `MSP/fourway.py`.
14. **python-imgui-esc-configurator** (github.com/tcmichals/python-imgui-esc-configurator; local
   copies `~/projects/quadcopter/esc-configurator` and the USB one are identical, at `main` e301160):
   branch `feat/asyncio-kernel` (54c072a, 3 ahead / 4 behind main) adds `comm_proto/asp_tlp.py`
   (64-byte AbstractX TLPs, channel 0x05 = ESC passthrough); compare with pio2's link frames
   `[SPEC-LINK-02]`. Untracked `studio_plugin/esc_plugin.py` in both copies is not on GitHub. The
   configurator never sends `MSP_SET_ARMING_DISABLED` (99) (fix belongs in that repo).

## Pick up here (handoff 2026-10-30, session stopped by the project owner)

State of `main` (pushed to origin 2026-10-31; both worktree branches pushed too): tile split, DMA + rings + crossbar + core registers, Icarus as second
simulator, k-induction on both AXI4-Lite front ends are committed. Verilator: 11/11 cocotb suites
(including the 384-case ISA suite), pytest 442 passed. The Icarus run of the merged tree did not finish
before the stop: re-run `uv run pio2-cocotb --sim icarus` (the new pio2_core suites have never run on
Icarus).

Unmerged work, in branches (each has its own commits and a report in its last commit messages):
* `worktree-agent-a1f543a2c9553e1ee`: **merged 2026-10-31 (db40f9d); spec follow-ups below still open**. `src/pio2/host/frame.py` (link frames,
  CRC32), `src/pio2/trace/ctf.py` + `barectf_config.yaml` (CTF, read back by babeltrace2), `pio2-trac`
  evidence ledger (TODO item 7). Expect conflicts in engineering_log.md (append both) and in the
  spec text it asked for (op-code table is already in [SPEC-LINK-02]; add [SPEC-VER-02] "evidence
  ledger", point [SPEC-TRC-02] Verified-by at tests/test_trace_ctf.py, align [SPEC-DBG-13]). After the
  merge re-run `uv run pio2-trac --record --formal --lint` on a clean tree and commit the ledger.
* `worktree-agent-aa5ce2bdeac89fae2`: debugger M3-M7, ESC examples, conformance in the debugger
  (TODO items 2, 3). Stopped mid-work. Two commits: 7908888 (conformance programs stepped in the
  debugger, tested) and 99d587f (WIP, **untested**: more parts, stimulus generators, example sessions
  with diagrams for 12 programs, snapshots for reverse step, bench / part / diagram modules; ~900
  changed lines + new files). Written against the OLD pio2_core (trace record port, i_trace_mask);
  main moved the trace to channel 1 of the core's output stream (`pio2sim.cpp`, `session.py`), so
  merge those two files by hand and run `uv run pytest tests -q`.

How to resume:
```bash
cd /media/tcmichals/projects/pio2
git log --oneline -8                                     # main: db40f9d (branch 1 merged)
# done: worktree-agent-a1f543a2c9553e1ee merged (db40f9d)
git merge --no-ff worktree-agent-aa5ce2bdeac89fae2       # 4. debugger (conflicts in session.py, pio2sim.cpp)
export PATH=$HOME/.tools/oss-cad-suite/bin:$PATH
uv run pytest tests -q && uv run pio2-cocotb && uv run pio2-cocotb --sim icarus
git worktree remove .claude/worktrees/agent-*            # once merged (use --force if needed)
```
Use `git status --ignore-submodules=all` (plain `git status` / `git stash` hang on this disk).
Host moved to Ubuntu 26.04 (2026-10-31): system Python is 3.14 and uv was gone; uv 0.13.6 reinstalled
to `~/.local/bin`, `.venv` rebuilt with `uv sync` (Python 3.12.16).
babeltrace2 is now installed: 7 of 8 CTF tests pass. **Open**: `test_debugger_session_records_to_ctf`
fails on the merged tree; DROPPED counts sum to less than the drop total (1835 vs 2040), see
engineering_log.md 2026-10-31. Fix the RTL accounting (or prove it right) before relaxing the test.

Uncommitted-then-committed draft (this commit): **SPI link RTL, untested, not in any build**
* `src/rtl/link/pio2_spi_phy.sv` (SCK domain, two frame buffers per direction, handover while CS_n
  high) and `src/rtl/link/pio2_link.sv` (frame check, CRC32, REG_READ/WRITE/STATUS/STREAM_DATA, credit
  check, stream frames out, link registers, INT_n). Never compiled. Known weak spots to review first:
  the reply byte writer (`put` / `put_idx` / `wcrc` updates in S_PUT_WORD), the REG_READ count path
  in S_RD_BUS, `stream_chan` declared after use, and the stream writer sharing the tx write port.
* The spec for it is done and committed ([SPEC-LINK-01..08], 2026-10-30): wire format, ops and NAK
  codes, STATUS layout, link registers at 0x3100, CS timing rules.

Next steps, in order:
1. Merge `worktree-agent-a1f543a2c9553e1ee` (above).
2. SPI link: lint and fix the two drafts; `src/rtl/link/pio2_spi_top.sv` (pio2_spi_phy + pio2_link +
   pio2_core, pins as _i/_o/_oe); add to a file list; `tests/cocotb/test_link_cocotb.py` with an SPI
   host model (single and dual, SCK slower and faster than f_sys, short read resent, bad CRC, credit
   NAK, every op, INT_n); formal faxil_master on the link's AXI4-Lite master.
3. `src/pio2/host/driver.py` ([SPEC-LINK-06]): one channel API, back ends: SPI pins (a Verilator model
   of pio2_spi_top, new small C wrapper like pio2sim.cpp), direct (Session.stream_push/stream_out +
   register calls), Zynq (`src/pio2/host/zynq.py`, UIO mmap; test with a file-backed fake).
   `tests/test_host_driver.py`: same results through pins and direct.
4. Debugger: merge the M3-M7 branch (or redo what is missing), then the IMU auto-DMA session on the
   new core (program `src/programs/imu_spi_burst.pio`, IMU model as in test_pio2_core_cocotb.py).
5. Tang tops (item 4): `boards/tang_nano9k`, `boards/tang_nano20k` with pio2_spi_top, pins.toml pin
   maps; synthesize with yosys `synth_gowin` + nextpnr-himbaechel + gowin_pack (all in oss-cad-suite);
   record LUT/FF/BRAM and Fmax ([SPEC-ICN-12], fpga-fit). No board here: board-bringup stays unverified.
6. AbstractX (item 6): move `asp_pio_tlp_ingress.sv` / `asp_pio_tlp_shim.sv` (from pio2 history, commit
   aca5ae5) into AbstractX `rtl/pio_tlp/`, wrap them around the new tile stream ports in
   `asp_pio_wrap`, serve OUT_CFG/OUT_TIMEOUT/IN_CFG/IN_STATUS/SMx+0x1C there, move
   test_asp_pio_tlp_path_cocotb.py + the old TLP packetizer test there, bump the submodule; then remove
   the Tang boards from AbstractX. Commit in AbstractX (no push).
7. Zynq (item 5): Vivado block-design Tcl (cannot be run here: no Vivado), Zynq driver back end.
8. Formal on the AXI-Stream ports (faxis_master/slave) of pio2_dma / pio2_core; pyuvm environments
   ([SPEC-VER-01]) are not written yet: the new suites are directed + randomized cocotb, not pyuvm.
9. Docs: rewrite docs/architecture.md (still describes the AbstractX-era tile), tutorials (item 8).

## Next

0. [x] Every RP2040 opcode / operand checked on the RTL (`tests/test_isa_every_opcode.py`, 384 cases, coverage
   test, 3 mutants caught). [ ] Same cases as a cocotb suite on Icarus as a second simulator.

1. [ ] Interconnect RTL, in order, each with formal + UVM-style cocotb + coverage ([SPEC-VER-01]):
   1. [x] Verification base: uv deps (cocotbext-axi, pyuvm, cocotb-coverage); ZipCPU property files from
      the `third_party/wb2axip` submodule; [x] first formal .sby (tile AXI4-Lite port, faxil_slave): BMC
      depth 24 passes, mutant fails; [ ] k-induction (needs invariants; stopped after 25 min at step 3)
   2. [x] Tile register port Wishbone -> AXI4-Lite ([SPEC-AXI-04]); all tile suites through cocotbext-axi
      (43 tests); front-end suite with stalls, orders, SLVERR, exactly-once; two mutants caught
   2b. [x] Reuse ([SPEC-AXI-05]): Forencich verilog-axi / verilog-axis and ZipCPU wb2axip as pinned
      `third_party/` submodules; licenses checked and recorded (`docs/third_party.md`, `LICENSES/`)
   2c. [x] AbstractX: `asp_pio_wrap` with ZipCPU `wbm2axilite` in front of the tile (AbstractX dded0fc)
   2d. [x] `pio2_core` with the fixed boundary ([SPEC-ICN-11], 2026-10-30): AXI4-Lite slave, AXI-Stream
      in / out with the channel in `TDEST`, IRQ; core registers at 0x3000; `test_pio2_core_cocotb` with
      DShot, ESC serial and IMU auto-DMA end to end at 27 MHz
   3. [x] Tile: TLP adapters out, AXI-Stream FIFO ports ([SPEC-ICN-09], 2026-10-30; stream suite with
      register / stream collisions, one product bug fixed); [ ] AbstractX wrapper with the TLP adapters
      (item 6); [x] observation port + `pio2_trace_tap` + `pio2_core` (tile, tap, event FIFO)
      ([SPEC-TRC-03]), shown in the debugger's Trace window
   4. [ ] SPI PHY single / dual, SCK-clocked + async FIFO ([SPEC-LINK-01]), framer with CRC32
      ([SPEC-LINK-02]), NIC registers ([SPEC-LINK-07])
   5. [ ] Channel mux, credits, `INT_n` ([SPEC-LINK-03..05])
   6. [x] AXI4-Lite crossbar ([SPEC-AXI-03]), DMA engine and rings ([SPEC-DMA-01/02]) (2026-10-30:
      `pio2_dma`, `pio2_dma_chan`, `pio2_axil_regs` with faxil_slave k-induction, 9 DMA tests)
   7. [x] Event FIFO ([SPEC-TRC-01]): Forencich `axis_fifo`, slots, priority, drop and count; 5 tests,
      2 mutants caught. [x] DMA trace channel into ring 1 (debugger reads records from channel 1);
      [ ] host CTF conversion ([SPEC-TRC-02])
   8. [ ] Python host driver with SPI-pins and direct back ends ([SPEC-LINK-06])
2. [x] Debugger M1: `libpio2sim.so` (Verilator + VPI), `Session`, `pio2-debug` CLI, 13 pytest tests ([SPEC-DBG-01..03, 09]);
   [ ] conformance programs stepped in the debugger vs the cocotb results
3. [x] Debugger M2: `pio2-gui` (imgui-bundle): program listing, SM state, FIFOs, IRQs, parts canvas (LED,
   button, UART terminal), logic analyzer + PC timeline, run / step clock / step instruction, 4 examples.
   [ ] M3-M7: breakpoints, reverse step, FST / CTF export, saved layout, more parts, diff mode, board back end
4. [ ] Tang boards in pio2 ([SPEC-ICN-12]): pio2-native tops (SPI link + core + PIO), DShot / ESC serial /
   UART as PIO programs, pin mux ([SPEC-PIO-09]); then remove the Tang boards (9K, Nano 20K, Primer
   20K) from AbstractX
5. [ ] Zynq ([SPEC-ICN-11], 2026-10-30: no vendor IP): `pio2_zynq_adapter` = Forencich `axi_axil_adapter` (GP0 to the
   core's AXI4-Lite) + `pio2_ring_dma` (AXI master on HP0, one DDR ring per channel by `TDEST`, data written and
   acknowledged before the head moves) + one `IRQ_F2P`; cocotb tests for both; Zynq driver back end over one
   `generic-uio` device (map 0 registers, map 1 rings).
   - [ ] Link traffic-mix test (AbstractX [SPEC-ARCH-14]): Dual SPI at 10 MHz SCK carrying 8 kHz IMU frames, 8 kHz
     DShot writes, 1 Mbaud serial and trace; measure link busy % and window rate (estimate ~30 %, ~16,000/s).
6. [ ] AbstractX: wrapper that adds the TLP adapters around the pio2 tile; its TLP-path test moves back
7. [ ] Evidence ledger for pio2 (trac.py records) so the matrix can show VERIFIED
8. [ ] Wiki / tutorials and demos (how the PIO works, debugger walkthroughs) once the debugger runs

## Best PIO by far (proposed 2026-10-31, project owner; spec only, nothing built)

Ranked. Each item: failing test first, then RTL / code, then the skill in its `Verified by`.
1. **Live board trace in the debugger** `[SPEC-DBG-17]`: board records in the same Timeline / Trace
   windows, sim-vs-board on one axis with the first difference marked. Needs: DMA trace ring on a
   board (`[SPEC-TRC-02]`), `board` back end (`[SPEC-DBG-12]`), TODO item 1 (DROPPED under-count) fixed.
2. **Three-way diff RTL / emulator / RP2040 silicon** `[SPEC-DBG-18]`: needs the reference emulator
   chosen (item 7) and the Pico firmware (`[SPEC-VER-04]`, item 9).
3. **Instruction memory pages** `[SPEC-PIO-18]`: `IMEM_WORDS` 32 / 64 / 128, per-SM `PAGE`; DShot and
   esc_serial resident in one tile. Pick the `PAGE` register offset in the `[SPEC-PIO-05]` review;
   `fpga-fit` cost first.
4. **Finish RP2040 parity** `[SPEC-PIO-15]`: missing `FDEBUG`, `FLEVEL`, `IRQ_FORCE`, `OUT_STICKY` /
   `INLINE_OUT_EN`, `DBG_PADOUT/PADOE/CFGINFO`, interrupt enables; partial `FSTAT`, `CLKDIV INT = 0`,
   `SMx_INSTR` read. "Best" needs parity first.
5. **Pin edge capture** `[SPEC-TRC-04]` (types `0x0D` / `0x0E`): host-side eRPM decode from edge times.
6. **FIFO depth parameter** `[SPEC-PIO-19]` (4 / 8 / 16).
7. **Half-clock output edges** `[SPEC-PIO-20]`: check the Gowin DDR output primitive on 9K / 20K first.
8. **Public scorecard** `[SPEC-VER-06]`: `docs/scorecard.md` generated by `pio2-trac` from recorded
   runs only; NOT RUN where nothing is recorded.

## Decisions

* 2026-10-29 (project owner): RP2350-only PIO features are not a goal; the focus is the RP2040 ISA plus
  tracing, DMA, the host link packets and the simulator of the whole path.

* 2026-10-29: AXI4-Lite (registers) and AXI-Stream (data) everywhere in pio2, including the tile's register
  port; no Wishbone, no TLP ([SPEC-AXI-01..04]). Reasons: native on the Zynq, proven verification tools
  (ZipCPU formal properties, cocotbext-axi, pyuvm). AbstractX bridges in its own wrapper.
* 2026-10-29: the SPI link is the throughput limit, not the bus (AXI-Stream ~108 MB/s at 27 MHz), so the
  PHY is SCK-clocked with an async FIFO, goal >= 10 MB/s on Dual SPI ([SPEC-LINK-01]).
* 2026-10-29: one core with a fixed boundary; the SPI link and the Zynq PS are alternative host adapters
  ([SPEC-ICN-11]); the trace goes through the DMA engine into ring 1, the same path for both ([SPEC-TRC-02]).
* 2026-10-29: full validation ([SPEC-VER-01]): formal on every bus port, UVM-style pyuvm testbenches,
  functional coverage closure per block.

* 2026-10-31 (project owner): the MSP passthrough sniffer lives in the Linux Python app, not in the
  FPGA (the MSP stream ends there anyway) ([SPEC-PIO-13]).
* 2026-10-31 (project owner): ESC passthrough uses 5 SMs, 4 DShot + 1 `esc_serial` on a spare switch
  tile, switched by pad lending with no program load ([SPEC-PIO-17]); reload is the fallback.

## Open questions

* Commit `studio_plugin/` in the ESC configurator repo, or drop it?
* Should passthrough entry also send / honour `MSP_SET_ARMING_DISABLED` (99)?

* Debugger GUI: imgui-bundle with a custom theme first, web/Tauri front end later if needed.
* Tang Primer 20K: move with the other Tang boards (assumed yes).

## Not verified

* `pio2-programs --check` (needs pioasm: `uv run pio2-setup-tools --pioasm`)
* `pio2-setup-tools --oss-cad-suite` download path
* anything on a board
