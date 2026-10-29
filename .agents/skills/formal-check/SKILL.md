---
name: formal-check
runs_on: rtl-sim
verifies: "Bus protocol compliance proved, not sampled: every AXI4-Lite and AXI-Stream port obeys the AMBA rules, checked with ZipCPU's formal property sets ([SPEC-AXI-01], [SPEC-AXI-02], [SPEC-VER-01])."
description: "Check skill. Bus protocol compliance proved, not sampled: every AXI4-Lite and AXI-Stream port obeys the AMBA rules, checked with ZipCPU's formal property sets ([SPEC-AXI-01], [SPEC-AXI-02], [SPEC-VER-01]). Run when a requirement names this skill in its Verified by line or a change touches what it checks."
---

# Skill: formal-check

**What it verifies**: Bus protocol compliance proved, not sampled: every AXI4-Lite and AXI-Stream port obeys the AMBA rules, checked with ZipCPU's formal property sets ([SPEC-AXI-01], [SPEC-AXI-02], [SPEC-VER-01]).

## How to run

```bash
export PATH="$HOME/.tools/oss-cad-suite/bin:$PATH"
uv run pio2-setup-tools --zipcpu-props   # ZipCPU wb2axip property files at a pinned commit
sby -f tests/formal/<block>.sby           # one .sby per block: faxil_slave / faxis_* bound to its ports
```

## Pass criteria

Every property proves (k-induction) or the bounded check passes at the depth stated in the .sby; the same proof fails on a deliberately broken copy of the block.

## Evidence

The sby summary per block in the commit message.
