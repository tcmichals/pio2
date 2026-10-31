# What pio2 Offers

pio2 is one **core** (PIO tile, DMA engine with rings, event FIFO, AXI4-Lite crossbar) with a fixed
boundary, plus a choice of **host adapter**: an SPI link for an FPGA with an external host (Tang Nano
9K / 20K), or the Zynq's own ARM cores on AXI. The pins are not tied to any protocol: every state
machine maps GPIO windows the way the RP2040 / RP2350 PIO does, and the program decides what the pins
mean (DShot, UART, SPI, WS2812, ...).

Requirements: [`INTERCONNECT_SPECIFICATION.md`](INTERCONNECT_SPECIFICATION.md) (core boundary,
adapters, DMA, trace) and [`SPECIFICATION.md`](SPECIFICATION.md) (the PIO tile). Build state of each
block: [Status](#status) at the end.

**How to read the diagrams**

| Drawn as | Means |
| :--- | :--- |
| dashed box, dotted arrow | optional: a host adapter; a build has one of them |
| thin arrow | AXI4-Lite (registers) |
| thick arrow | AXI-Stream (data, trace) |
| blue box | the pio2 core, the same RTL on every board |
| orange box | pins |

## 1. The system: one core, two optional host adapters

```mermaid
flowchart TB
    classDef host fill:#f2f2f2,stroke:#555,color:#111
    classDef opt fill:#ffffff,stroke:#777,color:#111
    classDef core fill:#e6efff,stroke:#2f5fb3,color:#0b1f44
    classDef pins fill:#fff1db,stroke:#c07a00,color:#3a2500

    EXT["external host<br/>Pico, SBC, Cubie, PC"]:::host
    PS["Zynq PS<br/>ARM Cortex-A9 + Linux"]:::host

    subgraph OPTA["option A: SPI host link (Tang Nano 9K / 20K, any FPGA)"]
        SPI["SPI / Dual-SPI link<br/>128-byte frames, CRC32<br/>channels, INT_n pin"]:::opt
    end
    subgraph OPTB["option B: Zynq-7000, no SPI"]
        ZAD["PS GP port + pio2 ring DMA<br/>pio2 RTL, no vendor IP"]:::opt
    end

    subgraph CORE["pio2 core: same RTL on every board"]
        BND["core boundary<br/>AXI4-Lite slave · AXI-Stream in · AXI-Stream out · IRQ"]:::core
        XBAR["AXI4-Lite crossbar<br/>registers"]:::core
        DMA["DMA engine + ring RAM<br/>8 PIO channels + 1 trace channel"]:::core
        TILE["PIO tile<br/>4 state machines, RP2040 / RP2350 ISA"]:::core
        EVF["event FIFO<br/>trace records"]:::core
    end

    MAP["GPIO mapping, per state machine<br/>OUT · SET · side-set · IN · JMP PIN windows"]:::pins
    PADS(["FPGA pads<br/>assigned in pins.toml"]):::pins

    EXT -.->|"SCK CS_n IO0 IO1, INT_n"| SPI
    PS -.->|"AXI GP + HP ports"| ZAD
    SPI -.->|"AXI4-Lite, AXI-Stream, IRQ"| BND
    ZAD -.->|"AXI4-Lite, AXI-Stream, IRQ"| BND
    BND --> XBAR
    BND <==> DMA
    XBAR --> TILE
    XBAR --> DMA
    XBAR --> EVF
    DMA <==>|"TX / RX FIFO streams"| TILE
    TILE -->|"events"| EVF
    EVF ==>|"trace channel"| DMA
    TILE <--> MAP <--> PADS

    style OPTA fill:none,stroke:#888,stroke-width:2px,stroke-dasharray:8 5
    style OPTB fill:none,stroke:#888,stroke-width:2px,stroke-dasharray:8 5
    style CORE fill:#f5f8ff,stroke:#2f5fb3,stroke-width:2px
```

**Which adapter a board builds** (`[SPEC-ICN-11]`): the SPI link is optional.

| Board | SPI link | Zynq adapter | Why |
| :--- | :--- | :--- | :--- |
| Tang Nano 9K / 20K (Gowin, no CPU on chip) | **required** | none | the host is outside the FPGA (Pico, SBC, Cubie) |
| Zynq-7000 | **not built** | **required** | the ARM cores are on the chip, on AXI |
| any other FPGA with an external host | required | none | as the Tang boards |
| simulation (Verilator) | at the pins, or bypassed (`[SPEC-LINK-06]`) | none | same driver either way |

Whatever the host, it sees the same thing: registers on channel 0, data streams on channels 2..9,
the trace on channel 1, one interrupt. The pio2 driver has an SPI back end and a Zynq back end with
the same channel API, and the simulator speaks to the same boundary.

## 2. Option A: the SPI host link

```mermaid
flowchart LR
    classDef host fill:#f2f2f2,stroke:#555,color:#111
    classDef opt fill:#ffffff,stroke:#777,color:#111
    classDef core fill:#e6efff,stroke:#2f5fb3,color:#0b1f44

    HOST["external host<br/>pio2 driver, SPI back end"]:::host

    subgraph LINK["SPI host link adapter"]
        direction LR
        subgraph SCKD["SCK clock domain"]
            PHY["SPI PHY<br/>command byte single,<br/>data single or dual"]:::opt
        end
        AF["async FIFOs<br/>Gray pointers"]:::opt
        RX["framer in<br/>CRC32 check, 128 B max<br/>bad frame dropped, counted"]:::opt
        SPLIT{"frame chan"}
        AXM["AXI4-Lite master<br/>REG_READ / REG_WRITE"]:::opt
        TOS["frame to block<br/>TDEST = chan"]:::opt
        FRB["block to frame<br/>chan = TDEST"]:::opt
        OUTQ["outbound buffers<br/>one reserved for ch 0 replies"]:::opt
        TX["framer out<br/>header + CRC32"]:::opt
        NIC["link registers<br/>CRC / length / overflow counters"]:::opt
    end

    CORE["pio2 core boundary"]:::core

    HOST <-->|"SCK CS_n IO0 IO1"| PHY
    PHY ==> AF ==> RX ==> SPLIT
    SPLIT -->|"ch 0"| AXM
    SPLIT ==>|"ch 2..9"| TOS
    AXM -->|"AXI4-Lite"| CORE
    TOS ==>|"AXI-Stream in"| CORE
    CORE ==>|"AXI-Stream out, ch 1..9"| FRB
    AXM ==>|"replies"| OUTQ
    FRB ==> OUTQ ==> TX ==> AF
    AF ==> PHY
    CORE -.->|"IRQ as INT_n"| HOST
    NIC --- RX

    style LINK fill:none,stroke:#888,stroke-width:2px,stroke-dasharray:8 5
```

* Each CS_n window starts with a command byte (`WRITE_FRAME` / `READ_FRAME`, bit 7 = dual); the FPGA
  returns the low byte of `ISR` during it, so every window also tells the host what is pending.
* A frame is at most 128 bytes: 12-byte header, up to 116 bytes payload, CRC-32 (the Ethernet CRC).
* The PHY is clocked by SCK itself and crosses to the system clock through async FIFOs, so the FPGA
  clock does not limit the link; goal: at least 10 MB/s payload on Dual SPI (`[SPEC-LINK-01]`).
* Register replies never wait behind stream data: one outbound buffer is theirs (`[SPEC-LINK-08]`).

## 3. Option B: Zynq-7000, straight AXI

```mermaid
flowchart LR
    classDef host fill:#f2f2f2,stroke:#555,color:#111
    classDef xil fill:#ffffff,stroke:#3b5fae,color:#111
    classDef core fill:#e6efff,stroke:#2f5fb3,color:#0b1f44

    subgraph PS["Zynq PS"]
        APP["pio2 driver, Zynq back end<br/>UIO mmap + DMA buffers"]:::host
        DDR[("DDR")]:::host
        GP["M_AXI_GP0"]:::host
        HP["S_AXI_HP0"]:::host
        IRQ["IRQ_F2P"]:::host
    end

    subgraph ZAD["Zynq adapter: Xilinx IP only"]
        CONV["axi_axil_adapter (Forencich)<br/>AXI3 to AXI4-Lite"]:::xil
        MC["pio2_ring_dma on HP0<br/>one DDR ring per channel<br/>chosen by TDEST"]:::xil
    end

    CORE["pio2 core boundary"]:::core

    APP --- GP
    APP --- DDR
    GP --> CONV -->|"AXI4-Lite"| CORE
    GP -->|"DMA control"| MC
    HP <==> DDR
    MC <==> HP
    MC ==>|"AXI-Stream in, TDEST 2..9"| CORE
    CORE ==>|"AXI-Stream out, TDEST 1..9"| MC
    CORE -.->|"IRQ"| IRQ

    style ZAD fill:none,stroke:#888,stroke-width:2px,stroke-dasharray:8 5
```

| | Option A: SPI link | Option B: Zynq |
| :--- | :--- | :--- |
| Registers | channel 0 frames, the link's AXI4-Lite master | PS GP port, memory mapped (UIO) |
| Streams in / out | `STREAM_DATA` frames, channel in the header | pio2 ring DMA to DDR rings, channel in `TDEST` |
| Integrity | CRC32 per frame | on-chip bus |
| Interrupt | `INT_n` pin | `IRQ_F2P` |
| Throughput | about 10 MB/s goal (Dual SPI) | hundreds of MB/s; the PIO is the limit |
| Host code | same channel API | same channel API |

## 4. Inside the core

```mermaid
flowchart LR
    classDef core fill:#e6efff,stroke:#2f5fb3,color:#0b1f44
    classDef bnd fill:#ffffff,stroke:#2f5fb3,color:#0b1f44

    AXIL["AXI4-Lite slave"]:::bnd
    SIN["AXI-Stream in<br/>TDEST = channel"]:::bnd
    SOUT["AXI-Stream out<br/>TDEST = channel"]:::bnd
    IRQ["IRQ"]:::bnd

    XBAR["AXI4-Lite crossbar<br/>0x0000 tile · 0x1000 DMA<br/>0x2000 trace · 0x3000 link / ISR"]:::core
    DEMUX["axis_demux<br/>by TDEST"]:::core
    AMUX["axis_arb_mux<br/>whole TLAST blocks<br/>round robin"]:::core
    RINGS["ring RAM<br/>ring 1 trace, rings 2..9 streams"]:::core
    DMA["DMA engine<br/>ONE_SHOT · RING · TRIGGERED · DIRECT<br/>8 PIO channels + trace channel"]:::core
    TILE["PIO tile<br/>4 SMs, per-SM TX / RX FIFO"]:::core
    EVF["event FIFO<br/>64-bit records"]:::core
    ISR["ISR / IMR"]:::core
    TRIG["triggers<br/>PIO IRQ flag or pin, e.g. IMU DRDY"]:::core

    AXIL --> XBAR
    XBAR --> TILE
    XBAR --> DMA
    XBAR --> EVF
    XBAR --> ISR
    SIN ==> DEMUX ==> RINGS
    RINGS ==> AMUX ==> SOUT
    RINGS <==> DMA
    DMA ==>|"TREADY = TX not full"| TILE
    TILE ==>|"TVALID = RX not empty"| DMA
    EVF ==>|"trace channel, 64 to 32 bit"| DMA
    TILE -->|"events"| EVF
    DMA -->|"events"| EVF
    TRIG --> DMA
    ISR --> IRQ
```

* DMA pacing is the AXI-Stream handshake: the same signal as the RP2040's DREQ (`[SPEC-DMA-01]`).
* The trace goes through the DMA like any other stream, into ring 1 (`[SPEC-TRC-02]`), so it reaches
  an SPI host and a Zynq the same way.
* Examples it must carry (`[SPEC-DMA-02]`): DShot frames out on a RING channel; ESC serial both
  ways; IMU auto-DMA, where DRDY triggers a PIO SPI burst and a TRIGGERED channel collects each
  sample block.

## 5. Pin mapping: windows per state machine, not fixed functions

The tile has 32 GPIO lines. No line has a fixed job; each state machine sees the lines through
**windows** set in its `PINCTRL` and `EXECCTRL` registers, as on the RP2040 / RP2350 (datasheet
section "GPIO Mapping": [RP2040 datasheet](https://datasheets.raspberrypi.com/rp2040/rp2040-datasheet.pdf),
[RP2350 datasheet](https://datasheets.raspberrypi.com/rp2350/rp2350-datasheet.pdf)).

```mermaid
flowchart LR
    classDef sm fill:#e6efff,stroke:#2f5fb3,color:#0b1f44
    classDef win fill:#ffffff,stroke:#2f5fb3,color:#0b1f44
    classDef pins fill:#fff1db,stroke:#c07a00,color:#3a2500

    subgraph SMN["state machine n, each of the 4"]
        I_OUT["OUT pins, MOV pins<br/>OUT pindirs"]:::sm
        I_SET["SET pins<br/>SET pindirs"]:::sm
        I_SIDE["side-set<br/>on any instruction"]:::sm
        I_IN["IN pins, MOV from pins<br/>WAIT pin"]:::sm
        I_JMP["JMP PIN<br/>WAIT jmppin"]:::sm
    end

    W_OUT["OUT window<br/>OUT_BASE, OUT_COUNT 0..32"]:::win
    W_SET["SET window<br/>SET_BASE, SET_COUNT 0..5"]:::win
    W_SIDE["side-set window<br/>SIDESET_BASE, SIDESET_COUNT 0..5<br/>values or pindirs"]:::win
    W_IN["IN window<br/>IN_BASE = bit 0, 32 lines"]:::win
    W_JMP["one line<br/>EXECCTRL.JMP_PIN"]:::win

    PRI["per line, every clock<br/>side-set beats OUT / SET in one SM<br/>highest-numbered SM wins<br/>a line nobody writes keeps its value"]:::win
    LAT["32 output latches + 32 direction latches<br/>every line an input after reset"]:::pins
    PADS["board top: tristate per pad<br/>pad to 2-flop sync to i_gpio<br/>which pad = pins.toml"]:::pins

    I_OUT --> W_OUT --> PRI
    I_SET --> W_SET --> PRI
    I_SIDE --> W_SIDE --> PRI
    PRI --> LAT --> PADS
    PADS --> W_IN --> I_IN
    PADS --> W_JMP --> I_JMP
```

Windows wrap modulo 32 (base 30, count 4 covers lines 30, 31, 0, 1). Windows of different state
machines may overlap; that is how one SM can drive a line another reads.

**Examples: the same pins, different programs.** The host sets the bases from the board's pin map
(`[SPEC-PIO-09]`: software asks for `motor3`, never a number), so one program runs on any line.

| Program | OUT | SET | side-set | IN | JMP PIN | Notes |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Bidirectional DShot (`dshot_bidir.pio`) | motor line | motor line | none | motor line | motor line | drives, then releases the line with `set pindirs, 0` and samples the ESC's reply on the same wire |
| UART TX (pico-examples `uart_tx`) | TX line | none | TX line (opt) | none | none | |
| SPI (pico-examples `spi`) | MOSI | none | SCK | MISO | none | three windows, three different lines |
| WS2812 (pico-examples `ws2812`) | none | none | data line | none | none | everything in side-set |

So "DShot on motor 3" is not a pin function: it is the `dshot_bidir` program on one state machine
with all its windows pointed at the line that the pin map calls `motor3`.

## 6. The trace: from a state machine event to a CTF file

```mermaid
flowchart LR
    classDef core fill:#e6efff,stroke:#2f5fb3,color:#0b1f44
    classDef opt fill:#ffffff,stroke:#777,color:#111
    classDef host fill:#f2f2f2,stroke:#555,color:#111

    subgraph PROD["producers: offer, never wait"]
        P0["errors"]:::core
        P1["DMA"]:::core
        P2["PIO tile"]:::core
        P3["link"]:::core
    end
    EVF["event FIFO<br/>slot per producer, fixed priority<br/>slot full: drop, count, DROPPED record<br/>timestamp at the event's clock"]:::core
    TDMA["DMA trace channel<br/>RING mode"]:::core
    R1["ring 1 = channel 1<br/>block at 14 records or flush time"]:::core
    AMUX["core stream output<br/>whole blocks, round robin"]:::core

    subgraph A["option A"]
        SPI["SPI link<br/>chan 1 frames, CRC32"]:::opt
    end
    subgraph B["option B"]
        MC["pio2 ring DMA<br/>to DDR"]:::opt
    end

    subgraph HOST["host: pio2 driver"]
        EXT["extend 32-bit timestamps<br/>sort by time"]:::host
        BCTF["barectf CTF 1.8 packets"]:::host
    end
    VIEW(["babeltrace2, Trace Compass,<br/>AbstractX Studio, pio2 debugger"]):::host

    P0 & P1 & P2 & P3 --> EVF
    EVF ==> TDMA ==> R1 ==> AMUX
    AMUX -.-> SPI -.-> EXT
    AMUX -.-> MC -.-> EXT
    EXT --> BCTF --> VIEW

    style A fill:none,stroke:#888,stroke-width:2px,stroke-dasharray:8 5
    style B fill:none,stroke:#888,stroke-width:2px,stroke-dasharray:8 5
```

* **barectf is not in the FPGA.** The FPGA makes 8-byte records (`data[15:0]`, `src[23:16]`,
  `type[31:24]`, `timestamp[63:32]`); the host writes the CTF, so simulation and board traces are
  the same files.
* **Losses are visible, never silent**: a producer never waits; if its slot is still full the event is
  dropped and counted, and a `DROPPED n` record (written when the slots are idle, so it never causes
  more losses) marks the gap by its timestamp.

## Status

State on 2026-10-29 (details: [`../TODO.md`](../TODO.md)).

| Block | State |
| :--- | :--- |
| PIO tile, AXI4-Lite register port, pin mapping | built; 43 cocotb tests, formal BMC on the AXI4-Lite port |
| Event FIFO | built; 5 cocotb tests, two mutants caught |
| Crossbar, DMA engine and rings, stream mux / demux, `ISR` / `IMR` | specified |
| Tile FIFO stream ports | specified (`[SPEC-ICN-09]`); today the FIFOs are reached through TXFx / RXFx |
| SPI host link | specified |
| Zynq adapter | specified |
| Host driver, CTF conversion | specified |
