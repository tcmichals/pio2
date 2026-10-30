# Third-Party Code and Licenses

pio2 is GPL-3.0-or-later (`LICENSE`, also `LICENSES/GPL-3.0-or-later.txt`). It reuses the projects
below as **git submodules** under `third_party/`, pinned to the commits listed, and never copies or
edits their files ([SPEC-AXI-05] in `docs/INTERCONNECT_SPECIFICATION.md`). Clone with
`git clone --recurse-submodules`, or run `git submodule update --init` after a plain clone.

| Path | Project | Author | License | Pinned commit | Used for |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `third_party/verilog-axi` | <https://github.com/alexforencich/verilog-axi> | Alex Forencich | MIT (its `COPYING`) | `516bd5dadc3365b7f9e225d2af8fe0b8d804fe53` (2025-02-27) | `axil_crossbar` (+ `_addr`, `_rd`, `_wr`, `axil_register_rd/wr`): pio2_core's crossbar ([SPEC-AXI-03]) |
| `third_party/verilog-axis` | <https://github.com/alexforencich/verilog-axis> | Alex Forencich | MIT (its `COPYING`) | `48ff7a7e2ef782cf778d47910cf85835c64b1bce` (2025-02-27) | `axis_fifo` (event FIFO, DMA rings), `axis_adapter` (trace 64 -> 32 bits), `axis_arb_mux` + `arbiter` + `priority_encoder` (core output merge) ([SPEC-TRC-01], [SPEC-DMA-02]). verilog-axi has its own `arbiter` / `priority_encoder`, which differ only in how the reset branch is written; the verilog-axis copies serve both (`src/rtl/pio2_core.f`) |
| `third_party/wb2axip` | <https://github.com/ZipCPU/wb2axip> | Dan Gisselquist, Gisselquist Technology, LLC (ZipCPU, <https://github.com/ZipCPU>) | Apache-2.0 (stated in every file header and its README; the text is in `LICENSES/Apache-2.0.txt`) | `2e8d3bc2d26ddc33d1881022a2a2b9d3f0c16b9b` (2026-06-02) | formal property files `bench/formal/faxil_slave.v`, `faxil_master.v`, `faxis_master.v`, `faxis_slave.v` ([SPEC-VER-01]); `rtl/wbm2axilite.v` for AbstractX's Wishbone bridge |

Other third-party content inside pio2's own tree:

| Path | Source | License |
| :--- | :--- | :--- |
| `src/programs/examples/pico/` | Raspberry Pi pico-examples, <https://github.com/raspberrypi/pico-examples> | BSD-3-Clause (its `LICENSE.TXT`, kept next to the files) |

## How pio2 complies

* **MIT (Forencich)**: the copyright and permission notice must stay with the code. The submodules
  carry their own `COPYING` unchanged; anything pio2 ships that contains their files (a release
  archive, a vendored copy for a board build) includes those `COPYING` files.
* **Apache-2.0 (ZipCPU)**: a copy of the license goes with any redistribution of the files
  (`LICENSES/Apache-2.0.txt`), their copyright headers stay unchanged, and modified files must say so.
  pio2 does not modify them. wb2axip has no `NOTICE` file at the pinned commit, so there is no NOTICE
  text to carry; re-check this when the pin moves.
* **Compatibility**: MIT and Apache-2.0 code may be combined into a GPL-3.0-or-later work (Apache-2.0 is
  compatible with GPLv3, not GPLv2). The combined design (bitstreams, simulation models) is distributed
  under GPL-3.0-or-later; the third-party files keep their own licenses.
* **BSD-3-Clause (pico-examples)**: the copyright notice and license stay with the files; the Raspberry
  Pi name is not used to endorse pio2.
* **Documentation** from Raspberry Pi and others is linked, never copied (`docs/references.md`).

## When a pin moves

Update the commit in this table in the same change, re-read the project's license and `NOTICE`
(they can change between commits), and re-run the checks that use it (`formal-check`,
`cocotb-regression`).
