#!/usr/bin/env python3
"""
The WSL half of the Windows build. WSL is a host install_mario64mr.py didn't have: Linux, with the Windows NDK
(clang.exe) — called the way MSYS2 has to call it, with --target= and Windows paths. MSYS2's sound tools don't run
there (they open the .aiff and never write), so both halves are made here instead:

  python3 make_game_wsl.py --copy <plan.json>          the few files the pack build needs (install_mario64mr.py's
                                                       plan_wsl_pack), copied out of the MSYS2 work folder
  python3 make_game_wsl.py --pack <plan.json>          ...plus the tools, and the pack ("PACK BUILT")
  python3 make_game_wsl.py --game <plan.json>          the three libraries for the headset ("BUILT ..."), from the
                                                       sources install_mario64mr.py fetched: plan's work, ndk, out, rom
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "native/fullgame"))
import install_mario64mr as m  # noqa: E402


def jobs():
    return str(max(4, 2 * (os.cpu_count() or 4)))


def run(cmd, cwd):
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=cwd, check=True)


def copy_plan(plan):
    tree, into = plan["source"], os.path.expanduser(plan["destination"])
    os.makedirs(into, exist_ok=True)
    for rel in plan["copy"]:
        src, dst = os.path.join(tree, rel), os.path.join(into, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
    return into


def host_tools(tree):
    """tools/ built by this machine's compiler: make passes its CC down to them, and they have to run here."""
    # (tabledesign's library is binned by make -C tools clean, and make -j alone would race past it)
    done = tree / "tools/tabledesign"
    if not done.exists():
        run(["make", "-C", "tools/audiofile", "-j" + jobs()], tree)
        run(["make", "-C", "tools", "-j" + jobs()], tree)


def make_pack(plan):
    tree = copy_plan(plan)
    build = plan["build_dir"]
    host_tools(tree)
    # (what the build needs to know of the machine, from the Makefile's own compiler probe: no sound tools needed)
    run(["make", "-j1", "HOST_OS=Linux", "OSX_BUILD=0", "CC=cc", "CXX=c++", "CPP=cc -E -P -x c", "VERSION=us",
         "BUILD_DIR=" + build, "NOEXTRACT=1", build + "/endian-and-bitwidth"], tree)
    targets = [build + "/sound/sound_data.ctl", build + "/sound/sequences.bin",
               build + "/levels/ending/cake.inc.c"]
    run(["make", "-j" + jobs(), "VERSION=us", "EXTERNAL_DATA=1", "BUILD_DIR=" + build, "NOEXTRACT=1",
         "HOST_OS=Linux", "OSX_BUILD=0", "CC=cc", "CXX=c++", "CPP=cc -E -P -x c"] + targets, tree)
    print("PACK BUILT: " + os.path.join(tree, build))


def wsl_native(path):
    """A path as the Windows NDK's programs take it."""
    return subprocess.check_output(["wslpath", "-w", str(path)], text=True).strip()


def wsl_compiler(ndk, cxx=False):
    """clang.exe with --target=..., as under MSYS2 (a Windows NDK has no aarch64-linux-android29-clang for a Linux host)."""
    bin_dir = Path(ndk) / "toolchains/llvm/prebuilt/windows-x86_64/bin"
    return [str(bin_dir / ("clang++.exe" if cxx else "clang.exe")), "--target=" + m.ANDROID_TARGET]


class ndk_host:
    """MSYS's host rules for the Windows NDK, for the duration of a `with` block."""

    def __enter__(self):
        self.saved = (m.MSYS, m.native, m.compiler)
        m.MSYS, m.native, m.compiler = True, wsl_native, wsl_compiler
        os.environ.setdefault("MSYS2_ARG_CONV_EXCL", "*")

    def __exit__(self, *exc):
        m.MSYS, m.native, m.compiler = self.saved


def make_game(plan):
    work, out, ndk = Path(plan["work"]), Path(plan["out"]), Path(plan["ndk"])
    (out / "native").mkdir(parents=True, exist_ok=True)
    host_tools(work / "sm64ex")
    with ndk_host():
        m.build_game(work / "sm64ex", Path(plan["rom"]).read_bytes(), ndk, out / "native")
        m.build_goddard(work / "sm64-decomp", work / "sm64ex", ndk, work / "goddard", out / "native")
        m.build_libsm64(work / "libsm64", ndk, out / "native")
    for name in m.LIBRARIES:
        print("BUILT %s: %.1f MB" % (name, (out / "native" / name).stat().st_size / 1e6))


def main():
    with open(sys.argv[-1]) as f:
        plan = json.load(f)
    if "--game" in sys.argv:
        make_game(plan)
    elif "--pack" in sys.argv:
        make_pack(plan)
    else:
        print("copied into " + copy_plan(plan))


if __name__ == "__main__":
    main()
