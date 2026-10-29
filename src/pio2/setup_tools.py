# Copyright (C) 2026 Tim Michals
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Native tools pio2 needs that are not Python packages (uv cannot install them).

  uv run pio2-setup-tools --check            # what is installed, and the versions
  uv run pio2-setup-tools --oss-cad-suite    # pinned YosysHQ release -> ~/.tools/oss-cad-suite
  uv run pio2-setup-tools --pioasm           # build pioasm from a pinned pico-sdk tag -> ~/.tools/pioasm
  uv run pio2-setup-tools --zipcpu-props     # ZipCPU wb2axip formal properties -> ~/.tools/zipcpu-wb2axip

Downloads are pinned and checked (SHA-256 for the oss-cad-suite archive); nothing is piped into a
shell. Existing installs are left alone unless --force is given.
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

TOOLS = Path(os.environ.get("PIO2_TOOLS", Path.home() / ".tools"))

# Pinned oss-cad-suite release (https://github.com/YosysHQ/oss-cad-suite-build/releases).
OSS_RELEASE = "2026-10-29"
OSS_ASSET = "oss-cad-suite-linux-x64-20261029.tgz"
OSS_SHA256 = "ae6f7f11eeaf7c504762f1b812d28654a61cbb965f76ecda7aeb8306b1d015eb"

# Pinned pico-sdk tag for pioasm (https://github.com/raspberrypi/pico-sdk).
PICO_SDK_TAG = "2.2.0"

# ZipCPU formal property files (Dan Gisselquist, https://github.com/ZipCPU/wb2axip, bench/formal:
# faxil_slave.v, faxil_master.v, faxis_master.v, faxis_slave.v) for [SPEC-VER-01]. The commit used is
# recorded in tests/formal/zipcpu.lock on the first fetch and reused afterwards; check the repository's
# license file before use (it is copied next to the properties).
ZIPCPU_REPO = "https://github.com/ZipCPU/wb2axip.git"
ZIPCPU_LOCK = Path(__file__).resolve().parents[2] / "tests" / "formal" / "zipcpu.lock"
ZIPCPU_FILES = ("faxil_slave.v", "faxil_master.v", "faxis_master.v", "faxis_slave.v")


def run(cmd, **kw):
    print("+", " ".join(str(c) for c in cmd))
    subprocess.run(cmd, check=True, **kw)


def which_version(name, args=("--version",)):
    path = shutil.which(name, path=os.pathsep.join([str(TOOLS / "oss-cad-suite" / "bin"),
                                                     str(TOOLS / "pioasm"), os.environ.get("PATH", "")]))
    if not path:
        return None, None
    try:
        out = subprocess.run([path, *args], capture_output=True, text=True, timeout=20)
        text = (out.stdout or out.stderr).strip().splitlines()
        return path, text[0] if text else "?"
    except (OSError, subprocess.TimeoutExpired):
        return path, "?"


def check():
    ok = True
    for name, args, need in (("verilator", ("--version",), True), ("iverilog", ("-V",), False),
                             ("pioasm", ("--version",), False), ("make", ("--version",), True),
                             ("g++", ("--version",), True)):
        path, ver = which_version(name, args)
        print(f"  {'ok  ' if path else ('MISS' if need else 'opt ')} {name:10s} {ver or '-':50.50s} {path or ''}")
        ok = ok and (path is not None or not need)
    zp = TOOLS / "zipcpu-wb2axip" / "COMMIT"
    print(f"  zipcpu props : {zp.read_text().strip() if zp.exists() else 'not fetched (--zipcpu-props)'}")
    oss = TOOLS / "oss-cad-suite" / "VERSION"
    print(f"  oss-cad-suite: {oss.read_text().strip() if oss.exists() else 'not in ' + str(TOOLS)}")
    return 0 if ok else 1


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def oss_cad_suite(force):
    dest = TOOLS / "oss-cad-suite"
    if dest.exists() and not force:
        print(f"{dest} exists (use --force to replace)")
        return 0
    url = f"https://github.com/YosysHQ/oss-cad-suite-build/releases/download/{OSS_RELEASE}/{OSS_ASSET}"
    TOOLS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=TOOLS) as tmp:
        archive = Path(tmp) / OSS_ASSET
        print(f"downloading {url} (~750 MB)")
        urllib.request.urlretrieve(url, archive)
        got = sha256(archive)
        if got != OSS_SHA256:
            print(f"SHA-256 mismatch: {got} != {OSS_SHA256}", file=sys.stderr)
            return 1
        with tarfile.open(archive) as t:
            t.extractall(tmp, filter="data")
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(Path(tmp) / "oss-cad-suite"), dest)
    print(f"installed {dest}; add {dest / 'bin'} to PATH (or source {dest / 'environment'})")
    return 0


def pioasm(force):
    dest = TOOLS / "pioasm" / "pioasm"
    if dest.exists() and not force:
        print(f"{dest} exists (use --force to rebuild)")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        sdk = Path(tmp) / "pico-sdk"
        run(["git", "clone", "--depth", "1", "--branch", PICO_SDK_TAG,
             "https://github.com/raspberrypi/pico-sdk.git", sdk])
        build = Path(tmp) / "build"
        run(["cmake", "-S", sdk / "tools" / "pioasm", "-B", build, "-DCMAKE_BUILD_TYPE=Release"])
        run(["cmake", "--build", build, "-j"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(build / "pioasm", dest)
    print(f"installed {dest}")
    return 0


def zipcpu_props(force):
    dest = TOOLS / "zipcpu-wb2axip"
    if dest.exists() and not force:
        print(f"{dest} exists (use --force to refetch)")
        return 0
    pinned = ZIPCPU_LOCK.read_text().strip() if ZIPCPU_LOCK.exists() else ""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "wb2axip"
        run(["git", "clone", "--quiet", ZIPCPU_REPO, repo])
        if pinned:
            run(["git", "-C", repo, "checkout", "--quiet", pinned])
        commit = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True,
                                check=True).stdout.strip()
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        missing = []
        for name in ZIPCPU_FILES:
            src = repo / "bench" / "formal" / name
            if src.exists():
                shutil.copy2(src, dest / name)
            else:
                missing.append(name)
        for lic in ("LICENSE", "LICENSE.txt", "COPYING", "doc/apache-2.0.txt"):
            if (repo / lic).exists():
                shutil.copy2(repo / lic, dest / Path(lic).name)
        (dest / "COMMIT").write_text(commit + "\n")
    if not pinned:
        ZIPCPU_LOCK.parent.mkdir(parents=True, exist_ok=True)
        ZIPCPU_LOCK.write_text(commit + "\n")
        print(f"pinned {commit} in {ZIPCPU_LOCK} (commit it)")
    if missing:
        print(f"not found in bench/formal: {missing}", file=sys.stderr)
        return 1
    print(f"installed {dest} at {commit}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--check", action="store_true", help="report installed tools (default)")
    ap.add_argument("--oss-cad-suite", action="store_true", help=f"install oss-cad-suite {OSS_RELEASE}")
    ap.add_argument("--pioasm", action="store_true", help=f"build pioasm from pico-sdk {PICO_SDK_TAG}")
    ap.add_argument("--zipcpu-props", action="store_true", help="fetch ZipCPU AXI formal property files")
    ap.add_argument("--force", action="store_true", help="replace an existing install")
    a = ap.parse_args()
    rc = 0
    if a.oss_cad_suite:
        rc |= oss_cad_suite(a.force)
    if a.pioasm:
        rc |= pioasm(a.force)
    if a.zipcpu_props:
        rc |= zipcpu_props(a.force)
    if a.check or not (a.oss_cad_suite or a.pioasm or a.zipcpu_props):
        rc |= check()
    return rc


if __name__ == "__main__":
    sys.exit(main())
