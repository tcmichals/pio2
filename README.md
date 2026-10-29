# pio2

A programmable I/O block that runs the Raspberry Pi RP2040 / RP2350 PIO instruction set,
written in SystemVerilog for FPGAs, plus the tools to write, test and **see** PIO programs run.

* `src/rtl/`: the PIO tile (four state machines, shared 32-word instruction memory, FIFOs, IRQs,
  pin mapping, side-set), a Wishbone register port, and an optional 64-byte TLP stream adapter.
* `src/programs/`: the program library (`.pio` sources and the `pioasm` images next to them):
  bidirectional DShot600, ESC serial passthrough, the conformance suite, and programs from
  Raspberry Pi's pico-examples (BSD-3-Clause, see `src/programs/examples/pico/LICENSE.TXT`).
* `src/pio2/`: Python package: program loader / assembler wrapper, example importer, DShot
  reference decoder, and (in progress) the visual debugger.
* `tests/cocotb/`: cocotb regression of the tile on Verilator.
* `docs/`: specification (`[SPEC-PIO-*]` requirements), architecture, debugger design, links.

The design goal of the debugger is in [`docs/debugger.md`](docs/debugger.md): single-step the real
RTL (not a separate software model) one clock or one instruction at a time, with every state
machine's registers, FIFOs, clock divider phase, stall reason and pins visible.

This project started as `rtl/pio` inside [AbstractX](https://github.com/tcmichals/AbstractX), which
uses it as a git submodule ([`docs/provenance.md`](docs/provenance.md)).

## Direction

* **pio2 is the simulation home of the PIO.** Every way of running a PIO program without silicon
  lives here: the RTL on Verilator (primary, also the debugger's engine), the same RTL on Icarus
  through cocotb, and independent software reference emulators for comparison (diff mode).
* **One host link, like a small NIC** ([`docs/interconnect.md`](docs/interconnect.md)): SPI or
  Dual-SPI with an interrupt pin, carrying frames of at most 128 bytes with CRC32, NIC-style
  registers (`ISR` / `IMR`, frame queues, error counters), and numbered channels: channel 0 for
  addressed register access, the others as streams. Inside the FPGA everything is Wishbone B4
  (classic for registers, pipelined bursts for streaming); a DMA engine paced by the PIO FIFOs moves
  data between ring buffers and the state machines. No AXI, no TLP in pio2 (AbstractX adds its TLP
  adapters around the tile in its own wrapper).
* **Same protocol for simulation and hardware.** The IDE / debugger drives the simulated IP through the
  same host driver as a board (at the SPI pins bit for bit, or straight to the channels for speed).
* **Tracing is barectf / CTF 1.8**, with AbstractX's schema (`trace/barectf_config.yaml`, which already
  has `pio_event`: tile, SM, event type, PC, duration, next to `tlp_push` / `tlp_pop`). Simulation
  traces and board traces open in the same viewers (AbstractX Studio, babeltrace2), and a
  simulated run can be compared with a board run event by event.
* **Example boards: Tang Nano 9K and Tang Nano 20K** (Gowin, open-source flow). The board tops,
  pin constraints and bitstream build stay in AbstractX (`hw/tang9k/`, `hw/tang_nano20k/`,
  `make -f hw/Makefile tang9k|tang_nano20k`); pio2 documents how to run its example programs on
  them and connect the debugger.
* **A Wokwi-style simulator** (like <https://wokwi.com/pi-pico>), built with imgui-bundle: a parts
  canvas with the tile's pins wired to LEDs, buttons, a UART terminal, a WS2812 strip, an ESC with a
  motor model, an SPI IMU, a logic analyzer; plus the debugger views (per-SM registers, FIFOs,
  clock divider, timeline) and single step by clock or instruction, forward and back. Unlike
  software-model simulators it runs the real RTL. See [`docs/debugger.md`](docs/debugger.md).
* **Examples** that ship as debugger sessions: Raspberry Pi pico-examples, Wokwi Pi Pico projects
  as a behaviour reference, Betaflight's PIO programs, our DShot / ESC serial programs, and the
  IMU auto-DMA path (DRDY -> SPI burst -> TLP frames). Details: [`docs/debugger.md`](docs/debugger.md).

```
          .pio  ──pioasm──►  image
                               │
   IDE / debugger (Python, imgui-bundle) ── host link: SPI frames, channels ─┐
        │                                                                   │
        │ barectf CTF traces (AbstractX schema)                 ┌───────────┴───────────┐
        ▼                                                       │                       │
   AbstractX Studio / babeltrace2                    Verilator model of        Tang Nano 9K / 20K
                                                     src/rtl (simulation)      (Wishbone + DMA + link)
```

## Quick start

Python is managed with [uv](https://docs.astral.sh/uv/). The simulator (Verilator, or Icarus) is
not a Python package; see [Tools](#tools).

```bash
uv sync                      # Python 3.12 venv with cocotb + pytest (dev group)
uv sync --extra gui          # also imgui-bundle, for the debugger
uv run pio2-programs --list  # programs and their sizes
uv run pio2-programs --check # images match pioasm (needs pioasm, else exits 2: "not checked")
```

Run the RTL regression (Verilator):

```bash
cd tests/cocotb
uv run make MODULE=test_asp_pio_cocotb
uv run make MODULE=test_asp_pio_conformance_cocotb
uv run make MODULE=test_asp_pio_dshot_cocotb
uv run make MODULE=test_asp_pio_esc_serial_cocotb
uv run make MODULE=test_asp_pio_tlp_path_cocotb
```

A run passes only when cocotb reports `FAIL=0`.

## Ubuntu setup (checked on 26.04.1 LTS)

```bash
# 1. System packages: compiler and build tools for Verilator models, OpenGL for the GUI
sudo apt update
sudo apt install -y build-essential git make cmake curl python3 libgl1 libegl1 gtkwave

# 2. uv (Python and the project environment). Official installer, or a release tarball:
#    https://docs.astral.sh/uv/getting-started/installation/
curl -LsSf https://astral.sh/uv/install.sh | sh

# 3. Simulators. Either the pinned oss-cad-suite (Verilator 5.x, Icarus, Yosys, GTKWave) ...
uv run pio2-setup-tools --oss-cad-suite      # ~750 MB into ~/.tools/oss-cad-suite, SHA-256 checked
export PATH="$HOME/.tools/oss-cad-suite/bin:$PATH"
#    ... or the Ubuntu packages (26.04 ships Verilator 5.032; Verilator 5.x is required):
sudo apt install -y verilator iverilog

# 4. pioasm (only to re-assemble .pio sources; the checked-in images do not need it)
uv run pio2-setup-tools --pioasm             # builds it from pico-sdk 2.2.0 into ~/.tools/pioasm

# 5. Check
uv run pio2-setup-tools --check
```

`pio2-setup-tools --check` lists what is found and where (`~/.tools/oss-cad-suite/bin`,
`~/.tools/pioasm`, then `PATH`). Verilator, make and g++ are required; Icarus and pioasm are optional.

## Tools

| Tool | Used for | Where it comes from |
| :--- | :--- | :--- |
| Verilator 5.x | RTL simulation, debugger model | oss-cad-suite (`~/.tools/oss-cad-suite/bin`) or your PATH |
| Icarus Verilog | alternative simulator (`SIM=icarus`) | oss-cad-suite or your PATH |
| pioasm | assembling `.pio` sources | Raspberry Pi pico-sdk (`$PIOASM`, `~/.tools/pioasm/pioasm`) |
| cocotb, pytest, imgui-bundle | tests, debugger GUI | PyPI through uv |

## Documentation

* [`docs/SPECIFICATION.md`](docs/SPECIFICATION.md): requirements (`[SPEC-PIO-*]`), RP2040 parity checklist
* [`docs/architecture.md`](docs/architecture.md): block diagram, register map, ports
* [`docs/debugger.md`](docs/debugger.md): visual single-step debugger and parts canvas design
* [`docs/interconnect.md`](docs/interconnect.md): host link (SPI / Dual-SPI, 128-byte CRC32 frames, NIC-style registers), channels, Wishbone, DMA, barectf trace
* [`docs/boards.md`](docs/boards.md): Tang Nano 9K / 20K example boards (built from AbstractX)
* [`docs/references.md`](docs/references.md): links to the Raspberry Pi datasheets and tools (not copied here)
* [`docs/provenance.md`](docs/provenance.md): where every file came from

## License

GPL-3.0-or-later (`LICENSE`). The programs under `src/programs/examples/pico/` are Raspberry Pi
pico-examples under BSD-3-Clause (their own `LICENSE.TXT`). No Raspberry Pi documentation is copied
into this repository; `docs/references.md` links to it.
