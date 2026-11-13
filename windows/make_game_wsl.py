#!/usr/bin/env python3
"""
The WSL half of the Windows pack build (install_mario64mr.py --pack-in-wsl).

MSYS2's sound tools don't run on some Windows setups (they open the .aiff and never write), so the pack is built here
instead, by Linux gcc, and copied back:

  python3 make_game_wsl.py --copy <source> <destination>   the files the pack build needs, out of the Windows work
                                                           folder (install_mario64mr.py's pack_files), into a Linux
                                                           tree. <source> may be a \\\\wsl$\\ path: WSL reads it
                                                           directly, and nothing is copied through a path WSL
                                                           can't see.
  python3 make_game_wsl.py --pack <build_dir>              the pack's own targets ("PACK BUILT")

The targets are the same list install_mario64mr.py's build_pack() asks make for ("PACK TARGETS" below is compared with
it by the Windows run), skyboxes included: without them base.zip has no skies and nothing complains.
"""
import os
import subprocess
import sys
from pathlib import Path

# (the pack build's targets, as build_pack() in install_mario64mr.py asks for them: sound, every skybox, and the cake)
PACK_TARGETS = ("sound/sound_data.ctl", "sound/sequences.bin", "bin/*_skybox.c", "levels/ending/cake.inc.c")


def jobs():
    return str(max(4, 2 * (os.cpu_count() or 4)))


def run(cmd, cwd):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], cwd=cwd, check=True)


def copy_tree(source, destination):
    """The pack build's sources, from a Windows work folder (a Windows path, or \\\\wsl$\\...) to a Linux tree. Only what
    the build needs: the work folder holds gigabytes of ROM assets and object files."""
    source, destination = Path(source), Path(destination)
    if not source.is_dir():
        sys.exit(f"{source}: not a folder (from Windows, pass it as \\\\wsl$\\<distro>\\home\\... or \\\\wsl.localhost\\...)")
    destination.mkdir(parents=True, exist_ok=True)
    for rel in PACK_SOURCES:
        src, dst = source / rel, destination / rel
        if src.is_dir():
            for path in src.rglob("*"):
                if path.is_file():
                    target = dst / path.relative_to(src)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(path.read_bytes())  # (not copy2: a \\wsl$ file's mode is Windows')
        elif src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
    return destination


PACK_SOURCES = ("sound", "assets", "levels/bin", "build/us_pc/bin", "Makefile", "Makefile.split", "include", "src",
                "enhancements", "rsp", "textures", "tools", "charmap.txt", "charmap_menu.txt")


def host_tools(tree):
    """tools/, built by this machine's compiler: make passes its CC down to them, and they have to run here."""
    # (tabledesign's library is binned by make -C tools clean, and make -j alone would race past its library)
    if not (tree / "tools/tabledesign").exists():
        run(["make", "-C", "tools/audiofile", "-j" + jobs()], tree)
        run(["make", "-C", "tools", "-j" + jobs()], tree)


def skyboxes(tree, build):
    """Every skybox the pack has, as build_pack() asks for them: from the extracted textures, so the list is the same."""
    return sorted("bin/%s_skybox.c" % p.stem for p in (tree / "textures/skyboxes").glob("*.png"))


def make_pack(tree, build):
    host_tools(tree)
    # (what the build needs to know of the machine, from the Makefile's own compiler probe: no sound tools needed)
    run(["make", "-j1", "HOST_OS=Linux", "OSX_BUILD=0", "CC=cc", "CXX=c++", "CPP=cc -E -P -x c", "VERSION=us",
         "BUILD_DIR=" + build, "NOEXTRACT=1", build + "/endian-and-bitwidth"], tree)
    targets = [build + "/sound/sound_data.ctl", build + "/sound/sequences.bin",
               build + "/levels/ending/cake.inc.c"] + [build + "/" + s for s in skyboxes(tree, build)]
    print("%d skyboxes: %s" % (len(targets) - 3, ", ".join(skyboxes(tree, build))), flush=True)
    run(["make", "-j" + jobs(), "VERSION=us", "EXTERNAL_DATA=1", "BUILD_DIR=" + build, "NOEXTRACT=1",
         "HOST_OS=Linux", "OSX_BUILD=0", "CC=cc", "CXX=c++", "CPP=cc -E -P -x c"] + targets, tree)
    missing = [t for t in targets if not (tree / t).exists()]
    if missing:
        sys.exit("the pack build finished but did not make: " + ", ".join(missing))
    print("PACK BUILT: " + build)


def main():
    args = sys.argv[1:]
    if args[:1] == ["--copy"]:
        if len(args) != 3:
            sys.exit("usage: make_game_wsl.py --copy <windows work folder> <linux folder>")
        print("copied into " + str(copy_tree(args[1], os.path.expanduser(args[2]))))
    elif args[:1] == ["--pack"]:
        if len(args) < 2:
            sys.exit("usage: make_game_wsl.py --pack <build directory>")
        # (the build directory is relative to the tree it was copied into, as make takes it)
        tree = Path(os.getcwd())
        make_pack(tree, os.path.expanduser(args[1]))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
