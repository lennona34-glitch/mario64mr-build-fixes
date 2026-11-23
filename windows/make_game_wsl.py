#!/usr/bin/env python3
"""
The WSL half of the Windows pack build (install_mario64mr.py --pack-in-wsl).

MSYS2's sound tools don't run on some Windows setups (they open the .aiff and never write), so the pack is built here
instead, by Linux gcc, and copied back:

  python3 make_game_wsl.py --copy <windows work folder> <linux tree>   the files the pack build needs, out of the
                                                                      Windows work folder (PACK_SOURCES below),
                                                                      into a Linux tree. The work folder is given
                                                                      as this side's own path for it (/mnt/c/...:
                                                                      install_mario64mr.py runs wslpath -u), and
                                                                      nothing is copied through a path either side
                                                                      can't open.
  python3 make_game_wsl.py --pack <build dir>                          the pack's own targets ("PACK BUILT")
  python3 make_game_wsl.py --winpath <windows work folder> <build dir> the build dir copied back into that Windows
                                                                      work folder ("PACK BACK")

The targets are the same list install_mario64mr.py's pack_targets() asks make for: the sound banks, every skybox cut
into tiles, and the cake. Skyboxes included: without them base.zip has no skies and nothing complains.
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# (the pack build's sources: the work folder holds gigabytes of ROM assets and object files. levels/ with the levels'
#  rules but not their bin/: a build rule needs levels/level_rules.mk, and make reads bin/ for the ROM's own assets)
PACK_SOURCES = ("sound", "assets", "levels", "build/us_pc/bin", "Makefile", "Makefile.split", "include", "src",
                "enhancements", "rsp", "textures", "tools", "charmap.txt", "charmap_menu.txt",
                # (extract_assets.py's signature, which its .assets-local.txt marks as finished)
                ".assets-local.txt")
# (a record of what was copied, kept in the tree: a run started again copies only what changed since)
VIA = ".via-wsl"


def jobs():
    return str(max(4, 2 * (os.cpu_count() or 4)))


def run(cmd, cwd):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], cwd=cwd, check=True)


def copy_tree(source, destination):
    """The pack build's sources, from its Windows work folder into the Linux tree: only what is missing or has changed
    since the last copy (reading a file on a Windows drive costs far more than writing it here, and the first copy is
    tens of thousands of files)."""
    source, destination = Path(source), Path(destination).expanduser()
    if not source.is_dir():
        sys.exit(f"{source}: not a folder. install_mario64mr.py passes this as wslpath -u of the Windows work folder, "
                 f"which is /mnt/c/...: a \\\\wsl$ path is one Windows opens, not this side.")
    records = destination / VIA
    records.mkdir(parents=True, exist_ok=True)
    copied = 0
    for rel in PACK_SOURCES:
        src, dst = source / rel, destination / rel
        if src.is_dir():
            pairs = [(path, dst / path.relative_to(src)) for path in src.rglob("*")
                     if path.is_file() and path.name != VIA]
        elif src.is_file():
            pairs = [(src, dst)]
        else:
            continue
        if rel == "textures":  # (the skyboxes are cut into tiles, and nothing else under textures is read)
            pairs = [(path, target) for path, target in pairs if path.relative_to(src).parts[0] == "skyboxes"]
        elif rel == "levels":  # (the rules make reads (level_rules.mk, level_defines.h), not the levels' bin/)
            pairs = [(path, target) for path, target in pairs
                     if len(path.relative_to(src).parts) == 1 or path.relative_to(src).parts[0] != "bin"]
        for path, target in pairs:
            status, size = path.stat().st_mtime_ns, path.stat().st_size
            record = records / target.relative_to(destination)
            if record.is_file() and record.read_text() == f"{status} {size}":
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)  # (not copy2: a file on a Windows drive's mode is Windows')
            record.parent.mkdir(parents=True, exist_ok=True)
            record.write_text(f"{status} {size}")
            copied += 1
    return copied


def host_tools(tree):
    """tools/, built by this machine's compiler: make passes its CC down to them, and they have to run here."""
    # (tabledesign's library is binned by make -C tools clean, and make -j alone would race past its library)
    if not (tree / "tools/tabledesign").exists():
        run(["make", "-C", "tools/audiofile", "-j" + jobs()], tree)
        run(["make", "-C", "tools", "-j" + jobs()], tree)


def skyboxes(tree):
    """Every skybox the pack has, as pack_targets() asks for them: from the extracted textures, so the list is the same."""
    return sorted(f"bin/{p.stem}_skybox.c" for p in (tree / "textures/skyboxes").glob("*.png"))


def make_pack(tree, build):
    host_tools(tree)
    # (what the build needs to know of the machine, from the Makefile's own compiler probe: no sound tools needed)
    run(["make", "-j1", "HOST_OS=Linux", "OSX_BUILD=0", "CC=cc", "CXX=c++", "CPP=cc -E -P -x c", "VERSION=us",
         "BUILD_DIR=" + build, "NOEXTRACT=1", f"{build}/endian-and-bitwidth"], tree)
    skies = skyboxes(tree)
    print(f"{len(skies)} skyboxes: " + (", ".join(skies) or "none"), flush=True)
    targets = [f"{build}/sound/sound_data.ctl", f"{build}/sound/sequences.bin",
               f"{build}/levels/ending/cake.inc.c"] + [f"{build}/{s}" for s in skies]
    run(["make", "-j" + jobs(), "VERSION=us", "EXTERNAL_DATA=1", "BUILD_DIR=" + build, "NOEXTRACT=1",
         "HOST_OS=Linux", "OSX_BUILD=0", "CC=cc", "CXX=c++", "CPP=cc -E -P -x c"] + targets, tree)
    missing = [t for t in targets if not (tree / t).exists()]
    if missing:
        sys.exit("the pack build finished but did not make: " + ", ".join(missing))
    # (what pack() zips with them, too: assemble_sound.py writes sound_data.tbl and bank_sets beside the .ctl)
    for name in ("sound/sound_data.tbl", "sound/bank_sets"):
        if not (tree / build / name).exists():
            sys.exit(f"the pack build finished but did not make: {build}/{name}")
    tiles = tree / build / "textures/skybox_tiles"
    print(f"PACK BUILT: {build} ({len(list(tiles.rglob('*.png')))} skybox tiles)")


def copy_back(tree, build, windows_work):
    """The results, into the Windows work folder the copy came from: install_mario64mr.py passes it as wslpath -u of
    where they go (/mnt/c/...), and this side writes there as it writes anywhere."""
    target = Path(windows_work).expanduser() / build
    if not Path(windows_work).expanduser().is_dir():
        sys.exit(f"{windows_work}: not a folder. install_mario64mr.py passes wslpath -u of the Windows work folder, "
                 f"which is /mnt/c/...: a \\\\wsl$ path is one Windows opens, not this side.")
    source = tree / build
    for path in source.rglob("*"):
        if path.is_file():
            destination = target / path.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
    sequences = target / "sound/sequences.bin"
    if not sequences.is_file():
        sys.exit(f"{sequences}: not copied back")
    print(f"PACK BACK: {sequences}")
    print(f"PACK BACK: {len(list(target.rglob('*.png')))} skybox tiles, cake.inc.c "
          f"{'yes' if (target / 'levels/ending/cake.inc.c').is_file() else 'no'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--copy", nargs=2, metavar=("WORK_FOLDER", "TREE"))
    which.add_argument("--pack", metavar="BUILD_DIR")
    which.add_argument("--winpath", nargs=2, metavar=("WORK_FOLDER", "BUILD_DIR"))
    args = parser.parse_args()
    if args.copy:
        print(f"copied {copy_tree(args.copy[0], args.copy[1])} file(s) into {args.copy[1]}")
    elif args.pack:
        # (the build directory is relative to the tree it was copied into, as make takes it)
        make_pack(Path.cwd(), args.pack)
    else:
        copy_back(Path.cwd(), args.winpath[1], args.winpath[0])


if __name__ == "__main__":
    main()
