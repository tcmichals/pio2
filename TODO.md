# pio2 TODO and Status

Updated 2026-10-29. Specs: `docs/SPECIFICATION.md` (tile), `docs/INTERCONNECT_SPECIFICATION.md`,
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
