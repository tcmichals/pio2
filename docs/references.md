# References

Linked, not copied: the documents stay with their owners.

## PIO definition

* RP2040 datasheet, chapter 3 (PIO): <https://datasheets.raspberrypi.com/rp2040/rp2040-datasheet.pdf>
  (the specification cites its sections, e.g. §3.4 instruction set, §3.5 functional details).
* RP2350 datasheet, PIO chapter (version 1 PIO: extra instructions and the RP2350 changes):
  <https://datasheets.raspberrypi.com/rp2350/rp2350-datasheet.pdf>
* Raspberry Pi Pico-series C/C++ SDK book (pioasm syntax, `.program` directives):
  <https://datasheets.raspberrypi.com/pico/raspberry-pi-pico-c-sdk.pdf>
* All Raspberry Pi documentation: <https://www.raspberrypi.com/documentation/microcontrollers/>

## Tools

* Betaflight (PIO DShot / ESC programs, read from a checkout): <https://github.com/betaflight/betaflight>

* pico-sdk (pioasm lives in `tools/pioasm`): <https://github.com/raspberrypi/pico-sdk>
* pico-examples (source of `src/programs/examples/pico/`): <https://github.com/raspberrypi/pico-examples>
* Verilator: <https://verilator.org/guide/latest/>
* cocotb: <https://docs.cocotb.org/>
* oss-cad-suite: <https://github.com/YosysHQ/oss-cad-suite-build>
* imgui-bundle (Dear ImGui, ImPlot, for Python): <https://pthom.github.io/imgui_bundle/>
* uv: <https://docs.astral.sh/uv/>

## Other PIO simulators and debuggers (prior art, see DEBUGGER_SPECIFICATION.md)

Listed to compare against; check each project's current state and license before reusing anything.

* Wokwi RP2040 simulation, browser based, includes PIO: <https://wokwi.com/>; Pi Pico examples:
  <https://wokwi.com/pi-pico> (behaviour reference for `[SPEC-DBG-11]`; linked, not copied)
* rp2040pio, a Java PIO emulator with a GUI (soundpaint): <https://github.com/soundpaint/rp2040pio>
* rp2040-pio-emulator, Python (NathanY3G): <https://github.com/NathanY3G/rp2040-pio-emulator>
* rp2040js, the TypeScript RP2040 emulator behind Wokwi (MIT): <https://github.com/wokwi/rp2040js>;
  Wokwi logic analyzer (VCD export): <https://docs.wokwi.com/guides/logic-analyzer>
* rp2040pio documentation (soundpaint, GPL-2.0): <https://rp2040pio-docs.readthedocs.io/>; fork:
  <https://github.com/byteit101/pio-debugger-emulator>
* rp2040-pio-emulator on PyPI (Apache-2.0): <https://pypi.org/project/rp2040-pio-emulator/>
* headblockhead/pio, Go, full PIO block, cycle exact (AGPL-3.0; diff-mode candidate):
  <https://github.com/headblockhead/pio>
* picoem, Rust RP2040 / RP2350 emulators incl. PIO (Apache-2.0 / MIT): <https://github.com/0x4D44/picoem>
* epio, C, RP2350 PIO emulator (MIT): <https://github.com/piersfinlayson/epio>
* pioemu, Rust, randomized tests against a real RP2040 (MIT): <https://github.com/wang-edward/pioemu>
* pio_sim, browser PIO simulator (MIT): <https://github.com/ice458/pio_sim>
* pioweave, Python / Amaranth PIO builder and emulator (MIT, early): <https://github.com/youprint/pioweave>
* rpipico_simple_PIO_emulator, C, single SM (GPL-3.0):
  <https://github.com/sumio-morioka/rpipico_simple_PIO_emulator>; derived: <https://github.com/esky-software/piosim>
* Renode RP2040 models and their PIO simulator (MIT): <https://github.com/matgla/Renode_RP2040>,
  <https://github.com/matgla/Renode_RP2040_PioSim>
* fpga_pio, Verilog RP2040 PIO (BSD-2-Clause, incomplete): <https://github.com/lawrie/fpga_pio>
* axi_pio_cocotb, Verilog PIO with AXI4-Lite and cocotb (**no license: do not reuse code**):
  <https://github.com/jgpeiro/axi_pio_cocotb>
* VisualGDB PIO debugger (commercial, steps a real chip): <https://visualgdb.com/w/?p=7335>
