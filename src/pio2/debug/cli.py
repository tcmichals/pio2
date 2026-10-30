# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""pio2-debug: run an example session headless and report or save its trace ([SPEC-DBG-09]).

  uv run pio2-debug --list
  uv run pio2-debug --example "UART TX" --clocks 20000
  uv run pio2-debug --example Blink --clocks 50000 --json blink.json
"""
# @impl [SPEC-DBG-09] docs/DEBUGGER_SPECIFICATION.md

import argparse
import json
import sys

from pio2.debug.examples import EXAMPLES
from pio2.debug.session import Session


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--list", action="store_true", help="list the example sessions")
    ap.add_argument("--example", default="Blink")
    ap.add_argument("--clocks", type=int, default=10_000)
    ap.add_argument("--json", help="write the per-clock trace (pins, PCs) as JSON")
    a = ap.parse_args()
    if a.list:
        for name in EXAMPLES:
            print(name)
        return 0
    if a.example not in EXAMPLES:
        print(f"unknown example {a.example!r}; --list shows them", file=sys.stderr)
        return 2
    with Session() as s:
        ex = EXAMPLES[a.example](s)
        for p in ex.parts:
            p.attach(s)
        left = a.clocks
        while left > 0:
            k = min(left, ex.speed)
            if ex.feed:
                ex.feed(s)
            s.step(k)
            for p in ex.parts:
                p.update(s)
            left -= k
        print(f"{ex.title}: {s.cycle} clocks")
        for sm, (p, _) in s.programs.items():
            st = s.sm_state(sm)
            print(f"  SM{sm} {p['name']}: pc {st['pc']} x {st['reg_x']:#x} y {st['reg_y']:#x} "
                  f"stalled {st['stalled']} tx {st['tx_level']} rx {st['rx_level']}")
        for part in ex.parts:
            if part.kind == "led":
                print(f"  {part.name}: {'on' if part.on else 'off'}, {part.toggles} toggles")
            elif part.kind == "terminal":
                print(f"  {part.name}: {part.text!r}")
        if a.json:
            tr = s.trace
            with open(a.json, "w") as f:
                json.dump({"example": a.example, "start": tr.start, "out": list(tr.out),
                           "dir": list(tr.dir), "in": list(tr.inp),
                           "pc": [list(tr.pcs[i:i + 4]) for i in range(0, len(tr.pcs), 4)]}, f)
            print(f"  trace: {len(tr)} clocks -> {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
