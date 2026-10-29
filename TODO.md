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
| Interconnect (link, Wishbone, DMA, trace, host adapters) | **specified only** |
| Debugger / simulator / GUI | **specified only** (milestones M1-M7) |
| Tang Nano 9K / 20K tops | still in AbstractX (they build AbstractX's whole `asp_top`) |
| AbstractX integration | submodule `rtl/pio`; AbstractX 16/16 cocotb, Tang Nano 20K synthesis ok |

## Next

1. [ ] Interconnect RTL, in order, each with cocotb + bus checker, and formal where it applies:
   1. [ ] Wishbone B4 bus checker (`tests/cocotb/wb_checker.py`) and formal properties ([SPEC-WB-01])
   2. [ ] Tile: TLP adapters out (to an AbstractX wrapper), FIFO data requests and event port out ([SPEC-ICN-09])
   3. [ ] SPI PHY single / dual ([SPEC-LINK-01]), framer with CRC32 ([SPEC-LINK-02]), NIC registers ([SPEC-LINK-07])
   4. [ ] Channel mux, credits, `INT_n` ([SPEC-LINK-03..05])
   5. [ ] Wishbone interconnect ([SPEC-WB-02]), DMA engine and rings ([SPEC-DMA-01/02])
   6. [ ] Event FIFO ([SPEC-TRC-01]) and host CTF conversion ([SPEC-TRC-02])
   7. [ ] Python host driver with SPI-pins and direct back ends ([SPEC-LINK-06])
2. [ ] Debugger M1: Verilator model library, `.vlt` public signals, `Session`, CLI ([SPEC-DBG-01..03, 09])
3. [ ] Debugger M2+: GUI with a modern theme, parts canvas ([SPEC-DBG-10], [SPEC-DBG-15]), then M3-M7
4. [ ] Tang boards in pio2 ([SPEC-ICN-12]): pio2-native tops (SPI link + core + PIO), DShot / ESC serial /
   UART as PIO programs, pin mux ([SPEC-PIO-09]); then remove the Tang boards (9K, Nano 20K, Primer
   20K) from AbstractX
5. [ ] Zynq host adapter ([SPEC-ICN-11]): AXI4-Lite to Wishbone, AXI-Stream ring ports, Zynq driver back end
6. [ ] AbstractX: wrapper that adds the TLP adapters around the pio2 tile; its TLP-path test moves back
7. [ ] Evidence ledger for pio2 (trac.py records) so the matrix can show VERIFIED
8. [ ] Wiki / tutorials and demos (how the PIO works, debugger walkthroughs) once the debugger runs

## Open questions

* **Internal bus**: Wishbone B4 (as specified now) or AXI4-Lite for registers + AXI-Stream for data
  (owner considering, 2026-10-29). Decide before the interconnect RTL; the spec changes first.

* Debugger GUI: imgui-bundle with a custom theme first, web/Tauri front end later if needed.
* Tang Primer 20K: move with the other Tang boards (assumed yes).

## Not verified

* `pio2-programs --check` (needs pioasm: `uv run pio2-setup-tools --pioasm`)
* `pio2-setup-tools --oss-cad-suite` download path
* anything on a board
