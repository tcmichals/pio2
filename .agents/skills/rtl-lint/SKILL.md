---
name: rtl-lint
runs_on: rtl-sim
verifies: "RTL is free of multi-driven signals, width errors and incomplete cases that simulation hides."
description: "Check skill. RTL is free of multi-driven signals, width errors and incomplete cases that simulation hides. Run when a requirement names this skill in its Verified by line or a change touches what it checks."
---

# Skill: rtl-lint

**What it verifies**: RTL is free of multi-driven signals, width errors and incomplete cases that simulation hides.

## How to run

```bash
export PATH="$HOME/.tools/oss-cad-suite/bin:$PATH"
uv run pio2-trac --no-cocotb --lint     # every src/rtl/**/*.sv as top, the rest found through -y (trac.lint_dirs)
# by hand, one module:
verilator --lint-only -Wall -Wno-DECLFILENAME -Wno-UNUSEDSIGNAL -Wno-UNUSEDPARAM -Wno-PINCONNECTEMPTY \
  -Wno-WIDTHTRUNC -Wno-WIDTHEXPAND -y src/rtl -y src/rtl/trace -y src/rtl/axi -y src/rtl/dma -y src/rtl/link \
  -y $PWD/third_party/verilog-axis/rtl -y $PWD/third_party/verilog-axi/rtl src/rtl/third_party.vlt \
  src/rtl/pio2_core.sv --top-module pio2_core
# $PWD: the third_party.vlt waiver pattern `*/third_party/*` does not match a relative third_party/ path
```

## Pass criteria

No `MULTIDRIVEN`, `SYNCASYNCNET` or `CASEINCOMPLETE`; width warnings reviewed.

## Evidence

Lint output (warning count per module) in the commit message.

Ledger: `uv run pio2-trac --record` (add `--formal` / `--lint` for those skills) writes one record per
requirement to `docs/verification/evidence/`, which the SpecTrace matrix turns into VERIFIED.
