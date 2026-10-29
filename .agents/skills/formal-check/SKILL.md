---
name: formal-check
runs_on: rtl-sim
verifies: "Bus protocol compliance proved, not sampled: every Wishbone master and slave port obeys Wishbone B4 ([SPEC-WB-01])."
description: "Check skill. Bus protocol compliance proved, not sampled: every Wishbone master and slave port obeys Wishbone B4 ([SPEC-WB-01]). Run when a requirement names this skill in its Verified by line or a change touches what it checks."
---

# Skill: formal-check

**What it verifies**: Bus protocol compliance proved, not sampled: every Wishbone master and slave port obeys Wishbone B4 ([SPEC-WB-01]).

## How to run

```bash
export PATH="$HOME/.tools/oss-cad-suite/bin:$PATH"
sby -f tests/formal/<block>.sby     # one .sby per block (added with the interconnect RTL)
```

## Pass criteria

Every property proves (or bounded check passes at the stated depth); a deliberately broken block fails.

## Evidence

The sby summary per block in the commit message.
