# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build libpio2sim.so (Verilator model of src/rtl/pio2_core.sv + the C ABI in sim/pio2sim.cpp).

The library lands in build/sim/ and is rebuilt when any RTL or wrapper file is newer than it.
"""
# @impl [SPEC-DBG-01] docs/DEBUGGER_SPECIFICATION.md

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RTL = ROOT / "src" / "rtl"
SIM = Path(__file__).resolve().parent / "sim"
OUT = ROOT / "build" / "sim"
LIB = OUT / "libpio2sim.so"
SOURCES = [ROOT / line.strip() for line in (RTL / "pio2_core.f").read_text().splitlines()
           if line.strip() and not line.startswith("#")]


def verilator():
    oss = Path(os.environ.get("PIO2_TOOLS", Path.home() / ".tools")) / "oss-cad-suite" / "bin" / "verilator"
    return str(oss) if oss.exists() else (shutil.which("verilator") or "verilator")


def stale():
    if not LIB.exists():
        return True
    t = LIB.stat().st_mtime
    deps = [*SOURCES, RTL / "pio2_core.f", SIM / "pio2sim.cpp", SIM / "pio2sim.vlt"]
    return any(p.stat().st_mtime > t for p in deps)


def build(force=False, quiet=True):
    if not force and not stale():
        return LIB
    # Fresh object directory on every rebuild: Verilator's make dependencies hold absolute paths, so an
    # obj/ left from a checkout at another path names sources that no longer exist (engineering log,
    # 2026-10-30).
    shutil.rmtree(OUT / "obj", ignore_errors=True)
    OUT.mkdir(parents=True, exist_ok=True)
    cmd = [verilator(), "--cc", "--exe", "--build", "--vpi", "-O2", "-j", str(os.cpu_count() or 2),
           "--top-module", "pio2_core", "-Wno-fatal", "-Mdir", str(OUT / "obj"), "-o", str(LIB),
           "-CFLAGS", "-fPIC -O2", "-LDFLAGS", "-shared",
           str(SIM / "pio2sim.vlt"), *map(str, SOURCES), str(SIM / "pio2sim.cpp")]
    env = dict(os.environ)
    vbin = Path(verilator()).parent
    env["PATH"] = os.pathsep.join([str(vbin), env.get("PATH", "")])
    r = subprocess.run(cmd, capture_output=quiet, text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError("libpio2sim build failed:\n" + ((r.stdout or "") + (r.stderr or ""))[-4000:])
    return LIB


if __name__ == "__main__":
    print(build(force=True, quiet=False))
