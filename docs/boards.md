# Example Boards: Tang Nano 9K and Tang Nano 20K

`[SPEC-DBG-14]` in [DEBUGGER_SPECIFICATION.md](DEBUGGER_SPECIFICATION.md). The board tops, pin constraints and the bitstream build
live in [AbstractX](https://github.com/tcmichals/AbstractX), which contains pio2 as `rtl/pio`.

| Board | FPGA | AbstractX top | Constraints | Build (in AbstractX) |
| :--- | :--- | :--- | :--- | :--- |
| Tang Nano 9K | Gowin GW1NR-9 | `hw/tang9k/top_tang9k.sv` | `hw/tang9k/tang9k.cst` | `make -f hw/Makefile tang9k` |
| Tang Nano 20K | Gowin GW2AR-18 | `hw/tang_nano20k/top_tang_nano20k.sv` | `hw/tang_nano20k/tang_nano20k.cst` | `make -f hw/Makefile tang_nano20k` |

The open-source Gowin flow (Yosys `synth_gowin`, nextpnr-himbaechel, gowin_pack) comes from
oss-cad-suite (`uv run pio2-setup-tools --oss-cad-suite`).

## State today (2026-10-29)

* Both tops build the PIO tile into `asp_top` (Tang Nano 20K synthesis checked 2026-10-29).
* **The tile cannot drive a pin yet**: `asp_top` leaves `o_gpio_out` / `o_gpio_pindir` unconnected and
  feeds `i_gpio` from fixed signals (PWM inputs, IMU INT/MISO). Pin ownership (DShot core vs PIO vs
  serial passthrough, output enables, bidirectional motor pins) is the open "PIO / ESC pin mux"
  item in AbstractX `TODO.md` and [SPEC-PIO-09] in [SPECIFICATION.md](SPECIFICATION.md).
* So the `board` back end ([SPEC-DBG-12]) is blocked on that pin mux; until then programs run in
  simulation only.

## To fill in when the pin mux exists

Per board: the pins the tile drives and reads (from the constraint file), how to load an example
program over the AbstractX host transport, and how to attach the debugger.
