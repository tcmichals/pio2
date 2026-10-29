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

* pico-sdk (pioasm lives in `tools/pioasm`): <https://github.com/raspberrypi/pico-sdk>
* pico-examples (source of `src/programs/examples/pico/`): <https://github.com/raspberrypi/pico-examples>
* Verilator: <https://verilator.org/guide/latest/>
* cocotb: <https://docs.cocotb.org/>
* oss-cad-suite: <https://github.com/YosysHQ/oss-cad-suite-build>
* imgui-bundle (Dear ImGui, ImPlot, for Python): <https://pthom.github.io/imgui_bundle/>
* uv: <https://docs.astral.sh/uv/>

## Other PIO simulators and debuggers (prior art, see debugger.md)

Listed to compare against; check each project's current state and license before reusing anything.

* Wokwi RP2040 simulation, browser based, includes PIO: <https://wokwi.com/>
* rp2040pio, a Java PIO emulator with a GUI (soundpaint): <https://github.com/soundpaint/rp2040pio>
* rp2040-pio-emulator, Python (NathanY3G): <https://github.com/NathanY3G/rp2040-pio-emulator>
