# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build libpio2sim.so (Verilator model of src/rtl/asp_pio_tile.sv + the C ABI in sim/pio2sim.cpp).

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
SOURCES = [RTL / "asp_pio_sm.sv", RTL / "asp_pio_tlp_ingress.sv", RTL / "asp_pio_tlp_shim.sv",
           RTL / "asp_pio_tile.sv"]


def verilator():
    oss = Path(os.environ.get("PIO2_TOOLS", Path.home() / ".tools")) / "oss-cad-suite" / "bin" / "verilator"
    return str(oss) if oss.exists() else (shutil.which("verilator") or "verilator")


def stale():
    if not LIB.exists():
        return True
    t = LIB.stat().st_mtime
    return any(p.stat().st_mtime > t for p in [*SOURCES, SIM / "pio2sim.cpp", SIM / "pio2sim.vlt"])


def build(force=False, quiet=True):
    if not force and not stale():
        return LIB
    OUT.mkdir(parents=True, exist_ok=True)
    cmd = [verilator(), "--cc", "--exe", "--build", "--vpi", "-O2", "-j", str(os.cpu_count() or 2),
           "--top-module", "asp_pio_tile", "-Mdir", str(OUT / "obj"), "-o", str(LIB),
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
