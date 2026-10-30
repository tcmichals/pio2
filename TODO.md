# pio2 TODO and Status

Updated 2026-10-30. Specs: `docs/SPECIFICATION.md` (tile), `docs/INTERCONNECT_SPECIFICATION.md`,
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

## Pick up here (handoff 2026-10-30, session stopped by the project owner)

State of `main` (not pushed): tile split, DMA + rings + crossbar + core registers, Icarus as second
simulator, k-induction on both AXI4-Lite front ends are committed. Verilator: 11/11 cocotb suites
(including the 384-case ISA suite), pytest 442 passed. The Icarus run of the merged tree did not finish
before the stop: re-run `uv run pio2-cocotb --sim icarus` (the new pio2_core suites have never run on
Icarus).

Unmerged work, in branches (each has its own commits and a report in its last commit messages):
* `worktree-agent-a1f543a2c9553e1ee`: **finished, merge next**. `src/pio2/host/frame.py` (link frames,
  CRC32), `src/pio2/trace/ctf.py` + `barectf_config.yaml` (CTF, read back by babeltrace2), `pio2-trac`
  evidence ledger (TODO item 7). Expect conflicts in engineering_log.md (append both) and in the
  spec text it asked for (op-code table is already in [SPEC-LINK-02]; add [SPEC-VER-02] "evidence
  ledger", point [SPEC-TRC-02] Verified-by at tests/test_trace_ctf.py, align [SPEC-DBG-13]). After the
  merge re-run `uv run pio2-trac --record --formal --lint` on a clean tree and commit the ledger.
* `worktree-agent-aa5ce2bdeac89fae2`: debugger M3-M7, ESC examples, conformance in the debugger
  (TODO items 2, 3). **Stopped mid-work**: only its first commit (7908888, conformance programs in the
  debugger) is on the branch; uncommitted work may be in `.claude/worktrees/agent-aa5ce2bdeac89fae2/`.
  It was written against the OLD pio2_core (trace port); when merging, Session's trace now comes from
  channel 1 of the core's output stream (`pio2sim.cpp`, `session.py` on main).

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
5. [ ] Zynq ([SPEC-ICN-11]): Vivado block design: PS GP port to the core's AXI4-Lite slave, AXI MCDMA on its two
   AXI-Stream ports (channel in `TDEST`), `IRQ_F2P`; Zynq driver back end
6. [ ] AbstractX: wrapper that adds the TLP adapters around the pio2 tile; its TLP-path test moves back
7. [ ] Evidence ledger for pio2 (trac.py records) so the matrix can show VERIFIED
8. [ ] Wiki / tutorials and demos (how the PIO works, debugger walkthroughs) once the debugger runs

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

## Open questions


* Debugger GUI: imgui-bundle with a custom theme first, web/Tauri front end later if needed.
* Tang Primer 20K: move with the other Tang boards (assumed yes).

## Not verified

* `pio2-programs --check` (needs pioasm: `uv run pio2-setup-tools --pioasm`)
* `pio2-setup-tools --oss-cad-suite` download path
* anything on a board
