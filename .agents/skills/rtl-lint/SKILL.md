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
for f in src/rtl/*.sv; do verilator --lint-only -Wall -Wno-DECLFILENAME -Wno-UNUSEDSIGNAL \
  -Wno-UNUSEDPARAM -Wno-PINCONNECTEMPTY -Wno-WIDTHTRUNC -Wno-WIDTHEXPAND -Isrc/rtl src/rtl/*.sv \
  --top-module $(basename $f .sv); done
```

## Pass criteria

No `MULTIDRIVEN`, `SYNCASYNCNET` or `CASEINCOMPLETE`; width warnings reviewed.

## Evidence

Lint output (warning count per module) in the commit message.
