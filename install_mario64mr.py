#!/usr/bin/env python3
"""
Mario 64 MR, made from your own Super Mario 64 ROM (US) and put on your headset.

The app (Mario64MR.apk) carries none of Nintendo's game: no data and no code. This tool makes both on your own
computer, from your ROM and from the community's public sources, fetched at pinned revisions:

  the game data    extracted from your ROM with the sm64ex PC port's own tools
                     Mario64MRData/baserom.us.z64      the ROM, for the title screen's music and sounds
                     Mario64MRData/sm64/res/base.zip   sm64ex's resource pack: the textures and the sound banks
  the game code    compiled for the headset with Android's NDK
                     Mario64MRData/native/libsm64game.so   the whole game: sm64ex with Mario 64 MR's changes
                                                           (native/fullgame)
                     Mario64MRData/native/libgoddard.so    the title screen's face: the decomp's Goddard
                                                           (n64decomp/sm64, with native/goddard)
                     Mario64MRData/native/libsm64.so       the title screen's sound (libsm64)

usage:
    python3 install_mario64mr.py "Super Mario 64 (USA).z64"            # makes ./Mario64MRData
    python3 install_mario64mr.py "Super Mario 64 (USA).z64" --push     # ...and adb pushes it to the headset
    python3 install_mario64mr.py "Super Mario 64 (USA).z64" --push --label my-label.png
                                                                       # ...with your own cartridge label
    python3 install_mario64mr.py "Super Mario 64 (USA).z64" --check    # only checks the ROM

Needs python3, git, make, patch and a C compiler (macOS: Xcode command line tools; Linux: build-essential;
Windows: WSL). The Android NDK (r27c) is found where Android Studio or Homebrew put it, or downloaded (~700 MB) the
first time. The ROM may be .z64, .v64 or .n64; it must be the unmodified US release (SHA-1 9bef1128...), checked
before anything else: a different region, a ROM hack, a bad dump or a zipped ROM is turned down with the reason.
"""
import argparse
import glob
import os
import platform
import re
import shlex
import shutil
import stat
import struct
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

# (the pack build on Windows runs under MSYS2's python3, which has been seen to fail at subprocess and at hashlib
#  when MSYS2's own OpenSSL is missing. Neither is needed to check the ROM or to plan, so neither is required here:
#  the build starts a process only on macOS and Linux)
try:
    import hashlib
except ImportError:
    hashlib = None
try:
    import subprocess
except ImportError:
    subprocess = None
try:
    import json
except ImportError:
    json = None

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "native/fullgame"))
import patch_sm64ex  # noqa: E402

SM64EX = "https://github.com/sm64pc/sm64ex.git"
SM64EX_REVISION = patch_sm64ex.REVISION
DECOMP = "https://github.com/n64decomp/sm64.git"
DECOMP_REVISION = "9921382a68bb0c865e5e45eb594d9c64db59b1af"
LIBSM64 = "https://github.com/libsm64/libsm64.git"
LIBSM64_REVISION = "fd11813208272b4271d92bd92feb8f3fdbe61be5"
NDK_VERSION = "r27c"
US_SHA1 = "9bef1128717f958171a4afac3ed78ee2bb4e86ce"
PACKAGE = "com.lucasmartinic.mario64mr"
DEVICE_FOLDER = f"/sdcard/Android/data/{PACKAGE}/files"
PACK_BUILD = "build/pack"
GAME_BUILD = "build/us_android_60_ext"
LIBRARIES = ("libsm64game.so", "libgoddard.so", "libsm64.so")
ANDROID_TARGET = "aarch64-linux-android29"
# Windows: run under MSYS2 (install_mario64mr.bat sets it up), where the NDK's compiler and adb are Windows programs
MSYS = platform.system().startswith(("MSYS", "MINGW", "CYGWIN"))


def native(path):
    """A path as the Windows programs (the NDK's compiler, adb) take it under MSYS2 (C:/...); as it is elsewhere."""
    if not MSYS:
        return str(path)
    return subprocess.check_output(["cygpath", "-m", str(path)], text=True).strip()


def run(cmd, cwd=None, env=None):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    if subprocess is None:  # (MSYS2's python3: the pack build there is make's, started by build_pack)
        sys.exit("this build starts no processes of its own: on Windows, run install_mario64mr.bat")
    subprocess.run([str(c) for c in cmd], cwd=cwd, check=True, env=env)


ROM_SIZE = 8 * 1024 * 1024
ROM_ORDERS = {  # the first word of an N64 ROM, as each kind of dump stores it
    b"\x80\x37\x12\x40": ".z64 (big-endian)",
    b"\x37\x80\x40\x12": ".v64 (byte-swapped)",
    b"\x40\x12\x37\x80": ".n64 (little-endian)",
}
REGIONS = {"E": "USA", "J": "Japan", "P": "Europe"}
ARCHIVES = {b"PK\x03\x04": "a .zip", b"7z\xbc\xaf": "a .7z", b"Rar!": "a .rar", b"\x1f\x8b": "a .gz"}


class RomError(Exception):
    pass


def rom_problem(path, message, hint):
    return RomError(f"{path}: {message}\n  {hint}\n  Nothing was built or sent to the headset.")


def describe_rom(rom):
    """What an N64 ROM's header says it is (in .z64 order): its name, game code, region and revision."""
    name = rom[0x20:0x34].decode("ascii", "replace").strip(" \0")
    code = rom[0x3B:0x3F].decode("ascii", "replace")
    region = REGIONS.get(code[3:], f"region '{code[3:]}'")
    if code == "NSMJ" and rom[0x3F] == 3:
        region = "Japan, Shindou edition"
    return name, code, region, rom[0x3F]


def read_rom(path):
    """The ROM in .z64 byte order (big-endian), from any of the three dump orders, checked to be exactly the
    unmodified US release, with a clear reason when it isn't (before anything is downloaded, built or pushed)."""
    if not path.is_file():
        raise rom_problem(path, "no such file", "Give the path to your ROM file (in quotes if it has spaces).")
    data = path.read_bytes()
    head = data[:4]
    for magic, kind in ARCHIVES.items():
        if head.startswith(magic):
            raise rom_problem(path, f"this is {kind} archive, not a ROM",
                              "Extract it first, and give the .z64 / .v64 / .n64 file inside.")
    order = ROM_ORDERS.get(head)
    if order is None:
        raise rom_problem(path, "not a Nintendo 64 ROM (its header isn't one)",
                          "Use a Super Mario 64 (USA) dump: a .z64, .v64 or .n64 file of about 8 MB.")
    rom = bytearray(data)
    if order.startswith(".v64"):
        rom[0::2], rom[1::2] = rom[1::2], rom[0::2]
    elif order.startswith(".n64"):
        for i in range(0, len(rom) - 3, 4):
            rom[i:i + 4] = rom[i:i + 4][::-1]
    rom = bytes(rom)
    if hashlib is not None:
        sha1 = hashlib.sha1(rom).hexdigest()
    else:  # (a python without hashlib: the launcher, install_mario64mr.ps1, has checked it already)
        sha1 = "unchecked here: the launcher checked it"
    if hashlib is not None and sha1 == US_SHA1:
        print(f"ROM: Super Mario 64 (USA), {order}, SHA-1 matches", flush=True)
        return rom
    name, code, region, revision = describe_rom(rom)
    expected = f"An unmodified Super Mario 64 (USA) dump has SHA-1 {US_SHA1} (as .z64)."
    if code[1:3] != "SM" or "MARIO" not in name.upper():
        raise rom_problem(path, f"this is an N64 ROM of \"{name}\" (game code {code}), not Super Mario 64", expected)
    if code != "NSME":
        raise rom_problem(path, f"this is Super Mario 64 ({region}): only the USA release is supported",
                          "The game is built from the decompilation's US version, which needs the US ROM. " + expected)
    if len(rom) != ROM_SIZE:
        raise rom_problem(path, f"Super Mario 64 (USA) by its header, but {len(rom) / 1048576:.0f} MB instead of 8 MB: "
                          "a ROM hack (they're often extended) or a bad dump", expected)
    raise rom_problem(path, f"Super Mario 64 (USA) by its header, but its contents differ (SHA-1 {sha1}): "
                      "a ROM hack, a patched or trainer ROM, or a bad dump", expected)


def fetch(url, revision, work, shallow=False):
    """A checkout of url at revision, in work (kept for the next run)."""
    if not (work / ".git").is_dir():
        work.mkdir(parents=True, exist_ok=True)
        run(["git", "init", "--quiet"], cwd=work)
        run(["git", "remote", "add", "origin", url], cwd=work)
    run(["git", "fetch", "--quiet"] + (["--depth", "1"] if shallow else []) + ["origin", revision], cwd=work)
    run(["git", "checkout", "--quiet", "--force", "--detach", revision], cwd=work)


# ------------------------------------------------------------------------------------------------ the game data

STALL_SECONDS = 240  # (the pack build writing nothing this long: not slow, stuck: see pack_stalled())


def newest(directory):
    """The time of the most recently written file under directory (what says a build is still going), or None."""
    latest = None
    for root, _, names in os.walk(directory):
        for name in names:
            try:
                m = os.path.getmtime(os.path.join(root, name))
            except OSError:
                continue
            if latest is None or m > latest:
                latest = m
    return latest


def pack_stalled(built_at):
    """Whether the pack build has written nothing for STALL_SECONDS, with its make still running: the MSYS2 tools that
    make the sound take very little of the time (seen on one machine: minutes per sample file), so a pack build
    that stops progressing is treated as that, and the pack is built in WSL instead (make_game_wsl.py)."""
    if not MSYS:
        return False
    make = subprocess.run(["tasklist", "/fi", "imagename eq make.exe", "/fo", "csv"], capture_output=True, text=True)
    if "make.exe" not in make.stdout.lower():
        return False
    return time.time() - built_at > STALL_SECONDS


def plan_wsl_pack(work):
    """What the pack build needs on the WSL side (a few tens of MB, not the work folder's gigabytes of ROM assets and
    build output), as make_game_wsl.py --copy takes it."""
    copy = [p for p in ("sound", "assets", "levels", "Makefile", "Makefile.split", "include", "src", "enhancements",
                        "rsp", "textures", "tools", "charmap.txt", "charmap_menu.txt") if (work / p).exists()]
    return {"source": native(work).rstrip("/"), "destination": "~/mario64mr-wsl/" + work.name, "copy": copy,
            "build_dir": PACK_BUILD}


def wsl(*args, tools=None):
    """make_game_wsl.py (tools), run by WSL with these arguments: Windows paths become WSL's, and the script runs from
    the WSL home (this is the Windows build: WSL is there, under its own home)."""
    # (on Windows, WSL takes these at once; inside WSL, its own paths: wsl.exe is still there to find it)
    to_wsl = (lambda a: run_out(["wsl.exe", "wslpath", "-u", str(a)])) if not on_in_wsl() else str
    quoted = " ".join(shlex.quote(to_wsl(a) if os.path.isabs(str(a)) and not str(a).startswith("/mnt/") else str(a))
                      for a in args)
    script = to_wsl(tools)
    run(["wsl.exe", "-e", "bash", "-lc", "cd \"$HOME\" && python3 %s %s" % (shlex.quote(script), quoted)])


def on_in_wsl():
    """Whether this is running inside WSL (Linux), not on Windows."""
    return platform.system() == "Linux" and "microsoft" in platform.uname().release.lower()


def build_pack_in_wsl(work, plan):
    """The pack build where MSYS2's sound tools hang: the same build, made by WSL (Linux gcc, its own fork), from a
    copy of what it needs, then copied back into the work folder as if it had been made there."""
    plan_path = work / "pack-plan.json"
    plan_path.write_text(json.dumps(plan))
    print("The pack build has stopped making progress (its sound tools are very slow on some Windows setups). "
          "Building the pack in WSL instead...", flush=True)
    tools = HERE / "windows/make_game_wsl.py"
    wsl("--copy", plan_path, tools=tools)
    wsl("--pack", plan_path, tools=tools)
    there = Path(run_out(["wsl.exe", "-e", "bash", "-lc", 'echo "$HOME/mario64mr-wsl/%s"' % work.name]))
    for top in ("build/pack", "assets", "textures", "sound"):
        src = there / top
        if src.is_dir():
            shutil.copytree(src, work / top, dirs_exist_ok=True)


def run_out(cmd):
    return subprocess.check_output([str(c) for c in cmd], text=True).strip()

def prepare_pack(work, rom):
    # macOS ships GNU Make 3.81, which predates "!=" assignments
    makefile = subprocess.check_output(["git", "show", "HEAD:Makefile"], cwd=work, text=True)
    (work / "Makefile").write_text(re.sub(r"^(\w+) != (.*)$", r"\1 := $(shell \2)", makefile, flags=re.M))
    split = subprocess.check_output(["git", "show", "HEAD:Makefile.split"], cwd=work, text=True)
    (work / "Makefile.split").write_text(re.sub(r"^define (\w+) =[^\S\n]*$", r"define \1", split, flags=re.M))
    (work / "baserom.us.z64").write_bytes(rom)
    # Sequence 0 is data-only assembly upstream: take its bytes from the ROM instead of needing a MIPS assembler
    table = 0x7B0860
    offset, size = struct.unpack_from(">II", rom, table + 4)
    (work / "sound/sequences/00_sound_player.m64").write_bytes(rom[table + offset:table + offset + size])
    (work / "sound/sequences/00_sound_player.s").unlink(missing_ok=True)


def jobs():
    """How many jobs to run at once: two per core (the usual oversubscription: much of a build is one core waiting on
    a file), at least 4."""
    return str(max(4, 2 * (os.cpu_count() or 4)))


def extracted(work):
    """Whether extract_assets.py has run here: what it makes, and the build needs. Its music sequences are the ones
    that matter for the check: the pack build's Makefile depends on the sound/sequences/us directory itself, and
    make fails with "No rule to make target 'sound/sequences/us'" when it is missing (assets/demo_data.json alone
    was not enough: it is written before the sequences)."""
    return (work / "assets/demo_data.json").is_file() and any((work / "sound/sequences/us").glob("*.m64"))


def count(directory, pattern):
    return len(list(directory.rglob(pattern))) if directory.is_dir() else 0


def extract(work):
    """extract_assets.py, followed while it runs: it says nothing between "extracting" and finishing (the sound
    samples are one long call to disassemble_sound.py), so on a slow disk it looks hung for tens of minutes. This
    reports what it has made so far every 30 seconds: samples, textures, sequences."""
    print("Extracting the game's assets from the ROM (it says nothing while it works: counted below)", flush=True)
    began = time.time()
    process = subprocess.Popen([sys.executable, "extract_assets.py", "us"], cwd=work,
                               stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    while process.poll() is None:
        time.sleep(30)
        if process.poll() is not None:
            break
        print("  %d:%02d  textures %d, sound samples %d/219, music sequences %d/34"
              % ((time.time() - began) // 60, (time.time() - began) % 60,
                 count(work / "textures", "*.png"), count(work / "sound/samples", "*.aiff"),
                 count(work / "sound/sequences/us", "*.m64")), flush=True)
    if process.returncode != 0:
        sys.exit(f"extract_assets.py failed ({process.returncode})")
    print("  %d:%02d  extracted" % ((time.time() - began) // 60, (time.time() - began) % 60), flush=True)


def build_pack(work):
    # (tools/common: the library tabledesign links against, built first. Upstream's tools/Makefile has no edge from
    #  tabledesign to audiofile/libaudiofile.a, so make -j can link it before the library exists: "cannot find
    #  -laudiofile". Built alone first, then the rest in parallel.)
    run(["make", "-C", "tools/audiofile", "-j" + jobs()], cwd=work)
    run(["make", "-C", "tools", "-j" + jobs()], cwd=work)
    # (extracted once: on a Windows drive its tens of thousands of small files take minutes to check again)
    if not extracted(work):
        extract(work)
    # Only the resource pack's parts of the game build: the sound banks, and the skyboxes (and cake) cut into tiles
    targets = [f"{PACK_BUILD}/sound/sound_data.ctl", f"{PACK_BUILD}/sound/sequences.bin"]
    targets += [f"{PACK_BUILD}/bin/{p.stem}_skybox.c" for p in sorted((work / "textures/skyboxes").glob("*.png"))]
    targets.append(f"{PACK_BUILD}/levels/ending/cake.inc.c")  # (the ending's cake picture, cut the same way)
    # (the system's compiler and preprocessor: on macOS the Makefile would look for Homebrew's gcc)
    command = ["make", "-j" + jobs(), "VERSION=us", "EXTERNAL_DATA=1", "BUILD_DIR=" + PACK_BUILD, "NOEXTRACT=1",
               "HOST_OS=Linux", "OSX_BUILD=0", "CC=cc", "CXX=c++", "CPP=cc -E -P -x c"] + targets
    print("+", " ".join(command), flush=True)
    make = subprocess.Popen([str(c) for c in command], cwd=work)
    built = newest(work / PACK_BUILD) or time.time()
    stalled = False
    # (in pieces, so a build that stops writing files (not slow: stuck, on Windows: its sound tools don't run under
    #  MSYS2) is noticed rather than waited on)
    while make.poll() is None:
        time.sleep(10)
        built_now = newest(work / PACK_BUILD)
        if built_now and built_now > built:
            built = built_now
        stalled = pack_stalled(built)
        if stalled:
            make.kill()
            make.wait()
            break
    if stalled:
        build_pack_in_wsl(work, plan_wsl_pack(work))
    elif make.returncode != 0:
        sys.exit(f"make failed ({make.returncode})")


def pack(work, out):
    """base.zip as sm64ex's own "res" target makes it (Makefile, basepack.lst)."""
    files = [(work / PACK_BUILD / "sound" / n, "sound/" + n) for n in ("bank_sets", "sequences.bin", "sound_data.ctl", "sound_data.tbl")]
    tiles = work / PACK_BUILD / "textures/skybox_tiles"
    files += [(p, "gfx/" + p.relative_to(work / PACK_BUILD).as_posix()) for p in sorted(tiles.rglob("*.png"))]
    for top in ("actors", "levels", "textures"):
        files += [(p, "gfx/" + p.relative_to(work).as_posix()) for p in sorted((work / top).rglob("*.png"))]
    missing = [str(p) for p, _ in files if not p.is_file()]
    if missing:
        sys.exit("not made: " + ", ".join(missing[:5]))
    zip_path = out / "sm64/res/base.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for src, name in files:
            z.write(src, name)
    print(f"{zip_path}: {len(files)} files, {zip_path.stat().st_size / 1e6:.1f} MB")


# ------------------------------------------------------------------------------------------------ the game code

def host_tag():
    system = platform.system()
    if system == "Darwin":
        return "darwin-x86_64"  # (universal: Apple silicon too)
    if system == "Linux":
        return "linux-x86_64"
    if MSYS:
        return "windows-x86_64"
    sys.exit(f"{system} isn't supported: on Windows, double-click install_mario64mr.bat (or run this in WSL)")


def ndk_compiler(ndk):
    """The NDK's C compiler for the headset (on Windows clang.exe itself: its .cmd wrappers don't run from make)."""
    bin_dir = Path(ndk) / "toolchains/llvm/prebuilt" / host_tag() / "bin"
    return bin_dir / "clang.exe" if MSYS else bin_dir / f"{ANDROID_TARGET}-clang"


def compiler(ndk, cxx=False):
    """The command that compiles for the headset, as a list."""
    path = ndk_compiler(ndk)
    if MSYS:
        return [native(path.with_name("clang++.exe" if cxx else "clang.exe")), "--target=" + ANDROID_TARGET]
    return [str(path) + ("++" if cxx else "")]


def find_ndk(work, given):
    """An Android NDK (r27 or later works; r27c is what the app is built with), else one downloaded into work."""
    candidates = [given] if given else []
    candidates += [os.environ.get(v) for v in ("ANDROID_NDK_HOME", "ANDROID_NDK_ROOT", "ANDROID_NDK")]
    home = Path.home()
    for pattern in (home / "Library/Android/sdk/ndk/*", home / "Android/Sdk/ndk/*",
                    "/opt/homebrew/share/android-ndk", "/usr/local/share/android-ndk",
                    "/Applications/Unity/Hub/Editor/*/PlaybackEngines/AndroidPlayer/NDK", ndk_home(work) / "ndk",
                    work / "ndk"):
        candidates += sorted(glob.glob(str(pattern)), reverse=True)
    for c in candidates:
        if c and ndk_compiler(c).is_file():
            print(f"Android NDK: {c}")
            return Path(c)
    return download_ndk(work)


def on_windows_drive(path):
    """A path on a Windows drive under WSL (/mnt/c/...): slow, and Windows' path length limit applies."""
    return Path(path).resolve().as_posix().startswith("/mnt/") and "microsoft" in platform.uname().release.lower()


WINDOWS_PATH_LIMIT = 260  # (MAX_PATH, as C:\... counts it)


def ndk_home(work):
    """Where a downloaded NDK goes: with the work folder, unless that's on a Windows drive under WSL, where its long
    paths can go past Windows' limit and its 1 GB of small files take ages: then in the Linux home."""
    return Path.home() / ".cache/mario64mr" if on_windows_drive(work) else work


class Progress:
    """Prints how far along something long is, every 5%, so it never looks stuck."""

    def __init__(self, what):
        self.what, self.shown = what, -1

    def __call__(self, done, total):
        if total <= 0:
            return
        step = min(100, done * 100 // total) // 5 * 5
        if step != self.shown:
            self.shown = step
            print(f"  {self.what}: {step}%", flush=True)


def extract_ndk_zip(archive, unpacked):
    """Unzips the NDK with its executable bits and symlinks, showing progress, and stops with the reason (rather than
    hanging, or going on half unpacked) when a file can't be written: on a Windows drive, usually the path length."""
    with zipfile.ZipFile(archive) as z:
        entries = z.infolist()
        if on_windows_drive(unpacked):
            longest = max(entries, key=lambda e: len(e.filename))
            # (/mnt/c/... is C:\..., 4 characters fewer)
            length = len((unpacked / longest.filename).as_posix()) - 4
            if length >= WINDOWS_PATH_LIMIT:
                sys.exit(f"Can't unpack the Android NDK in {unpacked}: its longest path would be {length} characters "
                         f"there, past Windows' limit of {WINDOWS_PATH_LIMIT - 1}.\n"
                         f"  Run the tool from your WSL home instead (cd ~), or give a shorter --work folder.")
        total, done = sum(e.file_size for e in entries), 0
        progress = Progress("Unpacking the Android NDK")
        root = unpacked.resolve()
        for entry in entries:
            path = unpacked / entry.filename
            if root not in path.resolve().parents and path.resolve() != root:
                sys.exit(f"{archive}: unexpected path inside it: {entry.filename}")
            mode = entry.external_attr >> 16
            try:
                if entry.is_dir():
                    path.mkdir(parents=True, exist_ok=True)
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                if stat.S_ISLNK(mode):
                    os.symlink(z.read(entry).decode(), path)
                else:
                    with z.open(entry) as source, open(path, "wb") as file:
                        shutil.copyfileobj(source, file, 1 << 20)
                    if mode & 0o777:
                        os.chmod(path, mode & 0o777)
            except OSError as error:
                hint = ("On a Windows drive this is usually Windows' path length limit: run the tool from your WSL home "
                        "(cd ~), or give a shorter --work folder." if on_windows_drive(unpacked) else
                        "Check there's enough free space (the NDK needs about 2 GB unpacked), or give another --work folder.")
                sys.exit(f"Unpacking the Android NDK failed at {path} ({len(path.as_posix())} characters):\n"
                         f"  {error}\n  {hint}")
            done += entry.file_size
            progress(done, total)


def download_ndk(work):
    system = platform.system()
    name = f"android-ndk-{NDK_VERSION}-" + ("darwin.dmg" if system == "Darwin" else "windows.zip" if MSYS else "linux.zip")
    url = "https://dl.google.com/android/repository/" + name
    home = ndk_home(work)
    home.mkdir(parents=True, exist_ok=True)
    if home != work:
        print(f"The Android NDK goes in {home} (your work folder is on a Windows drive, where it wouldn't fit)")
    archive = home / name
    if not archive.is_file():
        print(f"Downloading the Android NDK ({url}, about 700 MB)...", flush=True)
        part = archive.with_suffix(".part")
        progress = Progress("Downloading the Android NDK")
        urllib.request.urlretrieve(url, part, lambda blocks, size, total: progress(blocks * size, total))
        part.rename(archive)
    target = home / "ndk"
    shutil.rmtree(target, ignore_errors=True)
    if system == "Darwin":
        mount = work / "ndk-mount"
        mount.mkdir(exist_ok=True)
        run(["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", mount, archive])
        try:
            apps = glob.glob(str(mount / "*.app/Contents/NDK"))
            if not apps:
                sys.exit(f"No NDK inside {archive}")
            run(["cp", "-R", apps[0], target])
        finally:
            run(["hdiutil", "detach", mount])
    else:
        unpacked = home / "ndk-unpacked"
        shutil.rmtree(unpacked, ignore_errors=True)
        extract_ndk_zip(archive, unpacked)
        (unpacked / f"android-ndk-{NDK_VERSION}").rename(target)
        shutil.rmtree(unpacked)
    if not ndk_compiler(target).is_file():
        sys.exit(f"The NDK in {target} has no {ndk_compiler(target).name}")
    return target


def make_compilers(ndk, work):
    """The compilers as make takes them: single words (sm64ex's Makefile passes them on unquoted). On Windows, small
    wrappers round clang.exe and its --target, as the NDK's own are elsewhere (its .cmd ones don't run from make)."""
    if not MSYS:
        return " ".join(compiler(ndk)), " ".join(compiler(ndk, cxx=True))
    wrappers = work / "ndk-bin"
    wrappers.mkdir(exist_ok=True)
    names = []
    for name, cxx in ((ANDROID_TARGET + "-clang", False), (ANDROID_TARGET + "-clang++", True)):
        command = compiler(ndk, cxx)
        (wrappers / name).write_text(f'#!/bin/sh\nexec "{command[0]}" {" ".join(command[1:])} "$@"\n')
        (wrappers / name).chmod(0o755)
        names.append(str(wrappers / name))
    return tuple(names)


def build_game(work, rom, ndk, out):
    """libsm64game.so: sm64ex with the PC port's 60 fps interpolation, the Unity bridge and Mario 64 MR's changes,
    its data left out (it reads base.zip at runtime)."""
    patch_sm64ex.prepare(work, rom, sixty=True, external_data=True)
    # The attract demos' inputs are game data compiled in even with external data: blank them (never played, the
    # demo timer is off)
    for demo in (work / "assets/demos").glob("*.bin"):
        demo.write_bytes(bytes(4))
    cc, cxx = make_compilers(ndk, work)
    flags = "-Wno-error=implicit-function-declaration -Wno-error=int-conversion -Wno-error=incompatible-pointer-types"
    run(["make", "-j" + jobs(), "HOST_OS=Linux", "OSX_BUILD=0", "BUILD_DIR=" + GAME_BUILD,
         "CC=" + cc, "CXX=" + cxx, "LD=" + cc, "CPP=" + cc + " -E -P -x c",
         "RENDER_API=UNITY", "WINDOW_API=UNITY", "AUDIO_API=UNITY", "CONTROLLER_API=UNITY", "NOEXTRACT=1",
         "EXTERNAL_DATA=1", "EXT_OPTIONS_MENU=1", "TEXTURE_FIX=0", "BETTERCAMERA=0", "NODRAWINGDISTANCE=0",
         "LDFLAGS=-shared -Wl,--no-undefined -Wl,-soname,libsm64game.so -Wl,-z,max-page-size=16384 -lm",
         "OPT_FLAGS=-O2 -fPIC " + flags,
         f"CC_CHECK={cc} -fsyntax-only -fPIC -Isrc -Iinclude -I. -I{GAME_BUILD} -I{GAME_BUILD}/include -DVERSION_US "
         f"-D_LANGUAGE_C -DNON_MATCHING -DAVOID_UB -DF3DEX_GBI_2E -DRAPI_UNITY -DWAPI_UNITY -DAAPI_UNITY {flags}"],
        cwd=work)
    shutil.copy2(work / GAME_BUILD / "sm64.us.f3dex2e", out / "libsm64game.so")


def build_goddard(decomp, extracted, ndk, work, out):
    """libgoddard.so: the decomp's Goddard (the title screen's face) as a library, with Mario 64 MR's patches (two
    hands, holding, depth) and a small API (native/goddard/shim). Its textures are blanks, filled at runtime from
    base.zip."""
    kit = HERE / "native/goddard"
    src = work / "src"
    shutil.rmtree(src, ignore_errors=True)
    src.mkdir(parents=True)
    shutil.copytree(decomp / "src/goddard", src / "goddard")
    for p in sorted((kit / "patches").glob("*.patch")):
        with open(p) as f:
            print(f"+ patch {p.name}", flush=True)
            subprocess.run(["patch", "-s", "-d", str(src / "goddard"), "-p1"], stdin=f, check=True)
    with open(src / "goddard/renderer.c", "a") as f:
        f.write('#include "gd_texture_table.inc.c"\n')
    gen = work / "gen"
    shutil.rmtree(gen, ignore_errors=True)
    run([sys.executable, kit / "gen_textures.py", decomp, extracted, gen])
    sources = sorted((src / "goddard").glob("*.c")) + sorted((src / "goddard/dynlists").glob("*.c"))
    sources += [decomp / "lib/src" / n for n in ("guPerspectiveF.c", "guOrthoF.c", "guRotateF.c", "guTranslateF.c",
                                                 "guMtxF2L.c", "guScaleF.c", "guNormalize.c")]
    sources += [kit / "shim/n64_stubs.c", kit / "shim/gd_api.c"]
    defines = ["-DVERSION_US=1", "-DF3D_OLD=1", "-DNON_MATCHING=1", "-DAVOID_UB=1", "-D_LANGUAGE_C",
               "-DNO_SEGMENTED_MEMORY", "-DGBI_FLOATS"]
    includes = ["-I" + native(p) for p in (gen, src, decomp / "include", decomp / "src", decomp, kit / "shim")]
    run(compiler(ndk) + ["-std=gnu99", "-O2", "-fPIC", "-fno-strict-aliasing", "-fsigned-char", "-fvisibility=hidden",
         "-w", "-U_FORTIFY_SOURCE", "-D_FORTIFY_SOURCE=0"] + defines + includes +
        ["-shared", "-Wl,-soname,libgoddard.so", "-Wl,-z,max-page-size=16384", "-o", native(out / "libgoddard.so")]
        + [native(p) for p in sources] + ["-lm"])


# import_mario_geo()'s templates: libsm64's own, so what it makes is what its build expects (the includes are relative
# to src/decomp/mario)
GEO_INC_C_HEADER = """
#include "../include/sm64.h"
#include "../include/types.h"
#include "../include/geo_commands.h"
#include "../game/rendering_graph_node.h"
#include "../shim.h"
#include "../game/object_stuff.h"
#include "../game/behavior_actions.h"
#include "model.inc.h"

#define SHADOW_CIRCLE_PLAYER 99
"""

GEO_INC_C_FOOTER = """
const GeoLayout mario_geo_libsm64[] = {
   GEO_SHADOW(SHADOW_CIRCLE_PLAYER, 0xB4, 100),
   GEO_OPEN_NODE(),
      GEO_ZBUFFER(1),
      GEO_OPEN_NODE(),
         GEO_SCALE(0x00, 16384),
         GEO_OPEN_NODE(),
            GEO_ASM(0, geo_mirror_mario_backface_culling),
            GEO_ASM(0, geo_mirror_mario_set_alpha),
            GEO_BRANCH(1, mario_geo_load_body),
            GEO_ASM(1, geo_mirror_mario_backface_culling),
         GEO_CLOSE_NODE(),
      GEO_CLOSE_NODE(),
   GEO_CLOSE_NODE(),
   GEO_END(),
};

void *mario_geo_ptr = (void*)mario_geo_libsm64;

"""

GEO_INC_H = """
#pragma once

extern void *mario_geo_ptr;
"""

MODEL_INC_H = """
#pragma once

#include "../include/types.h"
#include "../include/PR/gbi.h"
"""


LIB_SM64_REVISION = "06ec56df7f951f88da05f468cdcacecba496145a"  # (the sources import-mario-geo.py takes Mario from)


def import_mario_geo(libsm64):
    """Mario's model, from the decomp's sources: what libsm64's import-mario-geo.py makes (its four files, transformed
    the same way), made here instead of run as an outside script: one fewer process to start, and its output is the
    installer's."""
    files = ("geo.inc.c", "model.inc.c", "model.inc.h", "geo.inc.h")
    if all((libsm64 / "src/decomp/mario" / n).is_file() for n in files):
        return
    base = "https://raw.githubusercontent.com/n64decomp/sm64/%s/actors/mario/" % LIB_SM64_REVISION
    print("Downloading " + base + "geo.inc.c and model.inc.c (Mario's model)", flush=True)
    geo = urllib.request.urlopen(base + "geo.inc.c", timeout=60).read().decode()
    model = urllib.request.urlopen(base + "model.inc.c", timeout=60).read().decode()
    lines, skip, header = model.splitlines(), 0, MODEL_INC_H
    for i, line in enumerate(lines):
        if skip:  # (the model's own textures: libsm64 draws Mario from the ROM's, so these are left out)
            skip -= 1
            lines[i] = "//" + line
        elif line.startswith("ALIGNED8 static const u8 mario_"):
            skip = 2
            lines[i] = "//" + line
        elif line.startswith("const "):
            header += "\nextern " + line.replace(" = {", ";")
    # (libsm64's script inserts them one at a time at the top, so the second one ends up first)
    lines[:0] = ['#include "../../load_tex_data.h"', '#include "../../gfx_macros.h"']
    mario = libsm64 / "src/decomp/mario"
    shutil.rmtree(mario, ignore_errors=True)
    mario.mkdir(parents=True)
    (mario / "geo.inc.c").write_text(GEO_INC_C_HEADER + geo + GEO_INC_C_FOOTER)
    (mario / "model.inc.c").write_text("\n".join(lines))
    (mario / "model.inc.h").write_text(header)
    (mario / "geo.inc.h").write_text(GEO_INC_H)


def build_libsm64(libsm64, ndk, out):
    """libsm64.so: libsm64 as it is upstream (the title screen plays the game's music and sounds through it, from
    your ROM)."""
    import_mario_geo(libsm64)
    dirs = ["src", "src/decomp", "src/decomp/engine", "src/decomp/include/PR", "src/decomp/game", "src/decomp/pc",
            "src/decomp/pc/audio", "src/decomp/mario", "src/decomp/tools", "src/decomp/audio"]
    sources = sorted(str(p.relative_to(libsm64)) for d in dirs for p in (libsm64 / d).glob("*.c"))
    run(compiler(ndk) + ["-O2", "-fno-strict-aliasing", "-Wno-everything", "-fPIC", "-fvisibility=hidden",
         "-DSM64_LIB_EXPORT", "-DGBI_FLOATS", "-DVERSION_US", "-DNO_SEGMENTED_MEMORY", "-I", "src/decomp/include",
         "-shared", "-Wl,-soname,libsm64.so", "-Wl,-z,max-page-size=16384", "-o", native(out / "libsm64.so")]
        + sources + ["-lm"], cwd=libsm64)


# ------------------------------------------------------------------------------------------------ the headset

def add_label(picture, out):
    """Your own cartridge label (the app has a plain one): a PNG or JPEG, square, with the label in the middle as
    tall as the picture."""
    ext = picture.suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg"):
        sys.exit(f"{picture}: the label must be a .png or .jpg picture")
    head = picture.read_bytes()[:4]
    if not (head.startswith(b"\x89PNG") or head.startswith(b"\xff\xd8")):
        sys.exit(f"{picture}: not a PNG or JPEG picture")
    for old in out.glob("label.*"):
        old.unlink()
    shutil.copy2(picture, out / ("label" + (".png" if ext == ".png" else ".jpg")))


def push(out, wait=False, apk=None):
    if MSYS:
        # adb is a Windows program: MSYS2 would turn its headset paths (/sdcard/...) into Windows ones
        os.environ["MSYS2_ARG_CONV_EXCL"] = "*"
    while True:
        devices = subprocess.run(["adb", "devices"], capture_output=True, text=True, check=True).stdout
        if "\tdevice" in devices:
            break
        if not wait:
            sys.exit("No headset on adb: connect it (developer mode, USB debugging) and run again with --push")
        input("\nNo headset found. Connect it with USB (developer mode on), put it on and allow USB debugging,\n"
              "then press Enter here to send the game to it... ")
    if apk:
        print(f"Installing {apk.name} on the headset...", flush=True)
        run(["adb", "install", "-r", "-g", native(apk)])
    # The app's own folder, made by the app (one made by adb may not be the app's): open it once if needed
    if subprocess.run(["adb", "shell", "ls", DEVICE_FOLDER], capture_output=True).returncode != 0:
        if PACKAGE not in subprocess.run(["adb", "shell", "pm", "list", "packages", PACKAGE], capture_output=True, text=True).stdout:
            sys.exit("Mario 64 MR is not installed: adb install -r -g Mario64MR.apk first (or run again with "
                     "--apk Mario64MR.apk)")
        print("Opening Mario 64 MR once, so it makes its data folder...")
        run(["adb", "shell", "monkey", "-p", PACKAGE, "-c", "android.intent.category.LAUNCHER", "1"])
        for _ in range(30):
            if subprocess.run(["adb", "shell", "ls", DEVICE_FOLDER], capture_output=True).returncode == 0:
                break
            time.sleep(1)
        else:
            sys.exit("Mario 64 MR did not start: put the headset on (it must be awake), open it once, and run again")
        run(["adb", "shell", "am", "force-stop", PACKAGE])
    run(["adb", "shell", "mkdir", "-p", DEVICE_FOLDER + "/sm64/res", DEVICE_FOLDER + "/native"])
    run(["adb", "push", native(out / "baserom.us.z64"), DEVICE_FOLDER + "/"])
    run(["adb", "push", native(out / "sm64/res/base.zip"), DEVICE_FOLDER + "/sm64/res/"])
    run(["adb", "push"] + [native(out / "native" / n) for n in LIBRARIES] + [DEVICE_FOLDER + "/native/"])
    labels = sorted(out.glob("label.*"))
    if labels:
        run(["adb", "shell", "rm", "-f", DEVICE_FOLDER + "/label.png", DEVICE_FOLDER + "/label.jpg"])
        run(["adb", "push", native(labels[0]), DEVICE_FOLDER + "/"])
    run(["adb", "shell", "am", "force-stop", PACKAGE])
    print("On the headset. Start Mario 64 MR from your Library (Unknown Sources, or search for \"Mario\").")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("rom", type=Path, help="your Super Mario 64 (US) ROM")
    parser.add_argument("--out", type=Path, default=Path("Mario64MRData"), help="where to put it all (./Mario64MRData)")
    parser.add_argument("--work", type=Path, default=Path("Mario64MRData-work"),
                        help="sources, NDK and builds, kept to make a second run quick (./Mario64MRData-work)")
    parser.add_argument("--ndk", type=Path, help="an Android NDK to use (else found, or downloaded)")
    parser.add_argument("--push", action="store_true", help="also adb push it to the headset")
    parser.add_argument("--label", type=Path, help="your own cartridge label: a square PNG or JPEG (see the README)")
    parser.add_argument("--check", action="store_true", help="only check the ROM, and stop")
    parser.add_argument("--wait", action="store_true", help="with --push: if no headset is connected, wait for it")
    parser.add_argument("--apk", type=Path, help="with --push: also install the app (Mario64MR.apk) on the headset")
    args = parser.parse_args()
    # The ROM first: the most likely thing to be wrong, and nothing should be fetched, built or pushed until it's right
    try:
        rom = read_rom(args.rom)
    except RomError as problem:
        sys.exit(str(problem))
    if args.check:
        return
    missing = [t for t in ("git", "make", "patch", "cc") if shutil.which(t) is None]
    if missing:
        # (what to do about it, where the machine is known: a bare "cc not found" deep inside make is far less useful)
        if on_in_wsl():
            sys.exit(f"{', '.join(missing)} not found: this WSL has no build tools yet. Install them, then run this "
                     f"again:\n  sudo apt update && sudo apt install -y build-essential git python3")
        sys.exit(f"{', '.join(missing)} not found (see the top of this script for what is needed)")
    work, out = args.work.resolve(), args.out.resolve()
    if on_windows_drive(work):
        print(f"Note: {work} is on a Windows drive. It works, but building there is much slower than in your WSL "
              "home: cd ~ and run the tool from there.", flush=True)
    sm64ex = work / "sm64ex"
    (out / "native").mkdir(parents=True, exist_ok=True)
    if args.label:
        add_label(args.label, out)

    print("== The game data, from your ROM", flush=True)
    fetch(SM64EX, SM64EX_REVISION, sm64ex, shallow=True)
    prepare_pack(sm64ex, rom)
    build_pack(sm64ex)
    (out / "baserom.us.z64").write_bytes(rom)
    pack(sm64ex, out)

    print("== The game code, for the headset", flush=True)
    ndk = find_ndk(work, args.ndk)
    build_game(sm64ex, rom, ndk, out / "native")
    decomp = work / "sm64-decomp"
    fetch(DECOMP, DECOMP_REVISION, decomp, shallow=True)
    build_goddard(decomp, sm64ex, ndk, work / "goddard", out / "native")
    libsm64 = work / "libsm64"
    fetch(LIBSM64, LIBSM64_REVISION, libsm64, shallow=True)
    build_libsm64(libsm64, ndk, out / "native")
    for name in LIBRARIES:
        print(f"{out / 'native' / name}: {(out / 'native' / name).stat().st_size / 1e6:.1f} MB")

    if args.push:
        push(out, args.wait, args.apk)
    else:
        print(f"Done: {out}. To the headset: python3 {Path(sys.argv[0]).name} <rom> --push  (or the README's adb push)")


if __name__ == "__main__":
    main()
