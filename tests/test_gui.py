# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""The debugger GUI driven like a user, through Dear ImGui Test Engine ([SPEC-DBG-10], [SPEC-DBG-15]):
the real widgets, clicked by label; after each action the app state is checked. Needs the `gui`
extra (`uv sync --extra gui`). By default it runs on hello_imgui's null backends (the whole UI, no
window, full speed, no display needed: a hidden window is throttled to about 1 frame per second or
less by some compositors). With PIO2_GUI_SHOTS=<dir> it opens the real window and saves screenshots."""
# @verifies [SPEC-DBG-10] [SPEC-DBG-15] docs/DEBUGGER_SPECIFICATION.md

import os
from pathlib import Path

import pytest

imgui_bundle = pytest.importorskip("imgui_bundle")
SHOTS_DIR = os.environ.get("PIO2_GUI_SHOTS")
if SHOTS_DIR and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
    pytest.skip("PIO2_GUI_SHOTS needs a display", allow_module_level=True)

from imgui_bundle import hello_imgui, imgui  # noqa: E402,F401
from imgui_bundle.immapp import testing  # noqa: E402

from pio2.debug.examples import EXAMPLES  # noqa: E402
from pio2.gui.app import App, runner  # noqa: E402

SHOTS = Path(os.environ.get("PIO2_GUI_SHOTS", "")) if os.environ.get("PIO2_GUI_SHOTS") else None


def drive(app, steps):
    """Run the GUI with ImGui Test Engine; `steps(ctx, check)` acts and records failed checks."""
    failures = []
    finished = []

    def check(cond, what):
        if not cond:
            failures.append(what)

    def test_fn(ctx):
        # A window that is not on screen is throttled to ~1 frame per second by some compositors;
        # the whole drive is then ~100 s, past the engine's default 60 s kill.
        ctx.engine_io.config_watchdog_warning = 300
        ctx.engine_io.config_watchdog_kill_test = 600
        try:
            steps(ctx, check)
            finished.append(not ctx.is_error())
        except Exception as e:                       # noqa: BLE001  (reported below, with the step)
            failures.append(f"exception: {e!r}")

    rp, addons = runner(app, {"n": 0})
    if not SHOTS:
        rp.platform_backend_type = hello_imgui.PlatformBackendType.null
        rp.renderer_backend_type = hello_imgui.RendererBackendType.null
        prev = rp.callbacks.post_init

        def null_textures():             # the null renderer ignores texture uploads; ImGui builds the atlas
            io = imgui.get_io()
            io.backend_flags |= imgui.BackendFlags_.renderer_has_textures
            if prev:
                prev()
        rp.callbacks.post_init = null_textures
    try:
        testing.run(test_function=test_fn, runner_params=rp, add_ons_params=addons,
                    run_speed=testing.TestRunSpeed.fast)
    except RuntimeError as e:
        # hello_imgui's null backend tears the test engine down before the ImGui context (a shutdown
        # order assert, after the test has finished); anything else is a real failure.
        if not (finished == [True] and "BEFORE ImGuiTestEngine_DestroyContext" in str(e)):
            raise
    if finished != [True] and not failures:
        failures.append("the drive did not reach its end")
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

        # A step button works while running: it pauses, then steps ([SPEC-DBG-03]) ------------
        ctx.item_click("Step clock")
        check(not app.running, "Step clock while running pauses")
        check(app.step_info == "1 clock", f"Step clock says what it did: {app.step_info!r}")
        c2 = app.s.cycle
        frames(ctx, 5)
        check(app.s.cycle == c2, f"stays paused after the step ({c2} -> {app.s.cycle})")
        ctx.item_click("  Run  ")
        check(app.running and app.step_info == "", "Run resumes and clears the step message")
        frames(ctx, 3)
        ctx.item_click("Step instr SM0")
        check(not app.running, "Step instr while running pauses")
        ctx.item_click("  Run  ")
        frames(ctx, 3)
        ctx.item_click("  Pause  ")
        check(not app.running, "Pause stops")
        c1 = app.s.cycle
        frames(ctx, 5)
        check(app.s.cycle == c1, f"paused: clock stays ({c1} -> {app.s.cycle})")

        # Step clock: exactly one clock --------------------------------------------------------
        ctx.item_click("Step clock")
        check(app.s.cycle == c1 + 1, f"Step clock: +1 ({c1} -> {app.s.cycle})")

        # Step instr SM0: one instruction retired (PC moves, or a delay / loop clock passes) --------
        # Blink's delay loop: the PC stays on 4, so X is what proves exactly one instruction ran.
        while app.s.sm_state(0)["pc"] != 4 or app.s.sm_state(0)["reg_x"] < 10:
            app.b.step_clock()
        before = (app.s.cycle, app.s.sm_state(0)["reg_x"])
        ctx.item_click("Step instr SM0")
        check(app.s.cycle == before[0] + 1, f"Step instr at divider 1: +1 ({before[0]} -> {app.s.cycle})")
        check(app.s.sm_state(0)["reg_x"] == before[1] - 1, "Step instr ran exactly one jmp x--")
        check(app.step_note == "", f"Step instr SM0 note: {app.step_note!r}")
        check(app.step_info == "SM0 ran 4: jmp x--, 4 in 1 clock", f"Step instr says what ran: {app.step_info!r}")

        # The logic analyzer shows the new cycle after a step, and the slider applies while paused
        app.window = 100
        app.follow = True
        frames(ctx, 2)
        for _ in range(150):                      # further than the view is wide
            app.b.step_clock()
        ctx.item_click("Step clock")
        frames(ctx, 2)
        end = app.s.trace.start + len(app.s.trace) - 1
        check(app.view_x[1] == end, f"analyzer follows the step (view ends {app.view_x[1]}, trace {end})")
        check(app.view_x[1] - app.view_x[0] == 99, f"analyzer view width {app.view_x}")

        # A host script's register access inside a step is reported, not hidden ---------------
        app.load_example("UART TX")
        frames(ctx, 1)
        c3 = app.s.cycle
        ctx.item_click("Step clock")
        check(app.s.cycle > c3 + 1, "UART TX: first step includes the feeder's accesses")
        check(f"{app.s.cycle - c3:,} clocks" in app.step_note and "host script" in app.step_note,
              f"overshoot reported: {app.step_note!r}")
        app.load_example("Blink")
        app.running = False
        frames(ctx, 1)
        c1 = app.s.cycle

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

        # A step message does not outlive an example change -------------------------------------------
        ctx.set_ref("Controls")
        ctx.item_click("  Pause  ")
        ctx.set_ref("State machines")
        ctx.item_click("sms/SM1")
        frames(ctx, 2)
        ctx.set_ref("Controls")
        ctx.item_click("Step instr SM1")
        check(app.step_note == "SM1 is disabled", "message shown before the example change")
        ctx.combo_click("##example/Square wave")
        frames(ctx, 2)
        check(app.step_note == "", f"step message cleared by the example change ({app.step_note!r})")
        ctx.set_ref("State machines")
        ctx.item_click("sms/SM0")
        ctx.set_ref("Controls")
        ctx.item_click("  Run  ")

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

        # FC board: DMA trace records are labelled by channel, not as an SM -------------------------
        ctx.set_ref("Controls")
        ctx.combo_click("##example/FC board (IMU auto-DMA)")
        ctx.item_input_value(r"clocks \/ frame", 20000)
        frames(ctx, 3)
        dma = [e for e in app.s.events if e.name.startswith("DMA")]
        check(dma, "FC board: DMA records in the trace")
        check(all(app.trace_source(e) == f"DMA{e.src}" for e in dma), "DMA records labelled DMAn")
        check(all(app.trace_source(e) == f"SM{e.src}" for e in app.s.events if e.name.startswith("stall")),
              "stall records labelled SMn")

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
