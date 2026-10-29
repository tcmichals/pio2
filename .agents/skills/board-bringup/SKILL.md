---
name: board-bringup
runs_on: board
verifies: "Behaviour on real hardware that no simulation can show (SPI link timing, interrupts, pin electrical behaviour)."
description: "Check skill. Behaviour on real hardware that no simulation can show (SPI link timing, interrupts, pin electrical behaviour). Run when a requirement names this skill in its Verified by line or a change touches what it checks."
---

# Skill: board-bringup

**What it verifies**: Behaviour on real hardware that no simulation can show (SPI link timing, interrupts, pin electrical behaviour).

## How to run

```bash
Follow the bring-up steps in the requirement and in docs/boards.md.
```

## Pass criteria

Every step in the requirement passes on the board.

## Evidence

Log with board, bitstream hash and date.
