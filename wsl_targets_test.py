#!/usr/bin/env python3
"""What the Windows run and the WSL script agree on: the pack's targets, and that a Windows path becomes WSL's own
(wslpath -u) and not a \\wsl$ path, which WSL can't open. Run from MSYS2 (Windows), where install_mario64mr.py runs:
the target check passes anywhere, the wslpath check needs wsl.exe."""
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    installer = load("install_mario64mr", HERE / "install_mario64mr.py")
    wsl_script = load("make_game_wsl", HERE / "windows/make_game_wsl.py")
    problems = []

    # 1. The targets: install_mario64mr.py's own list, against the WSL script's same rule (its skybox glob and the
    #    three targets it asks make for). They are the same list, in the same build folder.
    with tempfile.TemporaryDirectory() as scratch:
        work = Path(scratch) / "sm64ex"
        (work / "textures/skyboxes").mkdir(parents=True)
        for stem in ("bbh", "bidw", "bitdw"):
            (work / f"textures/skyboxes/{stem}.png").write_bytes(b"")
        expected = set(installer.pack_targets(work))
        seen = {f"{installer.PACK_BUILD}/{rel}" for rel in wsl_script.skyboxes(work)}
        seen |= {f"{installer.PACK_BUILD}/sound/{name}" for name in ("sound_data.ctl", "sequences.bin")}
        seen.add(f"{installer.PACK_BUILD}/levels/ending/cake.inc.c")
    if expected != seen:
        problems.append(f"the two target lists differ: only in install_mario64mr.py {sorted(expected - seen)}, "
                        f"only in make_game_wsl.py {sorted(seen - expected)}")

    # 2. A Windows path, as the installer turns it into one WSL opens: its own (/mnt/c/...), never \\wsl$.
    if not installer.MSYS:
        print("(not the Windows run: the wslpath check needs MSYS2, the target check above is done)")
        return problems
    for probe in (work, HERE / "windows/make_game_wsl.py"):
        windows = subprocess.check_output(["cygpath", "-w", str(probe)], text=True).strip()
        there = subprocess.check_output(["wsl.exe", "-e", "wslpath", "-u", windows], text=True).strip()
        print(f"{windows} -> {there}")
        if not there.startswith("/"):
            problems.append(f"wslpath -u {windows}: {there!r} is not WSL's own path")
        elif "wsl$" in there.lower():
            problems.append(f"wslpath -u {windows}: {there!r} is a \\wsl$ path (Windows opens those, WSL doesn't)")
    return problems


if __name__ == "__main__":
    found = main() or []
    for problem in found:
        print("FAIL: " + problem)
    print("FAILED" if found else "OK")
    sys.exit(1 if found else 0)
