# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""pio2-trac: run the checks, map their results to spec tags, and record evidence for the matrix.

    uv run pio2-trac                    # run pytest + pio2-cocotb, print the verdict per requirement
    uv run pio2-trac --record           # ... and append the evidence records (clean tree only)
    uv run pio2-trac --formal --lint    # also the formal BMC runs (tests/formal/*.sby) and RTL lint
    uv run pio2-trac --no-cocotb        # pytest only (requirements that need cocotb stay unverified)

Records use AbstractX's evidence ledger format ([SPEC-AUDIT-09] in AbstractX docs/SPECTRACE.md), one
JSON line per requirement in docs/verification/evidence/<YYYY-MM>/<host>.jsonl, so
`python3 ../AbstractX/tools/spectrace_matrix.py --root . --matrix` turns them into VERIFIED, FAILING
or STALE. Its rules: a record counts for the requirement whose tag it lists; it goes stale when the
spec file or a file with the requirement's @impl changed after the recorded commit.

How a requirement's record is judged here (stricter than "some test passed"):
* every test file with `@verifies [TAG]` that ran must pass (one failure: `fail`);
* every skill on the requirement's `Verified by` line must have run for it: host-tests = a pytest file
  with the tag, cocotb-regression = a cocotb suite with the tag, formal-check = a formal file with the
  tag (with --formal), rtl-lint = the lint run (with --lint). A skill that did not run (board-bringup,
  fpga-fit, or a missing test) makes the record `skip`, which the matrix shows as STALE.

Recording needs a clean tree (no change to a tracked file and no untracked file outside the ledger):
the record names HEAD, so the checked code must be the committed code.
"""
# @impl [SPEC-VER-02] docs/INTERCONNECT_SPECIFICATION.md (evidence ledger; requirement proposed to the lead)

import argparse
import datetime as dt
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LEDGER_DIR = Path("docs/verification/evidence")
SPEC_GLOB = "docs/*SPECIFICATION.md"

SPEC_RE = re.compile(r"^###\s+`?\[(SPEC-[A-Z0-9\-]+)\]`?\s*(.*)")
HEADING_RE = re.compile(r"^#{1,3}\s")
VERIFIED_RE = re.compile(r"^\s*[*-]\s+\*\*Verified by\*\*\s*:\s*(.*)")
SKILL_TOKEN_RE = re.compile(r"`([a-z0-9][a-z0-9\-]*)`")
TAG_RE = re.compile(r"\[(SPEC-[A-Z0-9\-]+)\]")
COCOTB_LINE = re.compile(r"^(PASS|FAIL)\s+(test_\S+_cocotb)\s+(.*)$")

HOST, COCOTB, FORMAL, LINT = "host-tests", "cocotb-regression", "formal-check", "rtl-lint"


@dataclass
class Requirement:
    tag: str
    spec: str
    skills: list[str] = field(default_factory=list)
    tests: dict[str, list[str]] = field(default_factory=dict)     # skill -> test files with @verifies


@dataclass
class FileResult:
    result: str            # pass / fail / skip
    passed: int = 0
    failed: int = 0
    detail: str = ""


# ---------------------------------------------------------------- what the specs ask for
def parse_requirements(root: Path) -> dict[str, Requirement]:
    """Requirement tags and their `Verified by` skills (same parsing rules as AbstractX's matrix:
    the first line of `Verified by`, parenthesised notes removed, backticked names)."""
    reqs = {}
    for spec in sorted(root.glob(SPEC_GLOB)):
        cur = None
        for raw in spec.read_text(encoding="utf-8").splitlines():
            m = SPEC_RE.match(raw.strip())
            if m or (cur and HEADING_RE.match(raw)):
                cur = None
            if m:
                cur = reqs[m.group(1)] = Requirement(m.group(1), str(spec.relative_to(root)))
            if cur:
                v = VERIFIED_RE.match(raw)
                if v:
                    names = SKILL_TOKEN_RE.findall(re.sub(r"\([^)]*\)", "", v.group(1)))
                    cur.skills += [s for s in names if s not in cur.skills]
    return reqs


def skill_of(test_file: str) -> str | None:
    p = Path(test_file)
    if p.parts[:2] == ("tests", "cocotb") and p.name.endswith("_cocotb.py"):
        return COCOTB
    if p.parts[:2] == ("tests", "formal"):
        return FORMAL
    if p.parts[0] == "tests" and p.suffix == ".py" and p.name.startswith("test_"):
        return HOST
    return None


def scan_verifies(root: Path) -> dict[str, list[str]]:
    """test file -> tags it verifies (`@verifies [SPEC-*]` lines)."""
    out = {}
    for p in sorted((root / "tests").rglob("*")):
        if not p.is_file() or p.suffix not in (".py", ".sv"):
            continue
        tags = []
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "@verifies" in line:
                tags += [t for t in TAG_RE.findall(line) if t not in tags]
        if tags:
            out[str(p.relative_to(root))] = tags
    return out


def attach_tests(reqs, verifies):
    for f, tags in verifies.items():
        sk = skill_of(f)
        for t in tags:
            if t in reqs and sk:
                reqs[t].tests.setdefault(sk, []).append(f)


# ---------------------------------------------------------------- running the checks
def tool_env():
    oss = Path(os.environ.get("PIO2_TOOLS", Path.home() / ".tools")) / "oss-cad-suite" / "bin"
    venv_bin = Path(sys.executable).parent
    return dict(os.environ, PATH=os.pathsep.join([str(venv_bin), os.environ.get("PATH", ""), str(oss)]))


def run_pytest(root: Path, files: list[str]) -> dict[str, FileResult]:
    """pytest on the given files; per file: pass (>= 1 passed, none failed), fail, or skip."""
    with tempfile.TemporaryDirectory(prefix="pio2-trac-") as tmp:
        xml = Path(tmp) / "junit.xml"
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "junit_family=xunit1",
               f"--junitxml={xml}", *files]
        proc = subprocess.run(cmd, cwd=root, env=tool_env(), capture_output=True, text=True)
        print(proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else proc.stderr[-2000:])
        res = {f: FileResult("skip", detail="not collected") for f in files}
        if not xml.exists():
            for f in files:
                res[f] = FileResult("fail", detail="pytest wrote no results")
            return res
        for case in ET.parse(xml).getroot().iter("testcase"):
            f = case.get("file")
            if f not in res:
                continue
            r = res[f]
            if case.find("failure") is not None or case.find("error") is not None:
                r.failed += 1
            elif case.find("skipped") is None:
                r.passed += 1
        for f, r in res.items():
            if r.failed:
                r.result, r.detail = "fail", f"{r.failed} failed, {r.passed} passed"
            elif r.passed:
                r.result, r.detail = "pass", f"{r.passed} passed"
            elif r.detail != "not collected":
                r.detail = "all skipped"
        if proc.returncode not in (0, 1):                  # 1 = tests failed; anything else: did not run
            for r in res.values():
                if r.result == "pass":
                    r.result, r.detail = "fail", f"pytest exit {proc.returncode}"
        return res


def run_cocotb(root: Path, files: list[str]) -> dict[str, FileResult]:
    proc = subprocess.run([sys.executable, "-m", "pio2.run_cocotb"], cwd=root, env=tool_env(),
                          capture_output=True, text=True)
    lines = {}
    for line in proc.stdout.splitlines():
        m = COCOTB_LINE.match(line)
        if m:
            lines[m.group(2)] = (m.group(1), m.group(3))
    print(proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else proc.stderr[-2000:])
    res = {}
    for f in files:
        st, detail = lines.get(Path(f).stem, ("FAIL", "suite did not report"))
        m = re.search(r"PASS=(\d+)\s+FAIL=(\d+)", detail)
        res[f] = FileResult("pass" if st == "PASS" else "fail", int(m.group(1)) if m else 0,
                            int(m.group(2)) if m else 0, detail)
    return res


def run_formal(root: Path, files: list[str]) -> dict[str, FileResult]:
    """A formal file (tests/formal/<block>.sv with @verifies) passes when `sby -f <block>.sby bmc` passes."""
    env = tool_env()
    sby = shutil.which("sby", path=env["PATH"])
    res = {}
    for f in files:
        job = (root / f).with_suffix(".sby")
        if not sby or not job.exists():
            res[f] = FileResult("skip", detail="no sby" if not sby else f"no {job.name}")
            continue
        with tempfile.TemporaryDirectory(prefix="pio2-sby-") as tmp:     # work dir outside the repo
            proc = subprocess.run([sby, "-f", "-d", str(Path(tmp) / "run"), job.name, "bmc"], cwd=job.parent,
                                  env=env, capture_output=True, text=True)
        ok = proc.returncode == 0 and "DONE (PASS" in proc.stdout
        tail = [ln for ln in proc.stdout.splitlines() if "DONE" in ln or "summary" in ln][-1:]
        res[f] = FileResult("pass" if ok else "fail", int(ok), int(not ok), " ".join(tail))
    return res


LINT_DIRS = ["src/rtl", "src/rtl/trace", "third_party/verilog-axis/rtl"]


def run_lint(root: Path) -> FileResult:
    """The rtl-lint skill: verilator --lint-only with each module under src/rtl as top (other modules
    found through -y: src/rtl, src/rtl/trace, Forencich verilog-axis); fail on MULTIDRIVEN,
    SYNCASYNCNET, CASEINCOMPLETE or any error."""
    env = tool_env()
    vl = shutil.which("verilator", path=env["PATH"])
    if not vl:
        return FileResult("skip", detail="no verilator")
    srcs = sorted(str(p.relative_to(root)) for p in (root / "src" / "rtl").rglob("*.sv"))
    bad = []
    for s in srcs:
        top = Path(s).stem
        ydirs = [a for d in LINT_DIRS for a in ("-y", d)]
        proc = subprocess.run([vl, "--lint-only", "-Wall", "-Wno-DECLFILENAME", "-Wno-UNUSEDSIGNAL",
                               "-Wno-UNUSEDPARAM", "-Wno-PINCONNECTEMPTY", "-Wno-WIDTHTRUNC", "-Wno-WIDTHEXPAND",
                               "-Wno-fatal", *ydirs, s, "--top-module", top],
                              cwd=root, env=env, capture_output=True, text=True)
        out = proc.stdout + proc.stderr
        hits = re.findall(r"%Warning-(MULTIDRIVEN|SYNCASYNCNET|CASEINCOMPLETE)", out)
        errors = re.findall(r"^%Error[^:]*: .*$", out, re.M)
        if hits or errors:
            bad.append(f"{top}: {', '.join(sorted(set(hits)))} {errors[0] if errors else ''}".strip())
    return FileResult("fail" if bad else "pass", len(srcs) - len(bad), len(bad),
                      "; ".join(bad) or f"{len(srcs)} modules clean")


# ---------------------------------------------------------------- judging and recording
def judge(req: Requirement, results: dict[str, FileResult], lint: FileResult | None):
    """(result, notes, metrics) for one requirement."""
    notes, missing = [], []
    passed = failed = 0
    failing = False
    for sk in req.skills:
        if sk == LINT:
            ran = [("rtl-lint", lint)] if lint and lint.result != "skip" else []
        else:
            ran = [(f, results[f]) for f in req.tests.get(sk, []) if f in results and results[f].result != "skip"]
        if not ran:
            missing.append(sk)
            continue
        for f, r in ran:
            passed += r.passed
            failed += r.failed
            notes.append(f"{f}: {r.result} ({r.detail})")
            failing |= r.result == "fail"
    # a failing test file that is not on the Verified by line still fails the requirement
    for sk, files in req.tests.items():
        if sk not in req.skills:
            for f in files:
                if f in results and results[f].result == "fail":
                    failing = True
                    notes.append(f"{f}: fail ({results[f].detail}; skill {sk} not in Verified by)")
    if missing:
        notes.append("not run: " + ", ".join(missing))
    result = "fail" if failing else "skip" if missing or not req.skills else "pass"
    return result, "; ".join(notes), {"tests_passed": passed, "tests_failed": failed}


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True).stdout


def dirty_paths(root: Path) -> list[str]:
    out = git(root, "status", "--porcelain", "--ignore-submodules=all", "--untracked-files=normal")
    paths = [ln[3:].strip() for ln in out.splitlines() if len(ln) > 3]
    return [p for p in paths if not p.startswith(str(LEDGER_DIR))]


def make_record(root: Path, tag: str, result: str, metrics: dict, notes: str, source: str, duration: float):
    commit = git(root, "rev-parse", "--short=12", "HEAD").strip() or "unknown"
    return {
        "time": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commit": commit, "dirty": False, "host": socket.gethostname(),
        "family": "test", "check": f"pio2-trac:{tag}", "requirements": [tag],
        "result": result, "metrics": metrics, "delta": {}, "duration_s": round(duration, 3),
        "tool": "pio2-trac", "notes": notes, "source": source,
    }


def read_ledger(root: Path) -> list[dict]:
    records = []
    for path in sorted((root / LEDGER_DIR).glob("*/*.jsonl")):
        records += [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
    return sorted(records, key=lambda r: r["time"])


def append(root: Path, records: list[dict]) -> Path:
    prev = {}
    for r in read_ledger(root):
        prev[r["check"]] = r
    path = root / LEDGER_DIR / records[0]["time"][:7] / f"{records[0]['host']}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for rec in records:
            p = prev.get(rec["check"])
            if p:
                rec["delta"] = {k: v - p["metrics"][k] for k, v in rec["metrics"].items()
                                if isinstance(p["metrics"].get(k), (int, float))}
            f.write(json.dumps(rec, sort_keys=True) + "\n")
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--record", action="store_true", help="append evidence records (needs a clean tree)")
    ap.add_argument("--no-cocotb", action="store_true", help="skip the cocotb suites")
    ap.add_argument("--formal", action="store_true", help="run sby bmc for each tests/formal/<block>.sby")
    ap.add_argument("--lint", action="store_true", help="run the rtl-lint skill")
    ap.add_argument("--root", type=Path, default=ROOT)
    a = ap.parse_args(argv)
    root = a.root.resolve()
    if a.record:
        dirty = dirty_paths(root)
        if dirty:
            print("refusing to record: the tree has changes, the record would name a commit that is not "
                  "what ran:\n  " + "\n  ".join(dirty[:20]), file=sys.stderr)
            return 2
    reqs = parse_requirements(root)
    verifies = scan_verifies(root)
    attach_tests(reqs, verifies)
    by_skill = {}
    for f in verifies:
        by_skill.setdefault(skill_of(f), []).append(f)

    start = time.monotonic()
    results: dict[str, FileResult] = {}
    if by_skill.get(HOST):
        results.update(run_pytest(root, sorted(by_skill[HOST])))
    if by_skill.get(COCOTB) and not a.no_cocotb:
        results.update(run_cocotb(root, sorted(by_skill[COCOTB])))
    if by_skill.get(FORMAL) and a.formal:
        results.update(run_formal(root, sorted(by_skill[FORMAL])))
    lint = run_lint(root) if a.lint else None
    duration = time.monotonic() - start

    print(f"\n{'requirement':16s} {'result':6s} notes")
    records, counts = [], {"pass": 0, "fail": 0, "skip": 0}
    for tag in sorted(reqs):
        req = reqs[tag]
        if not req.tests and not (lint and LINT in req.skills):
            continue                                       # no check of it ran: no record
        result, notes, metrics = judge(req, results, lint)
        counts[result] += 1
        print(f"{tag:16s} {result:6s} {notes}")
        source = "sim" if req.tests.get(COCOTB) or req.tests.get(FORMAL) else "host"
        records.append(make_record(root, tag, result, metrics, notes, source, duration))
    print(f"\n{counts['pass']} pass, {counts['fail']} fail, {counts['skip']} not fully checked "
          f"({len(reqs)} requirements, {len(records)} with checks)")
    if a.record and records:
        print(f"recorded {len(records)} records in {append(root, records).relative_to(root)}")
    return 1 if counts["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
