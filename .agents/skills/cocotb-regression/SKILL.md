---
name: cocotb-regression
runs_on: rtl-sim
verifies: "RTL behaviour of the PIO tile and the pio2 interconnect blocks, per module and end to end."
description: "Check skill. RTL behaviour of the PIO tile and the pio2 interconnect blocks, per module and end to end. Run when a requirement names this skill in its Verified by line or a change touches what it checks."
---

# Skill: cocotb-regression

**What it verifies**: RTL behaviour of the PIO tile and the pio2 interconnect blocks, per module and end to end.

## How to run

```bash
uv run pio2-cocotb                 # every tests/cocotb/test_*_cocotb.py on Verilator
uv run pio2-cocotb --only dshot    # a subset
uv run pio2-cocotb --sim icarus    # the same suites on Icarus (second simulator)
```

Each suite runs in a temporary copy (no `sim_build/` in the repository). Simulators come from
`~/.tools/oss-cad-suite/bin` or `PATH`; cocotb comes from the uv environment.

## Pass criteria

Every suite prints `PASS`; the tool exits non-zero if a suite fails or prints no `TESTS=` line (it did not build or did not run). A test file is picked up by its name `test_*_cocotb.py`; nothing else registers it.

## Evidence

The summary lines, quoted in the commit message.

Ledger: `uv run pio2-trac --record` (add `--formal` / `--lint` for those skills) writes one record per
requirement to `docs/verification/evidence/`, which the SpecTrace matrix turns into VERIFIED.
