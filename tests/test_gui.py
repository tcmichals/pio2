# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""The debugger GUI driven like a user, through Dear ImGui Test Engine ([SPEC-DBG-10], [SPEC-DBG-15]):
the real window, the real widgets, clicked by label; after each action the app state is checked.
Needs a display (skipped without one) and the `gui` extra (`uv sync --extra gui`)."""
# @verifies [SPEC-DBG-10] [SPEC-DBG-15] docs/DEBUGGER_SPECIFICATION.md

import os
from pathlib import Path

import pytest

imgui_bundle = pytest.importorskip("imgui_bundle")
if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
    pytest.skip("no display", allow_module_level=True)

from imgui_bundle import imgui  # noqa: E402
from imgui_bundle.immapp import testing  # noqa: E402

from pio2.debug.examples import EXAMPLES  # noqa: E402
from pio2.gui.app import App, runner  # noqa: E402

SHOTS = Path(os.environ.get("PIO2_GUI_SHOTS", "")) if os.environ.get("PIO2_GUI_SHOTS") else None


def drive(app, steps):
    """Run the GUI with ImGui Test Engine; `steps(ctx, check)` acts and records failed checks."""
    failures = []

    def check(cond, what):
        if not cond:
            failures.append(what)

    def test_fn(ctx):
        try:
            steps(ctx, check)
        except Exception as e:                       # noqa: BLE001  (reported below, with the step)
            failures.append(f"exception: {e!r}")

    rp, addons = runner(app, {"n": 0})
    testing.run(test_function=test_fn, runner_params=rp, add_ons_params=addons,
                run_speed=testing.TestRunSpeed.fast)
    return failures


def frames(ctx, n):
    for _ in range(n):
        ctx.yield_()


def shot(ctx, name):
    if SHOTS:
        SHOTS.mkdir(parents=True, exist_ok=True)
        testing.capture(ctx, str(SHOTS / f"{name}.png"))


@pytest.fixture
def app():
    a = App("Blink")
    yield a
    a.s.close()


def test_every_control(app):
    def steps(ctx, check):
        frames(ctx, 5)
        shot(ctx, "01_start_blink")

        # Run / Pause ------------------------------------------------------------------------
        ctx.set_ref("Controls")
        check(app.running, "starts running")
        c0 = app.s.cycle
        frames(ctx, 5)
        check(app.s.cycle > c0, "running advances the clock")
        ctx.item_click("  Pause  ")
        check(not app.running, "Pause stops")
        c1 = app.s.cycle
        frames(ctx, 5)
        check(app.s.cycle == c1, f"paused: clock stays ({c1} -> {app.s.cycle})")

        # Step clock: exactly one clock --------------------------------------------------------
        ctx.item_click("Step clock")
        check(app.s.cycle == c1 + 1, f"Step clock: +1 ({c1} -> {app.s.cycle})")

        # Step instr SM0: one instruction retired (PC moves, or a delay / loop clock passes) --------
        before = (app.s.cycle, app.s.sm_state(0)["pc"])
        ctx.item_click("Step instr SM0")
        check(app.s.cycle > before[0], "Step instr runs at least one clock")
        check(app.step_note == "", f"Step instr SM0 note: {app.step_note!r}")

        # SM tabs select the SM; stepping a disabled SM says so ---------------------------------
        ctx.set_ref("State machines")
        ctx.item_click("sms/SM1")
        frames(ctx, 2)
        check(app.sel_sm == 1, f"SM1 tab selects SM1 (sel_sm={app.sel_sm})")
        ctx.set_ref("Controls")
        ctx.item_click("Step instr SM1")
        check(app.step_note == "SM1 is disabled", f"disabled SM note: {app.step_note!r}")
        ctx.set_ref("State machines")
        ctx.item_click("sms/SM0")
        frames(ctx, 2)
        check(app.sel_sm == 0, "SM0 tab back")
        shot(ctx, "02_paused_stepped")

        # clocks / frame slider ----------------------------------------------------------------
        ctx.set_ref("Controls")
        ctx.item_input_value(r"clocks \/ frame", 1234)
        check(app.speed == 1234, f"clocks / frame = 1234 (got {app.speed})")
        ctx.item_click("  Run  ")
        check(app.running, "Run resumes")
        c2 = app.s.cycle
        frames(ctx, 4)
        d = app.s.cycle - c2
        check(d >= 1234 * 3, f"running at 1234 clocks / frame: +{d} in 4 frames")

        # Reset ---------------------------------------------------------------------------------
        ctx.item_click("  Pause  ")
        ctx.item_click("Reset")
        frames(ctx, 1)
        check(app.b.cycle < 1000, f"Reset: shown cycle starts again (cycle {app.b.cycle})")
        ctx.item_click("  Run  ")

        # Logic analyzer window slider ------------------------------------------------------------
        ctx.set_ref("Logic analyzer")
        ctx.item_input_value("clocks shown", 777)
        check(app.window == 777, f"clocks shown = 777 (got {app.window})")

        # Example list: every example loads and runs -----------------------------------------------
        ctx.set_ref("Controls")
        ctx.item_input_value(r"clocks \/ frame", 500)
        for name in EXAMPLES:
            ctx.set_ref("Controls")
            ctx.combo_click(f"##example/{name}")
            frames(ctx, 3)
            check(app.example_name == name, f"example list selects {name!r} (got {app.example_name!r})")
            check(app.step_note == "", f"{name}: no stale step message ({app.step_note!r})")
            check(all(0 <= e.cycle - app.b.origin <= app.b.cycle for e in app.s.events),
                  f"{name}: trace cycles count from the example load, like the Controls bar")
            check(app.ex.title, f"{name}: title shown")
            c = app.s.cycle
            frames(ctx, 3)
            check(app.s.cycle > c, f"{name}: runs")
            shot(ctx, f"10_{name.replace(' ', '_')}")

        # Terminal shows UART TX text -------------------------------------------------------------
        ctx.set_ref("Controls")
        ctx.combo_click("##example/UART TX")
        ctx.item_input_value(r"clocks \/ frame", 20000)
        frames(ctx, 20)
        ctx.window_focus("//Terminal")
        frames(ctx, 2)
        term = next(p for p in app.ex.parts if p.kind == "terminal")
        check("Hello" in term.text, f"terminal received text: {term.text[:40]!r}")
        shot(ctx, "20_uart_terminal")

        # Trace window --------------------------------------------------------------------------
        ctx.window_focus("//Trace")
        frames(ctx, 2)
        shot(ctx, "21_trace")

        # Button part: hold it, the LED follows; release, it goes off -------------------------------
        ctx.set_ref("Controls")
        ctx.combo_click("##example/Button to LED")
        ctx.item_input_value(r"clocks \/ frame", 50)
        frames(ctx, 5)
        led = next(p for p in app.ex.parts if p.kind == "led")
        btn = next(p for p in app.ex.parts if p.kind == "button")
        check(not btn.pressed and not led.on, "button up, LED off")
        ctx.set_ref("Parts")
        ctx.mouse_move(f"##{btn.name}")
        ctx.mouse_down(0)
        frames(ctx, 6)
        check(btn.pressed, "holding the button presses it")
        check(led.on, "LED on while the button is held")
        shot(ctx, "30_button_held")
        ctx.mouse_up(0)
        frames(ctx, 6)
        check(not btn.pressed, "releasing the button")
        check(not led.on, "LED off after release")

    failures = drive(app, steps)
    assert not failures, "\n".join(failures)
