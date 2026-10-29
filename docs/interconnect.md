# pio2 Interconnect: Host Link (SPI / Dual-SPI), Channels, Wishbone, DMA, Trace

Status: specification, nothing implemented yet (2026-10-29). Requirements carry tags; code that
implements one carries `@impl [SPEC-xxx-nn] docs/interconnect.md`.

## 1. Idea

The PIO IP looks to the host like a small network interface: one SPI link (single or dual data
lines) carries **numbered channels**, like ports. Channel 0 is addressed register access (read and
write any Wishbone address); the other channels are **streams** (data to and from the PIO through
DMA rings, and the trace). An interrupt pin tells the host which channel needs service. Inside the
FPGA everything is Wishbone; there is no AXI and no TLP in pio2 (AbstractX adds its TLP adapters in
its own wrapper, `[SPEC-ICN-09]`).

```
 host SPI master                                FPGA
 ───────────────     ┌───────────────────────────────────────────────────────────────────┐
  SCK, CS_n ────────►│ SPI PHY            framer (≤128 B,     channel mux / arbiter       │
  IO0 (MOSI) ◄──────►│ single / dual  ──► CRC32), NIC regs──►  ch0  register master ──┐    │
  IO1 (MISO) ◄──────►│ (per frame)        status, credits      ch1  trace  (out) ◄─┐  │    │
  INT_n ◄────────────│                                         ch2+ streams in/out │  │    │
                     │                                              ▲   │          │  ▼    │
                     │                         DMA engine ◄── rings in BRAM        │ Wishbone│
                     │                          │   ▲  (DREQ from PIO FIFOs)       │ arbiter │
                     │                          ▼   │                              │  + decoder
                     │                 PIO tile (Wishbone slave) ── events ──► event FIFO   │
                     └───────────────────────────────────────────────────────────────────┘
```

## 2. Wishbone

### `[SPEC-WB-01]` One Bus Profile: Wishbone B4, Classic and Pipelined
* **Requirement**: every internal port is Wishbone B4, 32-bit data, byte address, `sel` per byte.
  Classic single accesses (`cyc stb we adr dat sel ack`) for registers. Pipelined mode adds
  `stall` (slave not ready; the master holds the request) and burst tags:
  * `cti = 3'b000` classic, `3'b001` **constant-address burst** (FIFO ports: one address, N words),
    `3'b010` **incrementing burst** (memory: program load, ring buffers), `3'b111` end of burst;
  * `bte = 2'b00` linear only.
* **Requirement**: a slave that does not implement bursts answers each beat as a classic cycle
  (bursts are an optimisation, never required for correctness). `err` ends a cycle on an unmapped
  address. No `rty`.
* **Verified by**: cocotb bus checker on every port (flags any B4 rule violation), randomized
  `stall` / `ack` timing; SymbiYosys properties for master and slave compliance.

### `[SPEC-WB-02]` Interconnect: Arbiter and Address Decoder
* **Requirement**: masters: the link's register master (channel 0) and the DMA engine. Slaves: PIO
  tile, DMA registers, ring-buffer RAM, event FIFO, link registers. Round-robin arbitration with a
  configurable DMA priority (the DMA engine may win every tie so a state machine never starves while
  the host reads registers); a master holds the bus for a whole burst; a burst is bounded
  (`MAX_BURST`, default 16 beats) so neither master can block the other.
* **Requirement (address map, base 0x0000_0000, 4 KB windows)**:

  | Window | Slave |
  | :--- | :--- |
  | `0x0000` | PIO tile registers (as today: CTRL, FSTAT, FDEBUG, IRQ, SMx_*, TXFx/RXFx, INSTR_MEM) |
  | `0x1000` | DMA engine registers (per channel) |
  | `0x2000` | event FIFO / trace registers |
  | `0x3000` | link registers (version, channel status, interrupt mask, error counters) |
  | `0x1_0000` | ring-buffer RAM (size per build, default 16 KB) |

## 3. Host link

### `[SPEC-LINK-01]` SPI PHY: Single and Dual, Chosen per Window
* **Requirement**: SPI mode 0. Each CS_n window starts with one command byte on IO0 (always single):
  `0x01 WRITE_FRAME` (host sends one frame, `[SPEC-LINK-02]`) or `0x02 READ_FRAME` (FPGA returns the
  next queued frame, or an empty frame of length 0 when none is queued), with bit 7 `D` selecting the
  rest of the window: single (MOSI = IO0, MISO = IO1) or dual (IO0 and IO1 both carry data, 2 bits
  per clock; one turnaround clock before the FPGA drives). The FPGA returns the low byte of `ISR`
  during the command byte, so every window also tells the host what is pending.
* **Requirement**: SCK is sampled in the system clock domain (2-flop synchroniser, edge detect);
  SCK must be at most f_sys / 4. The tested limit per board is recorded in `docs/boards.md`.
* **Requirement**: pins as separate `_i`, `_o`, `_oe` signals at the IP boundary; tristate only in
  the board top (the same rule the PIO pins follow).

### `[SPEC-LINK-02]` Frames: At Most 128 Bytes, CRC32, Like a NIC
* **Requirement**: the link carries **frames** of at most 128 bytes, framed by CS_n. A framer on each
  side checks and adds a CRC32 (CRC-32/ISO-HDLC, the Ethernet polynomial `0x04C11DB7`, reflected,
  init and final XOR `0xFFFFFFFF`) over header and payload. A frame whose CRC fails is dropped whole
  and counted; nothing of it reaches Wishbone or a ring.
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
* **Requirement (ops)**: `REG_READ`, `REG_WRITE` (channel 0, incrementing or constant address),
  `STREAM_DATA` (a stream channel's payload, either direction), `STATUS` (no payload; the reply
  carries ISR, queue levels and per-channel credits), `ACK` / `NAK` (reply to a write, with the
  `seq` and an error code).
* **Requirement (transactions)**: the SPI master always drives the clock, so a transaction is: host
  writes one frame (command), the FPGA queues the reply frame, the host reads it in a later CS_n
  window (signalled by `INT_n` or by polling `STATUS`). Host-to-FPGA and FPGA-to-host frames use
  separate queues, so the host can write the next command while a reply is waiting.

### `[SPEC-LINK-07]` NIC-Style Register Model
* **Requirement**: the link block is controlled like a small NIC, through registers in the link
  window (`0x3000`) that both the host (as `REG_*` frames) and the simulator can read:

  | Register | Meaning |
  | :--- | :--- |
  | `ID` / `VERSION` | magic and protocol version, so the host checks it talks to the right IP |
  | `CTRL` | enable, reset queues, SPI mode (dual allowed), `RD_WAIT` |
  | `ISR` | interrupt status, write 1 to clear: RX frame ready, TX queue space, stream threshold, DMA done, DMA error, CRC error, overflow, trace dropped |
  | `IMR` | interrupt mask; `INT_n` = any (`ISR` and `IMR`) |
  | `TXQ_LEVEL` / `RXQ_LEVEL` | frames waiting in each direction |
  | `RX_FRAME_LEN` / `RX_FRAME_CHAN` | header of the next reply frame, without consuming it |
  | `CRC_ERR` / `LEN_ERR` / `OVERFLOW` / `DROPPED` | error counters (saturating, clear on write) |
  | `STREAM_THR[n]` | per stream channel: interrupt threshold in words |

* **Requirement (frame buffers)**: at least two frame buffers per direction (128 bytes each) so the
  host can stream back to back; buffer count is a build parameter and is reported in `CTRL`.

### `[SPEC-LINK-03]` Channels Are Ports
* **Requirement**: channel numbers (frame `chan`): channel 0 = registers (addressed); channel 1 = trace stream (out, `[SPEC-TRC-*]`);
  channels 2..9 = the eight DMA stream rings (`[SPEC-DMA-*]`), direction from the DMA channel's
  configuration. Unused channels answer with an error status.
* **Requirement**: a stream channel never needs an address: like a socket, the host only says "give me
  up to N words" or "take these N words".

### `[SPEC-LINK-04]` Flow Control by Credits
* **Requirement**: the `STATUS` reply returns, per channel, words available (out) or words free (in). The host
  never sends more than the free count; the FPGA never returns more than it has. Neither side can
  overflow the other; an attempted overflow is a counted error, not data loss.

### `[SPEC-LINK-05]` Interrupt Pin and Status
* **Requirement**: `INT_n` (active low, level) = any bit of (`ISR` and `IMR`) (`[SPEC-LINK-07]`):
  a reply frame queued, an out stream at or above `STREAM_THR`, an in stream with at least
  `STREAM_THR` free, a DMA channel done or in error, a link error, trace dropped. The host reads `ISR`
  (in the first byte of any window, or with `STATUS`), services the causes, and writes 1 to clear
  them; a cause that is still true sets its bit again, so no interrupt is lost.

### `[SPEC-LINK-06]` The Same Link in Simulation
* **Requirement**: the simulator (`docs/debugger.md`) can drive the link at the SPI pins (bit for bit
  what a host does) or call the channels directly (fast). Both go through the same Python host
  driver, so a host program tested against the simulator runs unchanged on a board.

## 4. DMA

### `[SPEC-DMA-01]` Channels Paced by the PIO FIFOs
* **Requirement**: eight channels. Each moves 32-bit words between a ring buffer in RAM and one PIO
  FIFO (TX of SM n, or RX of SM n), paced by that FIFO's data request (TX not full / RX not empty),
  as RP2040 DMA is paced by DREQ. FIFO side: constant-address burst; ring side: incrementing burst.
* **Requirement (modes)**: `ONE_SHOT` (N words, then done), `RING` (continuous; ring wraps),
  `TRIGGERED` (each trigger moves one block of N words; trigger = a PIO IRQ flag or an input pin, e.g.
  IMU DRDY), `DIRECT` (no ring: the link stream feeds or drains the FIFO directly, back-pressured by
  Wishbone `stall`; low latency, low rate).
* **Requirement (counters)**: per channel: words moved, underruns (TX FIFO wanted data, ring empty),
  overruns (RX FIFO had data, ring full), each counted and each producing a trace event. A DMA channel
  never blocks the bus beyond `MAX_BURST` beats.

### `[SPEC-DMA-02]` Rings Connect DMA and Link Streams
* **Requirement**: each stream channel's ring is one producer / one consumer (link side and DMA side),
  with read/write indices in registers; its levels feed the link credits (`[SPEC-LINK-04]`) and the
  interrupt thresholds (`[SPEC-LINK-05]`).
* **Requirement (examples it must carry)**: DShot / bidirectional DShot frames out on a RING channel;
  ESC serial bytes in both directions; IMU auto-DMA: DRDY triggers a PIO SPI burst, a TRIGGERED RX
  channel moves each sample block into a ring, the host collects batches on interrupt.

## 5. Trace (barectf)

### `[SPEC-TRC-01]` Event FIFO With Fixed Records
* **Requirement**: producers (PIO tile, DMA engine, link) write fixed 64-bit records into one event
  FIFO through a small arbiter (fixed priority: errors > DMA > PIO > link; no producer ever waits:
  a record that does not fit is dropped and counted, and a `DROPPED n` record is written when space
  returns).
* **Requirement (record)**: `timestamp[31:0]` (system clocks, wraps; the host extends it to 64 bits),
  `type[7:0]`, `src[7:0]` (SM or DMA channel), `data[15:0]` (PC, word count, IRQ number, ...).
* **Requirement (types)**: SM stall begin / end (with reason), IRQ set / clear, FIFO full / empty edges,
  DMA burst begin / done / underrun / overrun, trigger, link frame error, dropped. Per-instruction
  events exist in simulation only (`[SPEC-DBG-13]`): at full speed they would fill the FIFO.
* **Requirement (enable)**: a type mask register; tracing off costs no bandwidth.

### `[SPEC-TRC-02]` Records Become barectf CTF on the Host
* **Requirement**: channel 1 streams the records; the host driver converts them into CTF 1.8 packets
  with a barectf schema (`src/pio2/trace/barectf_config.yaml`, aligned with AbstractX's
  `pio_event`), so simulation and board traces are the same and open in babeltrace2, Trace Compass
  and AbstractX Studio.

## 6. Integration and tests

### `[SPEC-ICN-09]` pio2 Has No AXI or TLP; AbstractX Wraps It
* **Requirement**: the pio2 tile exposes Wishbone (+ FIFO data requests and the event port) only. The
  TLP ingress/egress adapters move out of `asp_pio_tile` into an AbstractX-side wrapper, which keeps
  AbstractX's AXI-Stream / TLP router working unchanged; its TLP-path test moves with it.

### `[SPEC-ICN-10]` Verification
* **Requirement**: per block a cocotb test with the bus checker and randomized timing, and a
  1000-transaction steady-state case; formal (SymbiYosys) for Wishbone compliance of each port; an
  end-to-end test with the Python host driver speaking single and dual SPI to the whole IP, running
  the three `[SPEC-DMA-02]` examples; every test shown to fail on a deliberately broken block.
* **Requirement**: resource use (LUT / FF / BRAM) on Tang Nano 9K and 20K recorded per change.
