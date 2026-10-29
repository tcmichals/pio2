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
