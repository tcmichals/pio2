# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""pio2.asm against pioasm: every checked-in pioasm image re-assembles identically ([SPEC-DBG-16])."""
# @verifies [SPEC-DBG-16] docs/DEBUGGER_SPECIFICATION.md

import json
from pathlib import Path

import pytest

from pio2.asm import AsmError, assemble

ROOT = Path(__file__).resolve().parents[1]
IMAGES = sorted(p for p in (ROOT / "src" / "programs").rglob("*.json")
                if p.with_suffix(".pio").exists())


def test_there_are_images():
    assert len(IMAGES) >= 25


@pytest.mark.parametrize("image", IMAGES, ids=lambda p: str(p.relative_to(ROOT / "src" / "programs")))
def test_matches_pioasm(image):
    ref = json.loads(image.read_text())["programs"]
    got = assemble(image.with_suffix(".pio").read_text())["programs"]
    assert [p["name"] for p in got] == [p["name"] for p in ref]
    for g, r in zip(got, ref):
        for key in ("instructions", "wrap_target", "wrap", "origin", "sideset"):
            assert g[key] == r[key], f"{r['name']}.{key}: {g[key]} != pioasm {r[key]}"


@pytest.mark.parametrize("src,msg", [
    ("set pins, 1", "before .program"),
    (".program a\nfoo x", "unknown instruction"),
    (".program a\nset pins, 40", "out of 0..31"),
    (".program a\njmp nowhere", "unknown name"),
    (".program a\n.side_set 1\nset pins, 1", "every instruction needs `side`"),
    (".program a\nset pins, 1 [32]", "does not fit"),
    (".program a\n.side_set 2\nnop side 4", "does not fit 2 bits"),
    (".program a\nl:\nl:\nnop", "defined twice"),
    (".program a\n" + "nop\n" * 33, "32 max"),
])
def test_errors_name_the_line(src, msg):
    with pytest.raises(AsmError) as e:
        assemble(src)
    assert msg in str(e.value)
    assert e.value.line >= 1
