# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Run every cocotb suite in tests/cocotb (each test_*_cocotb.py) on Verilator.

  uv run pio2-cocotb                 # all suites
  uv run pio2-cocotb --only dshot    # suites whose name contains "dshot"
  uv run pio2-cocotb --sim icarus    # another simulator

Each suite runs in a temporary copy of tests/cocotb, so no sim_build/ lands in the repository.
A suite passes only if cocotb prints its TESTS= summary with FAIL=0; a suite that did not build or
did not run fails (never a silent pass).
"""
# @impl [SPEC-PIO-15] docs/SPECIFICATION.md (executable form of the cocotb-regression skill)

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests" / "cocotb"
SUMMARY = re.compile(r"TESTS=(\d+)\s+PASS=(\d+)\s+FAIL=(\d+)\s+SKIP=(\d+)")


def suites(only):
    names = sorted(p.stem for p in TESTS.glob("test_*_cocotb.py"))
    return [n for n in names if not only or only in n]


def tool_path():
    oss = Path(os.environ.get("PIO2_TOOLS", Path.home() / ".tools")) / "oss-cad-suite" / "bin"
    # The project venv first (its cocotb), then oss-cad-suite (simulators), then PATH. oss-cad-suite
    # ships its own cocotb for its own Python; it must not shadow the venv's.
    venv_bin = Path(sys.executable).parent
    return os.pathsep.join([str(venv_bin), str(oss), os.environ.get("PATH", "")])


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--only", default="", help="run suites whose name contains this text")
    ap.add_argument("--sim", default="verilator")
    ap.add_argument("--keep", action="store_true", help="keep the build directory and print it")
    a = ap.parse_args()
    names = suites(a.only)
    if not names:
        print(f"no suite matches {a.only!r}", file=sys.stderr)
        return 2
    env = dict(os.environ, PATH=tool_path(), SIM=a.sim)
    failures = 0
    tmp = tempfile.mkdtemp(prefix="pio2-cocotb-")
    try:
        work = Path(tmp) / "repo"
        shutil.copytree(ROOT / "src", work / "src")
        shutil.copytree(TESTS, work / "tests" / "cocotb")
        cwd = work / "tests" / "cocotb"
        for name in names:
            shutil.rmtree(cwd / "sim_build", ignore_errors=True)
            proc = subprocess.run(["make", f"MODULE={name}"], cwd=cwd, env=env, capture_output=True, text=True)
            out = proc.stdout + proc.stderr
            m = None
            for m in SUMMARY.finditer(out):
                pass
            ok = proc.returncode == 0 and m is not None and int(m.group(3)) == 0 and int(m.group(1)) > 0
            failures += 0 if ok else 1
            detail = m.group(0) if m else "no TESTS= line (did not build or run)"
            print(f"{'PASS' if ok else 'FAIL'}  {name:40s} {detail}")
            if not ok:
                print("\n".join(out.splitlines()[-25:]))
    finally:
        if a.keep:
            print(f"kept {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(names) - failures}/{len(names)} cocotb suites passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
