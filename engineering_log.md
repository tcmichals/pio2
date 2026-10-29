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
