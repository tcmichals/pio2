---
name: host-tests
runs_on: host
verifies: "Python package and debugger engine: unit tests and simulation-backed tests (pytest)."
description: "Check skill. Python package and debugger engine: unit tests and simulation-backed tests (pytest). Run when a requirement names this skill in its Verified by line or a change touches what it checks."
---

# Skill: host-tests

**What it verifies**: Python package and debugger engine: unit tests and simulation-backed tests (pytest).

## How to run

```bash
uv run pytest
```

Test quality rules (from AbstractX): a test that cannot fail is not evidence; steady state of at least
1000 cycles for queues, DMA and the link; every `@verifies` names code it really calls.

## Pass criteria

Every test passes.

## Evidence

pytest summary line in the commit message.
