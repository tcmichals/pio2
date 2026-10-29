---
name: fpga-fit
runs_on: fpga-build
verifies: "The design fits the FPGA with headroom and meets timing on the example boards (Tang Nano 9K, Tang Nano 20K)."
description: "Check skill. The design fits the FPGA with headroom and meets timing on the example boards (Tang Nano 9K, Tang Nano 20K). Run when a requirement names this skill in its Verified by line or a change touches what it checks."
---

# Skill: fpga-fit

**What it verifies**: The design fits the FPGA with headroom and meets timing on the example boards (Tang Nano 9K, Tang Nano 20K).

## How to run

```bash
# Board tops and build flow: docs/boards.md (in AbstractX until the pio2 board tops exist)
```

## Pass criteria

LUT and FF use at most 80%; Fmax above the system clock for every clock domain.

## Evidence

Utilisation and Fmax lines from the nextpnr log, recorded per change.
