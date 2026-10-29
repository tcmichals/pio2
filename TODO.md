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
| Interconnect (link, AXI4-Lite / AXI-Stream, DMA, trace, host adapters) | **specified only** |
| Debugger / simulator / GUI | **specified only** (milestones M1-M7) |
| Tang Nano 9K / 20K tops | still in AbstractX (they build AbstractX's whole `asp_top`) |
| AbstractX integration | submodule `rtl/pio`; AbstractX 16/16 cocotb, Tang Nano 20K synthesis ok |

## Next

1. [ ] Interconnect RTL, in order, each with formal + UVM-style cocotb + coverage ([SPEC-VER-01]):
   1. [x] Verification base: uv deps (cocotbext-axi, pyuvm, cocotb-coverage) done; [ ] ZipCPU property
      files (`pio2-setup-tools --zipcpu-props`, needs a run with network), [ ] first formal .sby
   2. [x] Tile register port Wishbone -> AXI4-Lite ([SPEC-AXI-04]); all tile suites through cocotbext-axi
      (43 tests); front-end suite with stalls, orders, SLVERR, exactly-once; two mutants caught
   2b. [ ] Reuse ([SPEC-AXI-05]): Forencich verilog-axi / verilog-axis and ZipCPU wb2axip as pinned
      `third_party/` submodules
   2c. [ ] AbstractX: wrapper with ZipCPU `wbm2axilite` in front of the tile before moving its
      `rtl/pio` pointer past this change (AbstractX still instantiates the Wishbone port)
   3. [ ] Tile: TLP adapters out (AbstractX wrapper with its WB->AXI4-Lite bridge), AXI-Stream FIFO
      ports and event port in ([SPEC-ICN-09])
   4. [ ] SPI PHY single / dual, SCK-clocked + async FIFO ([SPEC-LINK-01]), framer with CRC32
      ([SPEC-LINK-02]), NIC registers ([SPEC-LINK-07])
   5. [ ] Channel mux, credits, `INT_n` ([SPEC-LINK-03..05])
   6. [ ] AXI4-Lite crossbar ([SPEC-AXI-03]), DMA engine and rings ([SPEC-DMA-01/02])
   7. [ ] Event FIFO ([SPEC-TRC-01]) and host CTF conversion ([SPEC-TRC-02])
   8. [ ] Python host driver with SPI-pins and direct back ends ([SPEC-LINK-06])
2. [ ] Debugger M1: Verilator model library, `.vlt` public signals, `Session`, CLI ([SPEC-DBG-01..03, 09])
3. [ ] Debugger M2+: GUI with a modern theme, parts canvas ([SPEC-DBG-10], [SPEC-DBG-15]), then M3-M7
4. [ ] Tang boards in pio2 ([SPEC-ICN-12]): pio2-native tops (SPI link + core + PIO), DShot / ESC serial /
   UART as PIO programs, pin mux ([SPEC-PIO-09]); then remove the Tang boards (9K, Nano 20K, Primer
   20K) from AbstractX
5. [ ] Zynq ([SPEC-ICN-11]): PS GP port as second AXI4-Lite master, PS AXI DMA on the ring AXIS ports, Zynq driver back end
6. [ ] AbstractX: wrapper that adds the TLP adapters around the pio2 tile; its TLP-path test moves back
7. [ ] Evidence ledger for pio2 (trac.py records) so the matrix can show VERIFIED
8. [ ] Wiki / tutorials and demos (how the PIO works, debugger walkthroughs) once the debugger runs

## Decisions

* 2026-10-29: AXI4-Lite (registers) and AXI-Stream (data) everywhere in pio2, including the tile's register
  port; no Wishbone, no TLP ([SPEC-AXI-01..04]). Reasons: native on the Zynq, proven verification tools
  (ZipCPU formal properties, cocotbext-axi, pyuvm). AbstractX bridges in its own wrapper.
* 2026-10-29: the SPI link is the throughput limit, not the bus (AXI-Stream ~108 MB/s at 27 MHz), so the
  PHY is SCK-clocked with an async FIFO, goal >= 10 MB/s on Dual SPI ([SPEC-LINK-01]).
* 2026-10-29: full validation ([SPEC-VER-01]): formal on every bus port, UVM-style pyuvm testbenches,
  functional coverage closure per block.

## Open questions


* Debugger GUI: imgui-bundle with a custom theme first, web/Tauri front end later if needed.
* Tang Primer 20K: move with the other Tang boards (assumed yes).

## Not verified

* `pio2-programs --check` (needs pioasm: `uv run pio2-setup-tools --pioasm`)
* `pio2-setup-tools --oss-cad-suite` download path
* anything on a board
