# pio2 Interconnect: Host Link (SPI / Dual-SPI), Channels, AXI4-Lite, AXI-Stream, DMA, Trace

Status (2026-10-30): the tile's AXI4-Lite port (`[SPEC-AXI-04]`), the event FIFO (`[SPEC-TRC-01]`),
the tile split (`[SPEC-ICN-09]`), the DMA engine and rings (`[SPEC-DMA-*]`), the crossbar and the core
registers (`[SPEC-AXI-03]`) are built; the SPI link (`[SPEC-LINK-*]`) follows. Requirements carry tags; code that
implements one carries `@impl [SPEC-xxx-nn] docs/INTERCONNECT_SPECIFICATION.md`.

## 1. Idea

The PIO IP looks to the host like a small network interface: one SPI link (single or dual data
lines) carries **numbered channels**, like ports. Channel 0 is addressed register access (read and
write any register address); the other channels are **streams** (data to and from the PIO through
DMA rings, and the trace). An interrupt pin tells the host which channel needs service.

Inside the FPGA (decision 2026-10-29): **AXI4-Lite** is the control plane (registers) and
**AXI-Stream** the data plane (FIFO data, DMA, trace), everywhere, including the PIO tile's own
register port. There is **no Wishbone and no TLP in pio2**. The same buses face the Zynq PS directly,
and the verification can use proven AXI tools (`[SPEC-VER-01]`). AbstractX, which still uses Wishbone
and TLP inside, bridges to pio2 in its own wrapper (`[SPEC-ICN-09]`).

Block diagrams: [`overview.md`](overview.md) (whole FPGA, host link, tile and DMA, trace path).

### Block diagram

The block diagrams (whole FPGA, host link, tile and DMA, trace path) are kept in one place:
[`overview.md`](overview.md). This section keeps the trace sequence, because it is the
contract between `[SPEC-TRC-01]`, `[SPEC-TRC-02]` and `[SPEC-LINK-08]`.

On the Zynq the SPI link is not built: the PS GP port drives the core's AXI4-Lite slave, the AXI MCDMA
takes the core's stream output (trace and rings, channel in `TDEST`) into DDR, and the interrupt is a
PL-to-PS line (`[SPEC-ICN-11]`).

### How the trace reaches the host

barectf is **not** in the FPGA. The FPGA only makes small fixed records; the host turns them into CTF.

```mermaid
sequenceDiagram
    participant SM as PIO SM / DMA / link
    participant EVF as event FIFO
    participant DMA as DMA trace channel
    participant RING as ring 1 (trace)
    participant ARB as outbound arbiter
    participant FR as framer + PHY
    participant H as host driver
    SM->>EVF: event (type, src, data), timestamp taken this clock
    Note over EVF: no producer waits: no room = drop and count,<br/>then one DROPPED n record when idle
    EVF->>DMA: 64-bit records, narrowed to 32-bit words (AXI-Stream)
    DMA->>RING: RING mode, paced by the event FIFO's TVALID
    Note over RING: offers a frame at 14 records,<br/>or after its flush time (same rule as every stream)
    RING->>ARB: one STREAM_DATA frame, chan 1, TLAST on the last word
    Note over ARB: takes whole frames in turn with ch 2..9,<br/>ch 0 replies have their own buffer
    ARB->>FR: frame, framer adds header and CRC32
    FR-->>H: INT_n (ISR: RX frame ready)
    H->>FR: READ_FRAME window
    FR->>H: 12 B header + 112 B records + CRC32
    Note over H: check CRC and seq, extend 32-bit timestamps,<br/>write CTF packets (barectf schema)
```

So the trace is just channel 1: a stream like the DMA rings, merged with them **one whole frame at a
time** by the outbound arbiter, never interleaved inside a frame. The framer does not care which
channel a frame came from; the `chan` byte in the header tells the host. The registers that control
the trace (type mask, level, dropped count, flush time) sit in the `0x2000` window on the
AXI4-Lite crossbar, like every other block.

## 2. Buses

### `[SPEC-AXI-01]` Control Plane: AXI4-Lite
* **Requirement**: every register block is an AXI4-Lite slave: 32-bit data, byte addresses, `WSTRB`
  per byte, `PROT` ignored, responses `OKAY` or `SLVERR` (`DECERR` from the crossbar for an unmapped
  address). All five channels follow AXI4 rules (AMBA AXI and ACE spec, IHI 0022, section A3): a
  source keeps `VALID` and its payload stable until `READY`; `VALID` never waits for `READY`; write
  address and write data are accepted in any order or together; one `B` response per write, one `R`
  per read; no combinational path from an input `VALID` to an output `READY` that could deadlock.
* **Requirement**: every slave sustains one transaction per clock when its master and the crossbar allow
  it (back-to-back, no forced idle cycle), and holds at most `N_OUTSTANDING` (default 2) per direction.
* **Verified by**: `formal-check` (ZipCPU AXI4-Lite properties on every master and slave port),
  `cocotb-regression` (cocotbext-axi master / monitor, randomized `READY` / `VALID` gaps, 1000
  transactions per slave, functional coverage of the handshake orders)
* **Implementation Target**: `src/rtl/axi/`

### `[SPEC-AXI-02]` Data Plane: AXI-Stream
* **Requirement**: FIFO data, DMA transfers, link stream channels and the trace use AXI-Stream (AMBA
  AXI4-Stream spec, IHI 0051): 32-bit `TDATA`, `TVALID` / `TREADY`, `TLAST` at the end of a block
  (a DMA block, a link frame's payload, a trace packet), optional `TKEEP` for a partial last word,
  `TUSER` for errors where stated. `TVALID` and its data stay stable until `TREADY`.
* **Requirement (pacing)**: a PIO TX FIFO's `TREADY` is "not full" and an RX FIFO's `TVALID` is "not
  empty", so the AXI-Stream handshake is the data request (RP2040 DREQ) and nothing else is needed.
* **Verified by**: `formal-check` (ZipCPU AXI-Stream properties on every port), `cocotb-regression`
  (cocotbext-axi source / sink with random back-pressure, `TLAST` framing checked, no word lost or
  duplicated over 1000 blocks)
* **Implementation Target**: `src/rtl/axi/`, `src/rtl/dma/`

### `[SPEC-AXI-03]` AXI4-Lite Crossbar and Address Map
* **Requirement**: one master per build, the core boundary's AXI4-Lite slave (`[SPEC-ICN-11]`), driven
  by the host adapter: the SPI link's channel-0 master or the Zynq PS GP port. Slaves: PIO tile front
  end, DMA registers, event FIFO / trace registers, `ISR` / `IMR` and link registers; `DECERR` on an unmapped address; a slow slave never blocks a transaction to another one.
* **Requirement (address map, base 0x0000_0000, 4 KB windows)**:

  | Window | Slave |
  | :--- | :--- |
  | `0x0000` | PIO tile registers (as today: CTRL, FSTAT, FDEBUG, IRQ, SMx_*, TXFx/RXFx, INSTR_MEM) |
  | `0x1000` | DMA engine registers (per channel) |
  | `0x2000` | event FIFO / trace registers (`[SPEC-TRC-02]`) |
  | `0x3000` | core registers: `ID`, `VERSION`, `ISR`, `IMR`, per-channel status (`[SPEC-ICN-11]`) |
  | `0x3100` | link registers (`[SPEC-LINK-07]`): served by the SPI adapter itself, never reach the core |

* **Requirement (2026-10-30)**: the crossbar is Forencich `axil_crossbar` (`[SPEC-AXI-05]`) with three
  slaves: the tile (`0x0000`, 4 KB), the DMA engine (`0x1000`, 4 KB) and the core registers (`0x2000`,
  8 KB: trace window and core window). Each of our slaves uses one AXI4-Lite front end,
  `pio2_axil_regs`, that takes one write and one read per clock and answers whole-word writes only
  (`WSTRB` other than `4'hF`: no effect, `SLVERR`, as the tile); an offset with no register reads 0.
* **Verified by**: `cocotb-regression` (`tests/cocotb/test_pio2_core_cocotb.py`: every window reached,
  `DECERR` above `0x4000`, back-to-back transactions), `formal-check` (`tests/formal/pio2_axil_regs.sby`:
  ZipCPU `faxil_slave` on the front end)
* **Implementation Target**: `src/rtl/pio2_core.sv`, `src/rtl/axi/pio2_axil_regs.sv`

### `[SPEC-AXI-05]` Reuse Proven AXI Building Blocks
* **Requirement**: generic AXI blocks are reused, not rewritten, from:
  * Alex Forencich, `verilog-axi` and `verilog-axis` (<https://github.com/alexforencich>, MIT):
    AXI4-Lite crossbar / interconnect, AXI-Stream FIFOs (synchronous and asynchronous), arbiters and
    muxes, register slices;
  * ZipCPU (Dan Gisselquist), `wb2axip` (<https://github.com/ZipCPU/wb2axip>, Apache-2.0): the formal
    property files (`[SPEC-VER-01]`) and bridges such as `wbm2axilite` (used by AbstractX's wrapper,
    `[SPEC-ICN-09]`).
* **Requirement**: each is a git submodule under `third_party/` at a pinned commit, listed in
  `docs/third_party.md` with its link, author, license and what pio2 uses it for; pio2 instantiates
  them and does not copy or edit them. License obligations (MIT notice; Apache-2.0 license text, no
  modification, NOTICE if present) are met as that file describes, and re-checked when a pin moves. Our own checks (formal properties,
  cocotbext-axi tests, coverage) run on every place we integrate one, so reuse never means untested.
* **Requirement**: pio2 writes only what is specific to it: the PIO tile, the SPI PHY and framer, the
  channel mux, the DMA engine, the event FIFO.
* **Verified by**: `cocotb-regression` (each integrated block under our tests), `formal-check`
* **Implementation Target**: `third_party/`, `docs/third_party.md`, `LICENSES/`, `src/rtl/`

### `[SPEC-AXI-04]` The PIO Tile's Register Port Is AXI4-Lite
* **Requirement**: `asp_pio_tile`'s register port is an AXI4-Lite slave (`[SPEC-AXI-01]`) with the same
  register map as before (CTRL, FSTAT, FDEBUG, IRQ, SMx_*, TXFx / RXFx, INSTR_MEM). Its Wishbone port is
  removed; pio2 contains no Wishbone. AbstractX connects its Wishbone master through a Wishbone to
  AXI4-Lite bridge in its own wrapper (`[SPEC-ICN-09]`).
* **Requirement (front end)**: write address and write data are accepted in either order or together;
  one register access reaches the register block at a time (reads and writes alternate when both
  wait); the next request is accepted only after the previous response was taken, so a register with
  a side effect (TX FIFO write, RX FIFO read, SMx_INSTR) acts exactly once per transaction. Registers
  are whole 32-bit words: a write with `WSTRB` other than `4'hF` changes nothing and answers `SLVERR`.
* **Requirement**: the move keeps behaviour: every existing tile test (tile, conformance, DShot, ESC
  serial) passes through the AXI4-Lite port, driven by cocotbext-axi, before and after.
* **Verified by**: `cocotb-regression` (all tile suites through the AXI4-Lite port, on Verilator and
  Icarus), `formal-check` (ZipCPU `faxil_slave` on the tile port, `tests/formal/pio_tile_axil.sby`:
  BMC depth 24 and, since 2026-10-30, k-induction, which closes at k = 8 with the port invariants in
  `tests/formal/pio_tile_axil.sv`)
* **Implementation Target**: `src/rtl/asp_pio_tile.sv`

## 3. Host link

### `[SPEC-LINK-01]` SPI PHY: Single and Dual, Chosen per Window
* **Requirement**: SPI mode 0. Each CS_n window starts with one command byte on IO0 (always single):
  `0x01 WRITE_FRAME` (host sends one frame, `[SPEC-LINK-02]`) or `0x02 READ_FRAME` (FPGA returns the
  next queued frame, or an empty frame of length 0 when none is queued), with bit 7 `D` selecting the
  rest of the window: single (MOSI = IO0, MISO = IO1) or dual (IO0 and IO1 both carry data, 2 bits
  per clock; one turnaround clock before the FPGA drives). The FPGA returns the low byte of `ISR`
  during the command byte, so every window also tells the host what is pending.
* **Requirement (clocking, 2026-10-29; handover revised 2026-10-30)**: SCK clocks the PHY's shift
  registers directly (its own clock domain), never sampled by the system clock; SCK is independent of
  f_sys, limited only by the pins and the board wiring. Bytes cross between the domains in **frame
  buffers** (two per direction, `[SPEC-LINK-07]`): during a window the SCK domain writes the bytes of a
  received frame into one buffer, or reads the bytes of the frame to send from one; the system domain
  hands buffers over only while CS_n is high. The only synchronised signals are CS_n and a toggle per
  window (2-flop synchronisers); every multi-bit value that crosses is stable while it is read (written
  by one side only while the other side cannot look at it). So a frame never crosses half written, and
  no asynchronous FIFO is needed.
* **Requirement (host timing in system clocks)**: CS_n low to the first SCK rising edge >= 3 f_sys
  clocks; CS_n high between windows >= 8 f_sys clocks; SCK idles low and does not toggle while CS_n is
  high.
* **Requirement (wire, 2026-10-30)**: bits MSB first. Single: the host drives IO0, the FPGA drives IO1
  for the whole window (the command byte, then data or zeros); each side samples on the SCK rising edge
  and changes after the falling edge. Dual (like SPI flash dual I/O): two bits per clock, IO1 the higher
  of each pair (7, 5, 3, 1), IO0 the lower; for `READ_FRAME` one turnaround clock follows the command
  byte (nobody drives), then the FPGA drives both lines. The `ISR` byte sent during the command byte is
  the link's `ISR` (`[SPEC-LINK-07]`), taken while CS_n was high.
* **Requirement (windows)**: `WRITE_FRAME`: the host sends exactly one frame; bytes beyond 128 are
  ignored and a frame that is cut short fails its length or CRC check. `READ_FRAME`: the FPGA sends the
  next frame to the host (channel-0 replies first, `[SPEC-LINK-08]`), then zeros; with no frame queued
  every byte is 0 (an `op` of 0 means "no frame"). The host reads the 8-byte header and then the rest of
  the frame in the same window. A frame counts as delivered only when the window read all of it; a
  window that ends early leaves it queued, so the next `READ_FRAME` sends it again (same `seq`).
* **Requirement (throughput)**: at least **10 MB/s of payload** on Dual SPI (about 50 MHz SCK: 100 Mbit/s
  raw, ~11 MB/s after the 12-byte header and CRC per 128-byte frame), sustained in both directions on a
  Tang Nano 20K, with the host and wiring stated. Single SPI gives half. Quad SPI is a later option if
  more is needed.
* **Rationale**: sampling SCK at f_sys / 4 (the first draft) caps a 27 MHz Tang at ~1.5 MB/s on Dual
  SPI, below the goal; the internal buses are not the limit (32-bit AXI-Stream at 27 MHz is
  ~108 MB/s).
* **Requirement**: pins as separate `_i`, `_o`, `_oe` signals at the IP boundary; tristate only in
  the board top (the same rule the PIO pins follow).
* **Verified by**: `cocotb-regression` (`tests/cocotb/test_link_cocotb.py`: single and dual, SCK
  unrelated to f_sys (slower and faster), randomized clock ratios, short read windows resent),
  `board-bringup` (10 MB/s measured, SCK limit per board)
* **Implementation Target**: `src/rtl/link/pio2_spi_phy.sv`

### `[SPEC-LINK-02]` Frames: At Most 128 Bytes, CRC32, Like a NIC
* **Requirement**: the link carries **frames** of at most 128 bytes, framed by CS_n. A framer on each
  side checks and adds a CRC32 (CRC-32/ISO-HDLC, the Ethernet polynomial `0x04C11DB7`, reflected,
  init and final XOR `0xFFFFFFFF`) over header and payload. A frame whose CRC fails is dropped whole
  and counted; nothing of it reaches a bus or a ring.
* **Requirement (frame layout, little endian)**:

  | Offset | Size | Field |
  | :--- | :--- | :--- |
  | 0 | 1 | `op` (`[SPEC-LINK-03]`) |
  | 1 | 1 | `chan` (channel / port number) |
  | 2 | 1 | `flags` (bit 0 constant address for register ops; bit 1 dual data phase for the reply) |
  | 3 | 1 | `seq` (sequence number; replies echo it, so the host matches answers and sees loss) |
  | 4 | 2 | `len` payload length in bytes (0..116, multiple of 4 for word data) |
  | 6 | 2 | reserved, 0 |
  | 8 | 4 | `addr` (register ops only; otherwise absent) |
  | 8 or 12 | `len` | payload |
  | end | 4 | CRC32 |

  So a frame carries at most 116 bytes (29 words) of payload with no address, 112 bytes (28 words)
  with one; larger transfers are several frames.
* **Requirement (ops, codes 2026-10-30)**:

  | Code | Op | Request payload | Reply |
  | :--- | :--- | :--- | :--- |
  | `0x01` | `REG_READ` | one u32: words to read, 1..28 | `REG_READ`, same `seq` and `addr`, the words |
  | `0x02` | `REG_WRITE` | the words to write, 1..28 | `ACK` (one u32, 0) or `NAK` |
  | `0x03` | `STREAM_DATA` | words, 1..29, channel 2..9 (to the host: channels 1..9) | none on success; `NAK` on an error |
  | `0x04` | `STATUS` | none | `STATUS`: core `ISR`, link `ISR`, `{TXQ_LEVEL[15:8], RXQ_LEVEL[7:0]}`, then the DMA `LEVEL` register (`{free, level}`) of channels 1..9: 12 words |
  | `0x05` | `ACK` | | one u32, 0 |
  | `0x06` | `NAK` | | one u32 error code: 1 bad op, 2 bad channel, 3 bus error (`resp << 8` added), 4 no credit, 5 bad length |

  `addr` is present exactly for `REG_READ` / `REG_WRITE`, which use channel 0; `STREAM_DATA` never
  uses channel 0. `reserved` must be 0 and flag bits 2..7 are reserved. `len` is a multiple of 4.
  Addresses `0x3100..0x31FF` are the link's own registers (`[SPEC-LINK-07]`); every other address is a
  transaction on the core's AXI4-Lite slave. Replies carry channel 0 and echo the request's `seq`;
  frames to the host on channels 1..9 number their `seq` per link (wrapping).
* **Requirement (transactions)**: the SPI master always drives the clock, so a transaction is: host
  writes one frame (command), the FPGA queues the reply frame, the host reads it in a later CS_n
  window (signalled by `INT_n` or by polling `STATUS`). Host-to-FPGA and FPGA-to-host frames use
  separate queues, so the host can write the next command while a reply is waiting.
* **Verified by**: `cocotb-regression` (`tests/cocotb/test_link_cocotb.py`: bad CRC dropped whole and
  counted, bad length, every op and NAK code), `host-tests` (`tests/test_host_frame.py`: CRC32 known
  vectors, encode / decode, limits)
* **Implementation Target**: `src/rtl/link/pio2_link.sv`, `src/rtl/link/pio2_crc32.sv`, `src/pio2/host/frame.py`

### `[SPEC-LINK-07]` NIC-Style Register Model
* **Requirement**: the link block is controlled like a small NIC, through registers in the link
  window (`0x3100`, served inside the SPI adapter, 2026-10-30; `ID`, `VERSION` and the core's `ISR` /
  `IMR` are at `0x3000`, `[SPEC-ICN-11]`) that both the host (as `REG_*` frames) and the simulator can
  read. The link's own `ISR` uses bits 0 (RX frame ready), 1 (TX queue space), 5 (CRC error) and 6
  (overflow); bit 2 there mirrors the core's interrupt, so the `ISR` byte at the start of every window
  shows all pending causes in one place:

  | Register | Meaning |
  | :--- | :--- |
  | Address | Register | Meaning |
  | :--- | :--- | :--- |
  | `0x3100` | `LINK_CTRL` | write 1 to bit 1: drop every queued frame; `[15:8]` frame buffers per direction (read) |
  | `0x3104` | `LINK_ISR` | W1C: `RX_READY[0]` (a frame waits for `READ_FRAME`), `TX_SPACE[1]` (a buffer can take a `WRITE_FRAME`), `CORE[2]` (the core's interrupt, read only), `CRC_ERR[5]`, `OVERFLOW[6]` |
  | `0x3108` | `LINK_IMR` | mask for `LINK_ISR` |
  | `0x310C` | `TXQ_LEVEL` | frames waiting to go to the host |
  | `0x3110` | `RXQ_LEVEL` | received frames not yet handled |
  | `0x3114` | `RX_FRAME` | header of the next frame to the host, without taking it: `{chan[23:16], len[15:0]}` |
  | `0x3118` .. `0x3124` | `CRC_ERR`, `LEN_ERR`, `OVERFLOW`, `DROPPED` | saturating counters, a write clears; `OVERFLOW` = a `WRITE_FRAME` with no free buffer or a stream frame beyond its credit, `DROPPED` = frames answered with `NAK` 1 / 2 / 5 |

  ID, version and the per-channel thresholds are the core's (`0x3000`, DMA `THR`).

* **Requirement (frame buffers)**: two frame buffers per direction (128 bytes each) so the host can
  stream back to back, reported in `LINK_CTRL`. Toward the host, one buffer holds channel-0 replies and
  one holds stream frames (`[SPEC-LINK-08]`).
* **Verified by**: `cocotb-regression` (`tests/cocotb/test_link_cocotb.py`: register map, counters
  count and clear)
* **Implementation Target**: `src/rtl/link/pio2_link.sv`

### `[SPEC-LINK-03]` Channels Are Ports
* **Requirement**: channel numbers (frame `chan`): channel 0 = registers (addressed); channel 1 = trace stream (out, `[SPEC-TRC-*]`);
  channels 2..9 = the eight DMA stream rings (`[SPEC-DMA-*]`), direction from the DMA channel's
  configuration. Unused channels answer with an error status.
* **Requirement**: a stream channel never needs an address: like a socket, the host only says "give me
  up to N words" or "take these N words".
* **Verified by**: `cocotb-regression` (`tests/cocotb/test_link_cocotb.py`: unused channels answer
  `NAK` 2)
* **Implementation Target**: `src/rtl/link/pio2_link.sv`

### `[SPEC-LINK-08]` Outbound Frames Are Merged Whole
* **Requirement**: frames to the host come from channel 0 (register replies, `ACK` / `NAK`, `STATUS`),
  channel 1 (trace, `[SPEC-TRC-02]`) and channels 2..9 (out streams). One arbiter fills the outbound
  frame buffers (`[SPEC-LINK-07]`) one **whole frame** at a time; a frame is never interleaved with
  another, and the framer adds the header and CRC32 the same way for every channel.
* **Requirement (order)**: one outbound buffer is reserved for channel 0, so a register reply never
  waits behind stream data; channels 1..9 arrive already merged from the core's AXI-Stream output
  (`[SPEC-ICN-11]`: whole `TLAST` blocks, round robin, so the trace cannot starve a stream and a stream
  cannot starve the trace) and fill the other buffers, one block per frame. A stream channel offers a
  frame when it has a full frame of words or, with fewer, after its flush time.
* **Rationale**: whole frames keep each frame's CRC and `chan` meaningful; the reserved buffer keeps
  register access latency bounded while streams run at full rate.
* **Verified by**: `cocotb-regression` (`tests/cocotb/test_link_cocotb.py`: streams and register
  replies at once, every frame whole, a reply never waits behind stream frames)
* **Implementation Target**: `src/rtl/link/pio2_link.sv`

### `[SPEC-LINK-04]` Flow Control by Credits
* **Requirement**: the `STATUS` reply returns, per channel, words available (out) or words free (in). The host
  never sends more than the free count; the FPGA never returns more than it has. Neither side can
  overflow the other; an attempted overflow is a counted error, not data loss.
* **Requirement (2026-10-30)**: before a `STREAM_DATA` frame reaches the core, the link reads the
  channel's free count (DMA `LEVEL`); a frame larger than that is answered `NAK` 4 and counted in
  `OVERFLOW`, and nothing of it is written, so the host can send it again.
* **Verified by**: `cocotb-regression` (`tests/cocotb/test_link_cocotb.py`: frame beyond the credit
  refused whole, counted, resent), `host-tests` (`tests/test_host_driver.py`: the driver never exceeds
  the credit)
* **Implementation Target**: `src/rtl/link/pio2_link.sv`, `src/pio2/host/driver.py`

### `[SPEC-LINK-05]` Interrupt Pin and Status
* **Requirement**: `INT_n` (active low, level) = the core's interrupt or any bit of the link's (`ISR`
  and `IMR`) (`[SPEC-LINK-07]`, `[SPEC-ICN-11]`):
  a reply frame queued, an out stream at or above `STREAM_THR`, an in stream with at least
  `STREAM_THR` free, a DMA channel done or in error, a link error, trace dropped. The host reads `ISR`
  (in the first byte of any window, or with `STATUS`), services the causes, and writes 1 to clear
  them; a cause that is still true sets its bit again, so no interrupt is lost.
* **Verified by**: `cocotb-regression` (`tests/cocotb/test_link_cocotb.py`: each link cause and the
  core's interrupt set INT_n; W1C; a still-true cause re-asserts)
* **Implementation Target**: `src/rtl/link/pio2_link.sv`, `src/rtl/pio2_core_regs.sv`

### `[SPEC-LINK-06]` The Same Link in Simulation
* **Requirement**: the simulator (`docs/DEBUGGER_SPECIFICATION.md`) can drive the link at the SPI pins (bit for bit
  what a host does) or call the channels directly (fast). Both go through the same Python host
  driver, so a host program tested against the simulator runs unchanged on a board.
* **Verified by**: `host-tests` (`tests/test_host_driver.py`: same results through pins and direct)
* **Implementation Target**: `src/pio2/host/driver.py`

## 4. DMA

### `[SPEC-DMA-01]` Channels Paced by the PIO FIFOs
* **Requirement**: eight PIO channels plus one trace channel. Each PIO channel moves 32-bit words
  between its ring and one PIO FIFO (TX of SM n, or RX of SM n); the trace channel moves event
  records from the event FIFO into ring 1 (`[SPEC-TRC-02]`). Every channel is paced by that FIFO's
  data request (TX not full / RX not empty), as RP2040 DMA is paced by DREQ; both sides are
  AXI-Stream (`[SPEC-AXI-02]`), so the FIFO's handshake is the pacing.
* **Requirement (numbering)**: a channel has the number of its link channel (`[SPEC-LINK-03]`): channel
  1 is the trace, channels 2..9 are the PIO channels. Each PIO channel has a direction: `OUT` (PIO RX
  FIFO -> ring -> host) or `IN` (host -> ring -> PIO TX FIFO).
* **Requirement (modes)**: `ONE_SHOT` (N words after a start, then done), `RING` (continuous),
  `TRIGGERED` (each trigger allows one more block of N words; trigger = rising edge of a PIO IRQ flag or
  of an input pin, e.g. IMU DRDY, or a software trigger), `DIRECT` (lowest latency, low rate: on the
  way out each word is its own block, offered at once without waiting for `FRAME_WORDS` or `FLUSH`; on
  the way in as `RING`. Words still pass through the ring FIFO, a few clocks, so the core's output
  stays AXI-Stream legal even when the register port reads the same RX FIFO; decision 2026-10-30). Mode, direction, SM, N and the trigger are registers; a channel acts only while enabled.
  When two enabled channels name the same FIFO, the lower-numbered one owns it. Register access to the
  FIFOs (TXFx / RXFx) keeps working; in a clock where the register port writes TXFx or reads RXFx the
  tile holds off the stream (`[SPEC-ICN-09]`), so no word is lost or duplicated.
* **Requirement (counters and events)**: per channel: words moved (wraps), triggers, underruns and
  overruns (saturating, cleared by a write). An *underrun* starts when an active `IN` channel's SM has
  drained its TX FIFO (TX FIFO empty) while the ring is empty, after the channel moved at least one word
  since it was started; an *overrun* starts when an `OUT` channel's RX FIFO holds a word while the ring
  is full. Each start of one is counted once and gives a trace event (`[SPEC-TRC-01]` `0x0A` / `0x0B`,
  `src` = channel, `data` = count); a trigger gives `0x0C` (`data` = trigger count); a `ONE_SHOT` or
  `TRIGGERED` block gives `0x08` at its first word and `0x09` when complete (`data` = N). Each channel
  is one event producer; events of one channel in the same clock are offered one per clock in the order
  trigger, begin, done, underrun, overrun, and a repeat of a pending one is merged and counted.
* **Requirement (registers, window `0x1000`, block per channel at `0x1000 + 0x40 * chan`, chan 1..9)**:

  | Offset | Register | Fields |
  | :--- | :--- | :--- |
  | `0x00` | `CFG` | `EN[0]`, `DIR[1]` (0 `OUT`, 1 `IN`), `MODE[3:2]` (0 `ONE_SHOT`, 1 `RING`, 2 `TRIGGERED`, 3 `DIRECT`), `SM[5:4]`, `TRIG_PIN[6]` (0: IRQ flag, 1: input pin), `TRIG_IDX[12:8]` |
  | `0x04` | `COUNT` | N, words per block `[15:0]` (0 = 65536) |
  | `0x08` | `LEVEL` | words in the ring `[15:0]`, free words `[31:16]` (read only) |
  | `0x0C` | `STATUS` | `DONE[0]` (read; cleared through `DMA_DONE`, `[SPEC-ICN-11]`), `BUSY[1]`, `REMAINING[31:16]` |
  | `0x10` | `MOVED` | words moved (read; write clears) |
  | `0x14` | `UNDERRUNS` | saturating (write clears) |
  | `0x18` | `OVERRUNS` | saturating (write clears) |
  | `0x1C` | `TRIGGERS` | saturating (write clears) |
  | `0x20` | `FLUSH` | `OUT` / trace: clocks a partial frame may wait before it is offered `[23:0]` (0 = whole frames only) |
  | `0x24` | `THR` | stream threshold in words `[15:0]` for `[SPEC-LINK-05]` (0 = off) |
  | `0x28` | `CTRL` | write 1: `RESET_RING[0]`, `START[1]` (`ONE_SHOT`: N more words), `SW_TRIGGER[2]` |

  `0x1000` itself is `DMA_CAPS` (read): ring words per channel `[15:0]`, frame words `[23:16]`. For
  channel 1 only `EN`, `LEVEL`, `MOVED`, `FLUSH`, `THR` and `CTRL.RESET_RING` apply (it is always a
  `RING` `OUT` channel fed by the event FIFO). `ONE_SHOT` starts when `EN` goes to 1 or on `START`.
  `RESET_RING` empties the ring; on an `OUT` ring it waits until the block being sent is complete, so a
  block on the output stream is never cut short.
* **Verified by**: `cocotb-regression` (`tests/cocotb/test_pio2_dma_cocotb.py`: each mode, DREQ pacing,
  underrun / overrun counted once per start, register FIFO access mixed with DMA, 1000-block steady
  state)
* **Implementation Target**: `src/rtl/dma/pio2_dma.sv`, `src/rtl/dma/pio2_dma_chan.sv`

### `[SPEC-DMA-02]` Rings Connect DMA and Link Streams
* **Requirement**: each channel's ring is one producer / one consumer (link side and DMA side). A
  ring is its own block-RAM FIFO (Forencich `axis_fifo`, `RING_WORDS` deep, default 256, a build
  parameter), so channels never wait for each other on a shared RAM; its level and free count (an
  exact occupancy counter at the ring's two ports) are registers and feed the link credits
  (`[SPEC-LINK-04]`) and the interrupt thresholds (`[SPEC-LINK-05]`).
* **Requirement (out to the host)**: an `OUT` ring (and ring 1) offers one block to the core's output
  stream (`[SPEC-ICN-11]`) when it holds `FRAME_WORDS` words (default 28, one link frame,
  `[SPEC-LINK-02]`), when the ring is full (a ring smaller than a frame), or when it holds fewer and
  the oldest has waited `FLUSH` clocks; the block has
  `TLAST` on its last word and `TDEST` = channel. Blocks of all out rings are merged whole, round robin.
* **Requirement (in from the host)**: the core's input stream is split by `TDEST` to the `IN` rings
  (`DIRECT`: to the TX FIFO). A word for a channel that is not an enabled `IN` channel is dropped and
  counted (`IN_DROPPED`, `[SPEC-ICN-11]`). A full ring back-pressures the input stream; the SPI adapter
  checks the credit before it forwards a frame, so this never blocks another channel.
* **Requirement (examples it must carry)**: DShot / bidirectional DShot frames out on a RING channel;
  ESC serial bytes in both directions; IMU auto-DMA: DRDY triggers a PIO SPI burst, a TRIGGERED OUT
  channel moves each sample block into a ring, the host collects batches on interrupt.
* **Verified by**: `cocotb-regression` (`tests/cocotb/test_pio2_core_cocotb.py`: DShot ring in, ESC
  serial both ways, IMU triggered burst out, end to end through the core's streams)
* **Implementation Target**: `src/rtl/dma/pio2_dma.sv`, `src/rtl/dma/pio2_ring.sv`

## 5. Trace (barectf)

### `[SPEC-TRC-01]` Event FIFO With Fixed Records
* **Requirement**: `N_PROD` producers (PIO tile, DMA engine, link, ...) each offer one record at a time
  (`valid` with `type`, `src`, `data`; no `ready`: **no producer ever waits**). The timestamp is taken
  in the clock the event is offered. Each producer has a one-record holding slot; the slots drain into
  one FIFO, one record per clock, by fixed priority (producer 0 highest: errors > DMA > PIO > link). An
  event offered while its producer's slot is still full is **dropped and counted**; when the drop count
  is non-zero, a `DROPPED` record (`data` = count, saturating; timestamp = the clock of the first lost
  event) is written in a clock when no slot is waiting (lowest priority), so reporting losses never
  causes more of them; the saturating 32-bit total stays exact. Records leave in priority order, not
  strictly in time order; each carries its own timestamp and the host orders them (`[SPEC-TRC-02]`).
  Type codes above `0x1F` are ignored (not recorded, not counted). In `pio2_core` (2026-10-30) the
  producers are the eight DMA channels (0..7, highest) and then the trace tap's five (8..12); the link
  is outside the core (`[SPEC-ICN-11]`) and counts its frame errors in its own registers
  (`[SPEC-LINK-07]`), so type `0x10` is reserved.
* **Requirement (record, 64 bits)**: `[15:0] data` (PC, word count, IRQ number, drop count, ...),
  `[23:16] src` (SM or DMA channel), `[31:24] type`, `[63:32] timestamp` (system clocks, wraps; the host
  extends it to 64 bits). On the link a record is two 32-bit words, bits `[31:0]` first.
* **Requirement (types)**:

  | Code | Type | Code | Type |
  | :--- | :--- | :--- | :--- |
  | `0x01` / `0x02` | SM stall begin / end (`data`: reason, PC) | `0x08` / `0x09` | DMA burst begin / done |
  | `0x03` / `0x04` | IRQ flag set / clear | `0x0A` / `0x0B` | DMA underrun / overrun |
  | `0x05` / `0x06` | FIFO full / empty edge | `0x0C` | DMA trigger |
  | `0x10` | link frame error | `0x1F` | `DROPPED` (`data` = records lost, saturating) |

  Per-instruction events exist in simulation only (`[SPEC-DBG-13]`): at full speed they would fill the
  FIFO. Field layouts per type live in the barectf schema (`[SPEC-TRC-02]`).
* **Requirement (enable)**: a 32-bit type mask, one bit per type code `0x00..0x1F`; a masked event is not
  recorded and not counted as dropped, so tracing off costs no bandwidth. `DROPPED` is always enabled.
* **Requirement (status)**: FIFO level and a saturating 32-bit total of dropped records, for the
  `0x2000` register window.
* **Requirement (reuse, `[SPEC-AXI-05]`)**: the FIFO is Forencich `axis_fifo` (64-bit, depth a build
  parameter, default 512 records); pio2 writes only the slots, the priority and the drop logic. The
  record stream is 64 bits wide inside; a Forencich `axis_adapter` narrows it to the 32-bit DMA stream
  (bits `[31:0]` first).
* **Verified by**: `cocotb-regression` (`tests/cocotb/test_pio2_event_fifo_cocotb.py`: priority,
  drop-and-count with a `DROPPED` record in the right place, mask, random back-pressure, no record lost
  or duplicated that was not counted, 1000-event steady state)
* **Implementation Target**: `src/rtl/trace/pio2_event_fifo.sv`

### `[SPEC-TRC-03]` PIO Trace Sources
* **Requirement**: the tile has a read-only observation port (per SM: PC, stalled, TX FIFO empty, RX
  FIFO full; the 8 IRQ flags) and `pio2_trace_tap` turns its edges into `[SPEC-TRC-01]` records, one
  producer per SM plus one for the IRQ flags:

  | Edge | Record (`type`, `src`, `data`) |
  | :--- | :--- |
  | SM n starts stalling | `0x01`, n, PC |
  | SM n stops stalling | `0x02`, n, PC of the instruction that was stalled |
  | IRQ flag i set / cleared | `0x03` / `0x04`, 0xFF, i |
  | SM n RX FIFO becomes full | `0x05`, n, 1 |
  | SM n TX FIFO becomes empty | `0x06`, n, 0 |

* **Requirement**: when several edges of one producer happen in the same clock they are offered one per
  clock in the table's order; each record carries the PC captured at its edge, and its timestamp is
  at most 3 clocks late (8 for IRQ edges). An edge that repeats before its first record was offered
  is merged into it and counted (`o_merged`); every other loss happens in the event FIFO, where it is
  counted too (`[SPEC-TRC-01]`).
* **Requirement**: `pio2_core` contains the tile, the tap and the event FIFO; until the DMA engine exists
  the record stream is a port of the core, and the debugger reads it directly.
* **Verified by**: `host-tests` (`tests/test_trace.py`: blink's stall on pull, IRQ set / clear,
  FIFO edges, timestamps against the per-clock trace)
* **Implementation Target**: `src/rtl/trace/pio2_trace_tap.sv`, `src/rtl/pio2_core.sv`, `src/rtl/asp_pio_tile.sv`

### `[SPEC-TRC-02]` Records Become barectf CTF on the Host
* **Requirement (path, project owner 2026-10-29)**: the trace goes through the DMA engine like any other
  stream, so it works the same on every host: the event FIFO is the source of a dedicated DMA trace
  channel (`[SPEC-DMA-01]`) in `RING` mode, which moves the records into ring 1. Ring 1 is the
  channel-1 out stream: on the SPI link it reaches the host through the outbound arbiter
  (`[SPEC-LINK-08]`), up to 14 records (112 bytes) per `STREAM_DATA` frame; on the Zynq through
  the core's stream output and the AXI MCDMA (`[SPEC-ICN-11]`). It offers a frame when full or after its flush
  time, so a quiet trace still reaches the host. There is no separate trace packer.
* **Rationale**: one path for SPI and Zynq; the ring gives deep buffering (BRAM) in front of a slow
  link; levels, thresholds, interrupts and counters are the ones every stream already has.
* **Requirement**: channel 1 streams the records; the host driver converts them into CTF 1.8 packets
  with a barectf schema (`src/pio2/trace/barectf_config.yaml`, aligned with AbstractX's
  `pio_event`), so simulation and board traces are the same and open in babeltrace2, Trace Compass
  and AbstractX Studio.
* **Verified by**: `host-tests` (records -> CTF, read back with babeltrace2)
* **Requirement (order)**: the host sorts records by their extended timestamp before writing CTF
  packets, because the event FIFO emits them in priority order (`[SPEC-TRC-01]`).
* **Requirement (registers, window `0x2000`, 2026-10-30)**: `0x2000 TRACE_MASK` (type mask,
  `[SPEC-TRC-01]`; reset 0 = tracing off, so a board does not stream trace frames nobody asked for),
  `0x2004 TRACE_LEVEL` (records in the event FIFO), `0x2008 TRACE_DROPPED` (saturating total; write
  clears), `0x200C TRACE_MERGED` (trace tap merges; write clears), `0x2010 TIMESTAMP` (the record
  clock, read). The record stream reaches ring 1 through a Forencich `axis_adapter` (64 -> 32 bits,
  bits `[31:0]` first); ring 1 is DMA channel 1 (`[SPEC-DMA-01]`).
* **Implementation Target**: `src/rtl/dma/pio2_dma.sv` (trace channel), `src/pio2/trace/ctf.py`, `src/pio2/trace/barectf_config.yaml`

## 6. Integration and tests

### `[SPEC-ICN-09]` Tile Split: TLP Adapters Out, AXI-Stream FIFO Ports and Event Port In
* **Requirement**: `asp_pio_tile` loses its TLP ingress / egress adapters; they move to an AbstractX-side
  wrapper (with AbstractX's Wishbone to AXI4-Lite bridge, `[SPEC-AXI-04]`) that keeps AbstractX's TLP
  router, its Wishbone fabric and its TLP-path test working unchanged. The tile gains,
  per state machine, an AXI-Stream TX input and RX output on its FIFOs (`[SPEC-AXI-02]` pacing) and an
  event output for `[SPEC-TRC-01]`. Register access to the FIFOs stays (TXFx / RXFx) for software that
  does not use DMA.
* **Requirement (ports, 2026-10-30)**: per SM n, `s_axis_tx_*[n]` (32-bit `TDATA`, `TVALID`, `TREADY` =
  TX FIFO not full) and `m_axis_rx_*[n]` (`TVALID` = RX FIFO not empty, `TDATA` = its head). In a clock
  where the register port writes TXFn the TX `TREADY` is low, and in a clock where it reads RXFn the RX
  `TVALID` is low, so register and stream access never take or lose the same word. The observation
  port (`[SPEC-TRC-03]`) gains `o_obs_active` (per SM: executing, not stalled on a clock divider).
* **Requirement (what leaves the tile)**: the TLP ingress queue and egress packetizer
  (`asp_pio_tlp_ingress`, `asp_pio_tlp_shim`), their registers (`OUT_CFG 0x0C`, `OUT_TIMEOUT 0x90`,
  `IN_CFG 0x94`, `IN_STATUS 0x98`), the software trigger `SMx + 0x1C` and the tile's timestamp input
  move to AbstractX (`rtl/pio_tlp/`), which serves those offsets in its wrapper before the AXI4-Lite
  bridge and builds TLPs from the stream ports (formerly SPEC-PIO-04 and SPEC-PIO-10, now in
  AbstractX's specification). In pio2 those offsets read 0 and writes do nothing.
* **Verified by**: `cocotb-regression` (`tests/cocotb/test_asp_pio_stream_cocotb.py`: TX and RX through
  the stream ports with random back-pressure, mixed with TXF / RXF register access, every word exactly
  once; all other tile suites unchanged), AbstractX (its `asp_pio_wrap` TLP-path test)
* **Implementation Target**: `src/rtl/asp_pio_tile.sv`

### `[SPEC-ICN-11]` Host Adapters: SPI Link for Tang, Native AXI for Zynq
* **Requirement**: the core (AXI4-Lite crossbar, DMA engine, rings, event FIFO, PIO tile) is the same on
  every board; only the host adapter changes, and a build has exactly one: the **SPI link**
  (`[SPEC-LINK-*]`, an external CPU over SPI / Dual-SPI: Tang Nano 9K / 20K with a Pico, an SBC or the
  Cubie) or the **Zynq-7000** PS (no SPI at all). Diagrams: `docs/overview.md`.
* **Requirement (which adapter, project owner 2026-10-29)**: the SPI link is optional, chosen per board:

  | Board | SPI link | Zynq adapter | Why |
  | :--- | :--- | :--- | :--- |
  | Tang Nano 9K / 20K (Gowin, no CPU on chip) | **required** | none | the host is outside the FPGA (Pico, SBC, Cubie) |
  | Zynq-7000 | **not built** | **required** | the ARM cores are on the chip, on AXI |
  | any other FPGA with an external host | required | none | as the Tang boards |
  | simulation (Verilator) | at the pins, or bypassed (`[SPEC-LINK-06]`) | none | same driver either way |

* **Requirement (core boundary, 2026-10-29)**: `pio2_core` has exactly four host-facing interfaces, and
  both host adapters attach to them unchanged:
  1. one AXI4-Lite slave (all registers, `[SPEC-AXI-03]` map);
  2. one AXI-Stream input, 32-bit, channel number in `TDEST` (2..9), `TLAST` per block;
  3. one AXI-Stream output, 32-bit, channel number in `TDEST` (1..9), whole `TLAST` blocks merged
     inside the core by Forencich `axis_arb_mux` (trace ring and out rings, round robin);
  4. one interrupt (any `ISR` and `IMR`).
* **Requirement (core registers, window `0x3000`, 2026-10-30)**:

  | Offset | Register | Meaning |
  | :--- | :--- | :--- |
  | `0x3000` | `ID` | `0x50494F32` ("PIO2") |
  | `0x3004` | `VERSION` | protocol version `0x0001_0000` (major `[31:16]`, minor `[15:0]`) |
  | `0x3008` | `ISR` | W1C: `STREAM[2]` (a channel at its threshold), `DMA_DONE[3]`, `DMA_ERR[4]` (underrun / overrun), `TRACE_DROPPED[7]`, `PIO_IRQ[8]` (tile IRQ flags 0..3); bits 0, 1, 5, 6 are the link's (`[SPEC-LINK-07]`) and read 0 here |
  | `0x300C` | `IMR` | interrupt mask; the core's interrupt = any (`ISR` and `IMR`) |
  | `0x3010` | `STREAM_PEND` | bit c: enabled channel c with `THR` != 0 and (`OUT`: level >= `THR`; `IN`: free >= `THR`) (read) |
  | `0x3014` | `DMA_DONE` | bit c: channel c finished a block (W1C) |
  | `0x3018` | `DMA_ERR` | bit c: channel c had an underrun or overrun (W1C) |
  | `0x301C` | `IN_DROPPED` | input-stream words for no enabled `IN` channel (saturating; write clears) |

  A cause that is still true sets its `ISR` bit again in the next clock, so a W1C never loses an
  interrupt (`[SPEC-LINK-05]`).
* **Requirement (SPI adapter)**: PHY, framers, CRC32, an AXI4-Lite master for channel 0, and the
  frame / block conversion (channel in the frame header <-> `TDEST`); it puts channel-0 replies first
  (`[SPEC-LINK-08]`) and drives `INT_n` from the interrupt. `pio2_spi_top` is the adapter and the core
  together, with the SPI and PIO pins as `_i` / `_o` / `_oe`.
* **Requirement (Zynq adapter)**: no pio2 RTL. The PS GP port reaches the AXI4-Lite slave through the
  Xilinx AXI interconnect / protocol converter; the Xilinx AXI MCDMA (one DMA channel per pio2 channel,
  routed by `TDEST`) connects the two streams to DDR through an HP port; the interrupt goes to
  `IRQ_F2P`.
* **Requirement**: the host driver (`[SPEC-LINK-06]`) has an SPI back end and a Zynq back end (UIO mmap
  for the registers, a DMA buffer for the streams) with the same channel API.
* **Verified by**: `cocotb-regression` (`tests/cocotb/test_pio2_core_cocotb.py`: the core through its
  boundary, registers in every window, streams by `TDEST`, interrupt), `board-bringup`
  (Zynq: registers and one stream through the PS)
* **Implementation Target**: `src/rtl/pio2_core.sv`, `src/pio2/host/zynq.py`

### `[SPEC-ICN-12]` On the Tang Boards the PIO Replaces the Hardwired Cores
* **Goal (project owner, 2026-10-29)**: DShot, bidirectional DShot, ESC serial passthrough and UART are
  PIO programs on the Tang Nano 9K / 20K, not dedicated RTL. The AbstractX cores they replace
  (`asp_dshot_core`, `asp_uart_core`, the ESC passthrough path, `asp_pwm_decoder`) are not part of the
  pio2 Tang tops.
* **Requirement**: each replaced function has a PIO program in `src/programs/` with a cocotb timing test
  against the protocol (as `dshot_bidir` and `esc_serial` have today) and a board test, and the pins
  it uses are owned by the PIO through the pin mux (`[SPEC-PIO-09]`).
* **Verified by**: `cocotb-regression` (per program), `fpga-fit` (the Tang tops fit without the old
  cores), `board-bringup` (motor and ESC on a Tang)
* **Implementation Target**: `src/programs/`, `boards/tang_nano9k/`, `boards/tang_nano20k/`

### `[SPEC-VER-01]` Full Validation: Formal Properties, UVM-Style Testbenches, Coverage
* **Goal (project owner, 2026-10-29)**: full validation of every bus port and block, not only directed
  tests.
* **Requirement (formal)**: every AXI4-Lite and AXI-Stream port carries the ZipCPU formal property
  sets (Dan Gisselquist, `wb2axip`: `faxil_slave`, `faxil_master`, `faxis_master`, `faxis_slave`; from
  the `third_party/wb2axip` submodule at its pinned commit, Apache-2.0, `docs/third_party.md`) and is
  proved with SymbiYosys: bounded model check to a stated depth and k-induction where it closes; the
  async FIFO pointers and the CRC32 framer have their own properties. Every proof is shown to fail on
  a deliberately broken copy of the block.
* **Requirement (simulation)**: cocotb testbenches built UVM-style with **pyuvm** (sequences, drivers,
  monitors, scoreboard, environment per block), using **cocotbext-axi** for AXI4-Lite and AXI-Stream
  masters, slaves and monitors, constrained-random sequences, random back-pressure on every
  `READY`, and a reference model per block (scoreboard compares).
* **Requirement (coverage)**: functional coverage with **cocotb-coverage**: per AXI port the handshake
  orders (AW before W, W before AW, together; READY before VALID and after), back-pressure lengths,
  responses; per block its modes and error paths (CRC fail, overflow, underrun, drop). A block is
  done when every cover point is hit, recorded per run.
* **Requirement (steady state)**: 1000 transactions or blocks per test, with no growth in any queue or
  counter that should return to zero.
* **Requirement (system)**: an end-to-end test with the Python host driver speaking single and dual SPI
  to the whole core, running the three `[SPEC-DMA-02]` examples. Resource use (LUT / FF / BRAM) on Tang
  Nano 9K and 20K recorded per change.
* **Verified by**: `formal-check`, `cocotb-regression`, `fpga-fit`
* **Implementation Target**: `tests/formal/`, `tests/cocotb/`, `tests/uvm/`
