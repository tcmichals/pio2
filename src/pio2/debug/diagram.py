# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""Diagram files (`*.diagram.json`, [SPEC-DBG-15]): the parts of a session, how they are wired to the
tile's GPIO lines, and their settings. The same file drives the GUI canvas and the headless run.

    {
      "pio2_diagram": 1,
      "title": "Blink: LED on GPIO 25",
      "about": "...",
      "reference": "how to compare with the Wokwi Pi Pico version (optional)",
      "parts": [{"type": "led", "name": "LED", "pins": {"pin": 25}, "attrs": {"color": [1, 0.25, 0.2]}}],
      "probes": [25],
      "labels": {"25": "LED"},
      "view": {"speed": 100, "window": 15000, "f_sys": 125000000}
    }

`type` is a key of `pio2.parts.part_types()`; `pins` maps the part's pin roles to GPIO lines;
`attrs` are its constructor settings (`Part.params`).
"""
# @impl [SPEC-DBG-15] docs/DEBUGGER_SPECIFICATION.md

import json
from pathlib import Path

SESSIONS = Path(__file__).resolve().parent / "sessions"
VERSION = 1


def path_of(name):
    p = Path(name)
    if p.suffix == ".json" or p.exists():
        return p
    return SESSIONS / f"{name}.diagram.json"


def load(name):
    """The diagram as a dict with `parts` built (Part objects)."""
    from pio2.parts import make_part
    d = json.loads(path_of(name).read_text())
    if d.get("pio2_diagram") != VERSION:
        raise ValueError(f"{name}: not a pio2 diagram (version {d.get('pio2_diagram')!r})")
    d = dict(d)
    d["parts"] = [make_part(p) for p in d.get("parts", [])]
    d["labels"] = {int(k): v for k, v in d.get("labels", {}).items()}
    d.setdefault("probes", [])
    d.setdefault("view", {})
    return d


def to_dict(example):
    """A diagram dict from a loaded example (parts as they are configured now)."""
    return {"pio2_diagram": VERSION, "title": example.title, "about": example.about,
            "reference": example.reference, "parts": [p.to_dict() for p in example.parts if p.wired],
            "probes": list(example.probes), "labels": {str(k): v for k, v in example.labels.items()},
            "view": {"speed": example.speed, "window": example.window, "f_sys": example.f_sys}}


def save(example, path):
    Path(path).write_text(json.dumps(to_dict(example), indent=2) + "\n")
