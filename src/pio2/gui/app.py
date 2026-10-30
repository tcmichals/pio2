# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""pio2 debugger GUI ([SPEC-DBG-10], [SPEC-DBG-15]): imgui-bundle front end on pio2.debug.Session.

    uv run --extra gui pio2-gui                 # opens the Blink example
    uv run --extra gui pio2-gui --example "UART TX"
    uv run --extra gui pio2-gui --screenshot out.png --frames 120   # render, save, exit
"""
# @impl [SPEC-DBG-10] [SPEC-DBG-15] docs/DEBUGGER_SPECIFICATION.md

import argparse
import struct
import zlib

import numpy as np
from imgui_bundle import hello_imgui, imgui, immapp, implot

from pio2.debug.examples import EXAMPLES
from pio2.debug.session import Session

ImVec2, ImVec4 = imgui.ImVec2, imgui.ImVec4

# Palette (dark, one accent per meaning)
BG = (0.075, 0.082, 0.098)
PANEL = (0.105, 0.114, 0.137)
PANEL2 = (0.135, 0.145, 0.175)
TEXT = (0.90, 0.92, 0.95)
MUTED = (0.55, 0.59, 0.66)
ACCENT = (0.18, 0.80, 0.72)            # teal
HIGH = (1.00, 0.70, 0.20)              # driven high: amber
LOW = (0.25, 0.42, 0.85)               # driven low: blue
SM_COLORS = [(0.18, 0.80, 0.72), (1.0, 0.55, 0.35), (0.65, 0.55, 1.0), (0.95, 0.40, 0.65)]


def col(rgb, a=1.0):
    return imgui.get_color_u32(ImVec4(rgb[0], rgb[1], rgb[2], a))


def v4(rgb, a=1.0):
    return ImVec4(rgb[0], rgb[1], rgb[2], a)


def setup_style():
    hello_imgui.apply_theme(hello_imgui.ImGuiTheme_.imgui_colors_dark)
    st = imgui.get_style()
    st.window_rounding = 10
    st.child_rounding = 8
    st.frame_rounding = 7
    st.popup_rounding = 8
    st.grab_rounding = 7
    st.tab_rounding = 7
    st.scrollbar_rounding = 8
    st.window_border_size = 0
    st.frame_border_size = 0
    st.window_padding = ImVec2(14, 12)
    st.frame_padding = ImVec2(12, 7)
    st.item_spacing = ImVec2(10, 8)
    st.cell_padding = ImVec2(8, 5)
    C = imgui.Col_
    colors = {
        C.window_bg: v4(PANEL), C.child_bg: v4(PANEL), C.popup_bg: v4(PANEL2),
        C.menu_bar_bg: v4(BG), C.title_bg: v4(BG), C.title_bg_active: v4(BG),
        C.title_bg_collapsed: v4(BG), C.text: v4(TEXT), C.text_disabled: v4(MUTED),
        C.frame_bg: v4(PANEL2), C.frame_bg_hovered: v4((0.17, 0.18, 0.22)),
        C.frame_bg_active: v4((0.20, 0.21, 0.26)),
        C.button: v4((0.16, 0.17, 0.21)), C.button_hovered: v4(ACCENT, 0.35),
        C.button_active: v4(ACCENT, 0.6), C.header: v4(ACCENT, 0.18),
        C.header_hovered: v4(ACCENT, 0.28), C.header_active: v4(ACCENT, 0.4),
        C.tab: v4(PANEL2), C.tab_hovered: v4(ACCENT, 0.35), C.tab_selected: v4(ACCENT, 0.25),
        C.slider_grab: v4(ACCENT), C.slider_grab_active: v4(ACCENT), C.check_mark: v4(ACCENT),
        C.separator: v4((0.2, 0.21, 0.25)), C.border: v4((0.2, 0.21, 0.25)),
        C.docking_empty_bg: v4(BG), C.table_header_bg: v4(PANEL2),
        C.table_row_bg_alt: v4((1, 1, 1), 0.02), C.plot_lines: v4(ACCENT),
    }
    for k, v in colors.items():
        st.set_color_(k, v)


def stall_reason(text, st):
    if not st["stalled"]:
        return ""
    op = text.split()[0] if text else ""
    if op == "pull" or (op == "out" and st["osr_count"] >= 32):
        return "TX FIFO empty"
    if op == "push" or op == "in":
        return "RX FIFO full"
    if op == "wait":
        return "waiting: " + text[5:]
    if op == "irq":
        return "irq wait"
    return "stalled"


class App:
    def __init__(self, example="Blink"):
        self.s = Session()
        self.example_name = example
        self.running = True
        self.speed = 200                    # clocks per frame
        self.window = 600                   # clocks shown in the logic analyzer
        self.f_sys = 125e6
        self.sel_sm = 0
        self.load_example(example)

    # ------------------------------------------------------------ model
    def load_example(self, name):
        self.s.reset()
        self.example_name = name
        self.ex = EXAMPLES[name](self.s)
        self.speed, self.window = self.ex.speed, self.ex.window
        for p in self.ex.parts:
            p.attach(self.s)

    def advance(self, n, feed=True):
        if feed and self.ex.feed:            # stimulus uses register accesses, which run clocks
            self.ex.feed(self.s)
        self.s.step(n)
        for p in self.ex.parts:
            p.update(self.s)

    def frame(self):
        if self.running:
            self.advance(self.speed)

    # ------------------------------------------------------------ windows
    def controls(self):
        s = self.s
        if imgui.button("  Pause  " if self.running else "  Run  "):
            self.running = not self.running
        imgui.same_line()
        imgui.begin_disabled(self.running)
        if imgui.button("Step clock"):
            self.advance(1, feed=False)       # exactly one clock
        imgui.same_line()
        if imgui.button(f"Step instr SM{self.sel_sm}"):
            if s.sm_state(self.sel_sm)["enabled"]:
                s.step_instr(self.sel_sm)
            for p in self.ex.parts:
                p.update(s)
        imgui.end_disabled()
        imgui.same_line()
        if imgui.button("Reset"):
            self.load_example(self.example_name)
        imgui.same_line()
        imgui.set_next_item_width(160)
        _, self.speed = imgui.slider_int("clocks / frame", self.speed, 1, 20000,
                                         flags=imgui.SliderFlags_.logarithmic)
        imgui.same_line()
        imgui.set_next_item_width(200)
        if imgui.begin_combo("##example", self.example_name):
            for name in EXAMPLES:
                if imgui.selectable(name, name == self.example_name)[0]:
                    self.load_example(name)
            imgui.end_combo()
        imgui.same_line()
        t = s.cycle / self.f_sys
        imgui.text_colored(v4(ACCENT), f"cycle {s.cycle:,}")
        imgui.same_line()
        imgui.text_colored(v4(MUTED), f"{t * 1e6:,.2f} us at {self.f_sys / 1e6:.0f} MHz")

    def source(self):
        s = self.s
        imgui.text_colored(v4(ACCENT), self.ex.title)
        imgui.push_text_wrap_pos(0)
        imgui.text_colored(v4(MUTED), self.ex.about)
        imgui.pop_text_wrap_pos()
        imgui.separator()
        states = {sm: s.sm_state(sm) for sm in s.programs}
        owner = {}
        for sm, (p, off) in s.programs.items():
            for a, w, t in s.listing(sm):
                owner.setdefault(a, (p["name"], w, t))
        for sm, (p, off) in s.programs.items():
            imgui.text_colored(v4(SM_COLORS[sm]), f"SM{sm}")
            imgui.same_line()
            imgui.text(p["name"])
            st = states[sm]
            imgui.same_line()
            imgui.text_colored(v4(MUTED), f"wrap {st['wrap_bottom']}..{st['wrap_top']}")
        imgui.separator()
        flags = imgui.TableFlags_.row_bg | imgui.TableFlags_.sizing_fixed_fit
        if imgui.begin_table("src", 4, flags):
            imgui.table_setup_column("", imgui.TableColumnFlags_.width_fixed, 70)
            imgui.table_setup_column("addr")
            imgui.table_setup_column("word")
            imgui.table_setup_column("instruction", imgui.TableColumnFlags_.width_stretch)
            for a in range(32):
                if a not in owner:
                    continue
                name, w, t = owner[a]
                imgui.table_next_row()
                imgui.table_next_column()
                marks = [sm for sm, st in states.items() if st["pc"] == a]
                for sm in marks:
                    imgui.text_colored(v4(SM_COLORS[sm]), f"SM{sm}>")
                    imgui.same_line()
                imgui.table_next_column()
                imgui.text_colored(v4(MUTED), f"{a:2d}")
                imgui.table_next_column()
                imgui.text_colored(v4(MUTED), f"{w:04x}")
                imgui.table_next_column()
                if marks:
                    imgui.table_set_bg_color(imgui.TableBgTarget_.row_bg1, col(SM_COLORS[marks[0]], 0.16))
                    imgui.text(t)
                else:
                    imgui.text_colored(v4((0.78, 0.80, 0.85)), t)
            imgui.end_table()

    def fifo_boxes(self, label, words, cap):
        imgui.text_colored(v4(MUTED), label)
        imgui.same_line(80)
        dl = imgui.get_window_draw_list()
        p = imgui.get_cursor_screen_pos()
        gap, h = 6, 22
        w = max(40.0, min(86.0, (imgui.get_content_region_avail().x - 80) / max(cap, 1) - gap))
        for i in range(cap):
            x = p.x + i * (w + gap)
            filled = i < len(words)
            dl.add_rect_filled(ImVec2(x, p.y), ImVec2(x + w, p.y + h),
                               col(ACCENT, 0.25) if filled else col(PANEL2), 6)
            if filled:
                txt = f"{words[i]:08x}" if w >= 80 else f"{words[i]:x}"
                dl.add_text(ImVec2(x + 6, p.y + 3), col(TEXT), txt)
        imgui.dummy(ImVec2(cap * (w + gap), h + 2))

    def state(self):
        s = self.s
        if imgui.begin_tab_bar("sms"):
            for sm in range(4):
                if imgui.begin_tab_item(f"SM{sm}")[0]:
                    self.sel_sm = sm
                    st = s.sm_state(sm)
                    text = next((t for a, w, t in s.listing(sm) if a == st["pc"]), f"{st['instr']:04x}")
                    on = "running" if st["enabled"] else "disabled"
                    imgui.text_colored(v4(SM_COLORS[sm]), f"SM{sm}  {on}")
                    reason = stall_reason(text, st)
                    if reason:
                        imgui.same_line()
                        imgui.text_colored(v4(HIGH), f"stalled: {reason}")
                    flags = imgui.TableFlags_.row_bg | imgui.TableFlags_.sizing_stretch_prop
                    if imgui.begin_table("st", 2, flags):
                        rows = [
                            ("PC", f"{st['pc']}   {text}"),
                            ("X", f"0x{st['reg_x']:08x}   {st['reg_x']}"),
                            ("Y", f"0x{st['reg_y']:08x}   {st['reg_y']}"),
                            ("ISR", f"0x{st['isr']:08x}   shift count {st['isr_count']}"),
                            ("OSR", f"0x{st['osr']:08x}   shift count {st['osr_count']}"),
                            ("delay", str(st["delay_cycles"])),
                            ("clock div", f"{st['clkdiv'] >> 8}.{st['clkdiv'] & 0xFF:03d}/256   "
                                          f"count {st['clkdiv_int_cnt']}"),
                            ("PINCTRL", f"0x{st['pinctrl']:08x}"),
                            ("EXECCTRL", f"0x{st['execctrl']:08x}"),
                            ("SHIFTCTRL", f"0x{st['shiftctrl']:08x}"),
                        ]
                        for k, v in rows:
                            imgui.table_next_row()
                            imgui.table_next_column()
                            imgui.text_colored(v4(MUTED), k)
                            imgui.table_next_column()
                            imgui.text(v)
                        imgui.end_table()
                    jt = st["shiftctrl"] >> 30 & 1
                    jr = st["shiftctrl"] >> 31 & 1 and not jt
                    imgui.spacing()
                    self.fifo_boxes("TX FIFO", st["tx_fifo"], 8 if jt else (0 if jr else 4))
                    self.fifo_boxes("RX FIFO", st["rx_fifo"], 8 if jr else (0 if jt else 4))
                    imgui.end_tab_item()
            imgui.end_tab_bar()
        flags = s.irq_flags()
        imgui.text_colored(v4(MUTED), "IRQ flags")
        imgui.same_line(80)
        dl = imgui.get_window_draw_list()
        p = imgui.get_cursor_screen_pos()
        for i in range(8):
            c = ImVec2(p.x + 10 + i * 26, p.y + 10)
            dl.add_circle_filled(c, 8, col(HIGH) if flags >> i & 1 else col(PANEL2))
            dl.add_text(ImVec2(c.x - 3, c.y + 10), col(MUTED), str(i))
        imgui.dummy(ImVec2(220, 36))

    def canvas(self):
        s = self.s
        out, dir_, inp = s.pins()
        dl = imgui.get_window_draw_list()
        p0 = imgui.get_cursor_screen_pos()
        avail = imgui.get_content_region_avail()
        dl.add_rect_filled(p0, ImVec2(p0.x + avail.x, p0.y + avail.y), col(BG), 10)
        # dotted grid, breadboard feel
        step = 24
        for gx in range(int(avail.x // step)):
            for gy in range(int(avail.y // step)):
                dl.add_circle_filled(ImVec2(p0.x + 12 + gx * step, p0.y + 12 + gy * step), 1.2,
                                     col((1, 1, 1), 0.06))
        chip_w, pin_pitch = 240, 22
        chip_h = 16 * pin_pitch + 40
        cx, cy = p0.x + 90, p0.y + max(60, (avail.y - chip_h) / 2)
        dl.add_rect_filled(ImVec2(cx + 4, cy + 6), ImVec2(cx + chip_w + 4, cy + chip_h + 6),
                           col((0, 0, 0), 0.35), 14)
        dl.add_rect_filled(ImVec2(cx, cy), ImVec2(cx + chip_w, cy + chip_h), col((0.16, 0.17, 0.2)), 14)
        dl.add_rect(ImVec2(cx, cy), ImVec2(cx + chip_w, cy + chip_h), col(ACCENT, 0.5), 14, thickness=1.5)
        dl.add_text(ImVec2(cx + chip_w / 2 - 30, cy + 6), col(TEXT), "pio2 tile")
        dl.add_text(ImVec2(cx + chip_w / 2 - 38, cy + chip_h - 22), col(MUTED), "4 SMs · RTL")
        pin_pos = {}
        for n in range(32):
            left = n < 16
            row = n if left else 31 - n
            y = cy + 28 + row * pin_pitch
            x = cx if left else cx + chip_w
            pin_pos[n] = (x, y)
            driven = dir_ >> n & 1
            level = (inp >> n) & 1
            c = (HIGH if level else LOW) if driven else MUTED
            dx = -14 if left else 14
            dl.add_line(ImVec2(x, y), ImVec2(x + dx, y), col(c, 0.9 if driven else 0.5), 3)
            if driven:
                dl.add_circle_filled(ImVec2(x + dx, y), 5, col(c))
            else:
                dl.add_circle(ImVec2(x + dx, y), 5, col(c, 0.7), 0, 1.5)
            name = self.ex.labels.get(n, "")
            label = f"GP{n}" + (f" {name}" if name else "")
            tc = col(TEXT) if name else col(MUTED, 0.8)
            if left:                           # labels inside the chip: the outside is for wires
                dl.add_text(ImVec2(x + 8, y - 8), tc, label)
            else:
                dl.add_text(ImVec2(x - 8 - 7.0 * len(label), y - 8), tc, label)
        # parts in a column to the right, wired to their pins
        px = cx + chip_w + 190
        py = cy + 10
        for part in self.ex.parts:
            pin = next(iter(part.pins.values()))
            sx, sy = pin_pos[pin]
            sx += 14 if pin >= 16 else -14
            target = ImVec2(px, py + 30)
            if pin < 16:                       # left pins: out past the labels, over the chip, to the part
                lane = self.ex.parts.index(part)
                nlanes = len(self.ex.parts)
                left = p0.x + 18 + 12 * lane            # nested lanes: no two wires cross
                top = cy - 26 - 12 * (nlanes - 1 - lane)
                pts = [ImVec2(sx, sy), ImVec2(left, sy), ImVec2(left, top), ImVec2(px - 40 - 12 * lane, top),
                       ImVec2(px - 40 - 12 * lane, target.y), target]
                for a_, b_ in zip(pts, pts[1:]):
                    dl.add_line(a_, b_, col(ACCENT, 0.7), 2.5)
                for c_ in pts[1:-1]:
                    dl.add_circle_filled(c_, 1.25, col(ACCENT, 0.7))
            else:
                dl.add_bezier_cubic(ImVec2(sx, sy), ImVec2(sx + 80, sy), ImVec2(px - 80, py + 30),
                                    target, col(ACCENT, 0.7), 2.5)
            dl.add_rect_filled(ImVec2(px, py), ImVec2(px + 200, py + 60), col(PANEL2), 10)
            dl.add_text(ImVec2(px + 70, py + 10), col(TEXT), part.name)
            dl.add_text(ImVec2(px + 70, py + 30), col(MUTED), f"GP{pin}")
            centre = ImVec2(px + 34, py + 30)
            if part.kind == "led":
                if part.on:
                    for r, a in ((26, 0.10), (20, 0.18), (15, 0.35)):
                        dl.add_circle_filled(centre, r, col(part.color, a))
                    dl.add_circle_filled(centre, 11, col(part.color))
                else:
                    dl.add_circle_filled(centre, 11, col(part.color, 0.18))
                dl.add_circle(centre, 11, col((1, 1, 1), 0.25), 0, 1.5)
            elif part.kind == "button":
                imgui.set_cursor_screen_pos(ImVec2(px + 12, py + 10))
                imgui.invisible_button(f"##{part.name}", ImVec2(44, 40))
                if imgui.is_item_activated():
                    part.set(s, True)
                elif imgui.is_item_deactivated():
                    part.set(s, False)
                pressed = part.pressed
                c = ACCENT if pressed else (0.3, 0.32, 0.38)
                dl.add_rect_filled(ImVec2(px + 14, py + 14), ImVec2(px + 54, py + 46),
                                   col((0.2, 0.21, 0.25)), 8)
                dl.add_circle_filled(centre, 12 if not pressed else 10, col(c))
                dl.add_text(ImVec2(px + 120, py + 30), col(MUTED), "hold me")
            elif part.kind == "terminal":
                dl.add_rect_filled(ImVec2(px + 14, py + 14), ImVec2(px + 54, py + 46),
                                   col((0.05, 0.06, 0.07)), 6)
                dl.add_text(ImVec2(px + 20, py + 22), col(ACCENT), ">_")
            py += 90
        imgui.dummy(avail)

    def terminal(self):
        terms = [p for p in self.ex.parts if p.kind == "terminal"]
        if not terms:
            imgui.text_colored(v4(MUTED), "No terminal in this example (try UART TX).")
            return
        for t in terms:
            imgui.text_colored(v4(MUTED), f"{t.name}: GP{t.pins['rx']}, 8N1, {t.cpb} clocks per bit, "
                                          f"{self.f_sys / t.cpb / 1e6:.2f} Mbaud at {self.f_sys / 1e6:.0f} MHz")
            imgui.begin_child("term", ImVec2(0, 0), imgui.ChildFlags_.borders)
            imgui.push_style_color(imgui.Col_.text, v4(ACCENT))
            imgui.text_unformatted(t.text[-4000:])
            imgui.pop_style_color()
            if imgui.get_scroll_y() >= imgui.get_scroll_max_y() - 4:
                imgui.set_scroll_here_y(1.0)
            imgui.end_child()

    def trace_view(self):
        """Hardware trace records from the core's event FIFO ([SPEC-TRC-03]), newest last."""
        s = self.s
        dropped, merged = s.trace_counters()
        imgui.text_colored(v4(MUTED), f"{len(s.events):,} records from the RTL event FIFO   "
                                      f"dropped {dropped}   merged {merged}")
        flags = (imgui.TableFlags_.row_bg | imgui.TableFlags_.scroll_y | imgui.TableFlags_.sizing_fixed_fit
                 | imgui.TableFlags_.borders_inner_v)
        if imgui.begin_table("trace", 4, flags, ImVec2(0, 0)):
            imgui.table_setup_scroll_freeze(0, 1)
            imgui.table_setup_column("cycle")
            imgui.table_setup_column("source")
            imgui.table_setup_column("event", imgui.TableColumnFlags_.width_stretch)
            imgui.table_setup_column("data")
            imgui.table_headers_row()
            for e in s.events[-400:]:
                imgui.table_next_row()
                imgui.table_next_column()
                imgui.text_colored(v4(MUTED), f"{e.cycle:,}")
                imgui.table_next_column()
                if e.src == 0xFF:
                    imgui.text_colored(v4(HIGH), "IRQ")
                elif e.src < 4:
                    imgui.text_colored(v4(SM_COLORS[e.src]), f"SM{e.src}")
                else:
                    imgui.text(f"{e.src}")
                imgui.table_next_column()
                imgui.text(e.name)
                imgui.table_next_column()
                if e.name.startswith("stall"):
                    imgui.text(f"pc {e.data}")
                elif e.name.startswith("irq"):
                    imgui.text(f"flag {e.data}")
                else:
                    imgui.text(f"{e.data}")
            if imgui.get_scroll_y() >= imgui.get_scroll_max_y() - 4:
                imgui.set_scroll_here_y(1.0)
            imgui.end_table()

    def analyzer(self):
        tr = self.s.trace
        n = min(len(tr), self.window)
        imgui.set_next_item_width(220)
        _, self.window = imgui.slider_int("clocks shown", self.window, 50, 50000,
                                          flags=imgui.SliderFlags_.logarithmic)
        if n == 0:
            return
        out = np.frombuffer(tr.out, dtype=np.uint32)[-n:]
        dir_ = np.frombuffer(tr.dir, dtype=np.uint32)[-n:]
        inp = np.frombuffer(tr.inp, dtype=np.uint32)[-n:]
        pcs = np.frombuffer(tr.pcs, dtype=np.uint8)[-4 * n:].reshape(-1, 4)
        x0 = tr.start + len(tr) - n
        xs = np.arange(x0, x0 + n, dtype=np.float64)
        size = imgui.get_content_region_avail()
        cond = imgui.Cond_.always if self.running else imgui.Cond_.once
        h_pins = max(80.0, size.y * 0.55)
        probes = list(self.ex.probes)
        if implot.begin_plot("##pins", ImVec2(-1, h_pins), implot.Flags_.no_legend):
            implot.setup_axes("", "", implot.AxisFlags_.no_tick_labels, implot.AxisFlags_.lock)
            implot.setup_axis_limits(implot.ImAxis_.x1, xs[0], xs[-1], cond)
            implot.setup_axis_limits(implot.ImAxis_.y1, -0.3, len(probes) - 0.1, imgui.Cond_.always)
            names = [f"GP{p} {self.ex.labels.get(p, '')}".strip() for p in probes]
            implot.setup_axis_ticks(implot.ImAxis_.y1, [k + 0.35 for k in range(len(probes))], names)
            for k, pin in enumerate(probes):
                bits = ((inp >> pin) & 1).astype(np.float64)
                driven = ((dir_ >> pin) & 1).astype(bool)
                spec = implot.Spec()
                spec.line_color = v4(SM_COLORS[k % 4])
                spec.line_weight = 2
                implot.plot_stairs(names[k], xs, k + 0.7 * bits, spec)
                if (~driven).any():          # mark clocks where nobody drives the line (pull only)
                    und = np.where(driven, np.nan, k + 0.7 * bits)
                    spec2 = implot.Spec()
                    spec2.line_color = v4(MUTED, 0.9)
                    spec2.line_weight = 2
                    implot.plot_stairs(names[k] + " (released)", xs, und, spec2)
            implot.end_plot()
        if implot.begin_plot("##pc", ImVec2(-1, -1)):
            implot.setup_axes("cycle", "PC")
            implot.setup_axis_limits(implot.ImAxis_.x1, xs[0], xs[-1], cond)
            implot.setup_axis_limits(implot.ImAxis_.y1, -1, 32, imgui.Cond_.once)
            for sm in self.s.programs:
                spec = implot.Spec()
                spec.line_color = v4(SM_COLORS[sm])
                spec.line_weight = 2
                implot.plot_stairs(f"SM{sm} PC", xs, pcs[:, sm].astype(np.float64), spec)
            stalls = [e for e in self.s.events if e.name == "stall begin" and xs[0] <= e.cycle <= xs[-1]]
            if stalls:                                   # hardware trace: where each SM started stalling
                spec = implot.Spec()
                spec.line_color = v4(HIGH, 0.5)
                implot.plot_inf_lines("stall (trace)", np.array([float(e.cycle) for e in stalls]), spec)
            implot.end_plot()
        del out


def write_png(path, img):
    img = np.ascontiguousarray(img[:, :, :3] if img.shape[2] == 4 else img, dtype=np.uint8)
    h, w, _ = img.shape
    raw = b"".join(b"\x00" + img[y].tobytes() for y in range(h))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def main():
    ap = argparse.ArgumentParser(description="pio2 debugger GUI")
    ap.add_argument("--example", default="Blink", choices=list(EXAMPLES))
    ap.add_argument("--screenshot", help="render, save a PNG here, exit")
    ap.add_argument("--frames", type=int, default=90, help="frames before the screenshot")
    ap.add_argument("--press", action="store_true", help="hold the example's button (screenshots)")
    ap.add_argument("--warmup", type=int, default=0, help="clocks to run before the window opens")
    ap.add_argument("--paused", action="store_true", help="open paused")
    a = ap.parse_args()
    app = App(a.example)
    if a.press:
        for p in app.ex.parts:
            if p.kind == "button":
                p.set(app.s, True)
    while a.warmup > 0:
        k = min(a.warmup, app.speed)
        app.advance(k)
        a.warmup -= k
    app.running = not a.paused
    frames = {"n": 0}

    rp = hello_imgui.RunnerParams()
    rp.app_window_params.window_title = "pio2 debugger: PIO RTL on Verilator"
    rp.app_window_params.window_geometry.size = (1680, 1000)
    rp.imgui_window_params.default_imgui_window_type = (
        hello_imgui.DefaultImGuiWindowType.provide_full_screen_dock_space)
    rp.imgui_window_params.background_color = v4(BG)
    rp.callbacks.setup_imgui_style = setup_style
    rp.fps_idling.enable_idling = False

    def before_frame():
        app.frame()
        frames["n"] += 1
        if a.screenshot and frames["n"] >= a.frames:
            hello_imgui.get_runner_params().app_shall_exit = True

    rp.callbacks.pre_new_frame = before_frame

    def split(new, direction, ratio, initial="MainDockSpace"):
        d = hello_imgui.DockingSplit()
        d.initial_dock, d.new_dock, d.direction, d.ratio = initial, new, direction, ratio
        return d

    Dir = imgui.Dir
    rp.docking_params.docking_splits = [
        split("Top", Dir.up, 0.07),
        split("Bottom", Dir.down, 0.32),
        split("Left", Dir.left, 0.25),
        split("Right", Dir.right, 0.30),
        split("BottomRight", Dir.right, 0.30, initial="Bottom"),
    ]

    def win(label, space, fn):
        w = hello_imgui.DockableWindow()
        w.label, w.dock_space_name, w.gui_function = label, space, fn
        w.can_be_closed = False
        return w

    rp.docking_params.dockable_windows = [
        win("Controls", "Top", app.controls),
        win("Program", "Left", app.source),
        win("Parts", "MainDockSpace", app.canvas),
        win("State machines", "Right", app.state),
        win("Logic analyzer", "Bottom", app.analyzer),
        win("Terminal", "BottomRight", app.terminal),
        win("Trace", "BottomRight", app.trace_view),
    ]
    rp.ini_disable = True                    # layout from code every start (saving: [SPEC-DBG-10], later)
    rp.docking_params.layout_condition = hello_imgui.DockingLayoutCondition.application_start
    addons = immapp.AddOnsParams()
    addons.with_implot = True
    immapp.run(rp, addons)
    if a.screenshot:
        write_png(a.screenshot, hello_imgui.final_app_window_screenshot())
        print(f"saved {a.screenshot} after {frames['n']} frames, cycle {app.s.cycle}")
    app.s.close()


if __name__ == "__main__":
    main()
