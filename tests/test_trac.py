# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""pio2-trac: checks -> evidence records per requirement, read by AbstractX's SpecTrace matrix."""
# @verifies [SPEC-VER-02] docs/INTERCONNECT_SPECIFICATION.md

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from pio2 import trac
from pio2.trac import FileResult, Requirement, judge

GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")


def abstractx():
    cands = [os.environ.get("ABSTRACTX_DIR", "")] + [str(p / "AbstractX") for p in Path(trac.ROOT).parents]
    for c in cands:
        if c and (Path(c) / "tools" / "spectrace_matrix.py").is_file():
            return Path(c)
    return None


SPEC = """# Spec

### `[SPEC-T-01]` Host only
* **Requirement**: x.
* **Verified by**: `host-tests` (`tests/test_ok.py`: notes with `pull` in them)
* **Implementation Target**: `src/m.py`

### `[SPEC-T-02]` Host and RTL
* **Requirement**: y.
* **Verified by**: `host-tests`, `cocotb-regression`
* **Implementation Target**: `src/m.py`

### `[SPEC-T-03]` Fails
* **Requirement**: z.
* **Verified by**: `host-tests`
* **Implementation Target**: `src/n.py`
"""


def skill(name, runs_on):
    return f"---\nname: {name}\nruns_on: {runs_on}\nverifies: \"x\"\ndescription: \"x\"\n---\n# {name}\n"


def make_repo(tmp: Path) -> Path:
    files = {
        "docs/T_SPECIFICATION.md": SPEC,
        ".agents/skills/host-tests/SKILL.md": skill("host-tests", "host"),
        ".agents/skills/cocotb-regression/SKILL.md": skill("cocotb-regression", "rtl-sim"),
        "src/m.py": "# @impl [SPEC-T-01] [SPEC-T-02] docs/T_SPECIFICATION.md\nX = 1\n",
        "src/n.py": "# @impl [SPEC-T-03] docs/T_SPECIFICATION.md\nY = 1\n",
        "tests/test_ok.py": "# @verifies [SPEC-T-01] [SPEC-T-02] docs/T_SPECIFICATION.md\ndef test_a():\n    assert 1\n",
        "tests/test_bad.py": "# @verifies [SPEC-T-03] docs/T_SPECIFICATION.md\ndef test_b():\n    assert 0\n",
    }
    for rel, text in files.items():
        (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp / rel).write_text(text)
    git(tmp, "init", "-q")
    git(tmp, "add", "-A")
    git(tmp, "commit", "-qm", "init")
    return tmp


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, env=GIT_ENV, check=True, capture_output=True, text=True).stdout


# ---------------------------------------------------------------- unit
def test_parse_requirements_like_the_matrix(tmp_path):
    make_repo(tmp_path)
    reqs = trac.parse_requirements(tmp_path)
    assert {t: r.skills for t, r in reqs.items()} == {
        "SPEC-T-01": ["host-tests"], "SPEC-T-02": ["host-tests", "cocotb-regression"], "SPEC-T-03": ["host-tests"]}
    v = trac.scan_verifies(tmp_path)
    assert v == {"tests/test_bad.py": ["SPEC-T-03"], "tests/test_ok.py": ["SPEC-T-01", "SPEC-T-02"]}
    assert trac.skill_of("tests/cocotb/test_x_cocotb.py") == "cocotb-regression"
    assert trac.skill_of("tests/formal/x.sv") == "formal-check"
    assert trac.skill_of("tests/test_x.py") == "host-tests"


def test_judge():
    ok, bad = FileResult("pass", 3, 0, "3 passed"), FileResult("fail", 1, 1, "1 failed")
    r = Requirement("SPEC-A-01", "s", ["host-tests"], {"host-tests": ["tests/a.py"]})
    assert judge(r, {"tests/a.py": ok}, None)[0] == "pass"
    assert judge(r, {"tests/a.py": bad}, None)[0] == "fail"
    assert judge(r, {}, None)[0] == "skip"                                      # its test did not run
    r2 = Requirement("SPEC-A-02", "s", ["host-tests", "cocotb-regression"], {"host-tests": ["tests/a.py"]})
    res, notes, _ = judge(r2, {"tests/a.py": ok}, None)
    assert res == "skip" and "not run: cocotb-regression" in notes
    r3 = Requirement("SPEC-A-03", "s", ["host-tests"], {"host-tests": ["tests/a.py"], "cocotb-regression": ["c.py"]})
    assert judge(r3, {"tests/a.py": ok, "c.py": bad}, None)[0] == "fail"           # failing extra test counts
    r4 = Requirement("SPEC-A-04", "s", ["rtl-lint"], {})
    assert judge(r4, {}, FileResult("pass", 7, 0, "clean"))[0] == "pass"
    assert judge(r4, {}, None)[0] == "skip"
    assert judge(Requirement("SPEC-A-05", "s", ["board-bringup"], {}), {}, None)[0] == "skip"


def test_record_refuses_a_dirty_tree(tmp_path, capsys):
    make_repo(tmp_path)
    (tmp_path / "src" / "m.py").write_text("X = 2\n")
    assert trac.main(["--record", "--no-cocotb", "--root", str(tmp_path)]) == 2
    assert "refusing to record" in capsys.readouterr().err
    assert not (tmp_path / trac.LEDGER_DIR).exists()


def test_records_and_ledger_format(tmp_path):
    make_repo(tmp_path)
    assert trac.main(["--record", "--no-cocotb", "--root", str(tmp_path)]) == 1          # SPEC-T-03 fails
    recs = {r["requirements"][0]: r for r in trac.read_ledger(tmp_path)}
    assert {t: r["result"] for t, r in recs.items()} == {"SPEC-T-01": "pass", "SPEC-T-02": "skip",
                                                         "SPEC-T-03": "fail"}
    head = git(tmp_path, "rev-parse", "--short=12", "HEAD").strip()
    for r in recs.values():           # AbstractX [SPEC-AUDIT-09] fields
        assert {"time", "commit", "dirty", "host", "family", "check", "requirements", "result", "metrics",
                "delta", "duration_s", "tool", "source"} <= set(r)
        assert r["commit"] == head and r["source"] == "host"
    files = list((tmp_path / trac.LEDGER_DIR).glob("*/*.jsonl"))
    assert len(files) == 1 and files[0].parent.name == recs["SPEC-T-01"]["time"][:7]
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "evidence")
    trac.main(["--record", "--no-cocotb", "--root", str(tmp_path)])                    # second run: deltas
    last = [json.loads(ln) for ln in files[0].read_text().splitlines()][-3:]
    assert all(r["delta"] == {"tests_passed": 0, "tests_failed": 0} for r in last)


@pytest.mark.skipif(abstractx() is None, reason="no AbstractX checkout (set ABSTRACTX_DIR)")
def test_abstractx_matrix_shows_verified(tmp_path):
    """The records turn into VERIFIED / STALE / FAILING in AbstractX's matrix, and a later change to an
    implementation file makes the record stale."""
    make_repo(tmp_path)
    trac.main(["--record", "--no-cocotb", "--root", str(tmp_path)])
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "evidence")
    tool = abstractx() / "tools" / "spectrace_matrix.py"

    def matrix():
        out = subprocess.run([sys.executable, str(tool), "--root", str(tmp_path), "--matrix"],
                             capture_output=True, text=True, timeout=120).stdout
        return {k: int(v) for k, v in re.findall(r"^\s+(VERIFIED|STALE|FAILING)\s*:\s*(\d+)", out, re.M)}

    assert matrix() == {"VERIFIED": 1, "STALE": 1, "FAILING": 1}
    (tmp_path / "src" / "m.py").write_text("# @impl [SPEC-T-01] [SPEC-T-02] docs/T_SPECIFICATION.md\nX = 2\n")
    git(tmp_path, "commit", "-qam", "change")
    assert matrix() == {"VERIFIED": 0, "STALE": 2, "FAILING": 1}


# ---------------------------------------------------------------- the checks reach what they claim to
def module(name, body=""):
    return f"module {name}(input logic clk);\n{body}\nendmodule\n"


def lint_tree(tmp_path, files):
    for path, text in files.items():
        p = tmp_path / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return trac.run_lint(tmp_path)


needs_verilator = pytest.mark.skipif(not __import__("shutil").which("verilator", path=trac.tool_env()["PATH"]),
                                     reason="no verilator")


@needs_verilator
def test_lint_finds_modules_in_any_rtl_folder(tmp_path):
    """Before the fix the folders were a fixed list without src/rtl/axi: pio2_core, pio2_core_regs and
    pio2_dma stopped at MODMISSING and were never linted. A new folder must just work."""
    r = lint_tree(tmp_path, {"src/rtl/top.sv": module("top", "new_block u(.clk(clk));"),
                             "src/rtl/new_folder/new_block.sv": module("new_block")})
    assert r.result == "pass", r.detail


@needs_verilator
def test_lint_still_fails_a_forbidden_warning(tmp_path):
    """The other way round: our own RTL is not waived."""
    r = lint_tree(tmp_path, {"src/rtl/bad.sv": module("bad", """
    logic [1:0] s; logic q;
    always_ff @(posedge clk) case (s) 2'd0: q <= 1'b0; endcase""")})
    assert r.result == "fail" and "CASEINCOMPLETE" in r.detail, r.detail


@needs_verilator
def test_lint_waives_third_party_but_not_ours():
    """The real tree: every module is linted (no %Error), and pio2_core is clean, which needs the
    third_party.vlt waiver to match (CASEINCOMPLETE in Forencich axil_crossbar_addr.v)."""
    r = trac.run_lint(Path(trac.ROOT))
    assert "%Error" not in r.detail, r.detail
    assert "pio2_core" not in r.detail and "pio2_dma" not in r.detail, r.detail


def test_bare_pytest_collects_only_our_tests():
    """The host-tests skill's command is `uv run pytest`. Without testpaths it collected
    third_party/verilog-axis/tb and stopped with 129 errors (no myhdl) before running anything."""
    p = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q"], cwd=trac.ROOT,
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stdout[-800:]
    assert not [ln for ln in p.stdout.splitlines() if ln.startswith("third_party/")], p.stdout[-800:]
    assert "tests/test_trac.py" in p.stdout
