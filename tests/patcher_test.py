#!/usr/bin/env python3
"""
Tests android/make_patched_exe.py against a fake Balatro.exe.

The fake exe is built like the real one: an executable stub with a zip
appended (LOVE "fused" game, zip offsets relative to the zip start).
If 7-Zip is installed, the result is also extracted with it, the same way
balatro-mobile-maker does.

Usage: python tests/patcher_test.py
"""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATCHER = os.path.join(ROOT, "android", "make_patched_exe.py")
CLIENT = os.path.join(ROOT, "pc-mod", "BalatroSync", "balatro_sync.lua")
CONFIG = os.path.join(ROOT, "pc-mod", "BalatroSync", "config.lua")
BOOT_LINE = 'require("balatro_sync").install(require("balatro_sync_config"))'

MAIN_LUA = (
    "if (love.system.getOS() == 'OS X' ) and (jit.arch == 'arm64' or jit.arch == 'arm') then jit.off() end\r\n"
    "require \"engine/object\"\r\n"
    "function love.load()\r\n"
    "        G.FPS_CAP = G.FPS_CAP or 60\r\n"
    "end\r\n"
)

results = []


def check(cond, label):
    results.append(bool(cond))
    print(("  ok   " if cond else "  FAIL ") + label, flush=True)


def make_fake_exe(path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("main.lua", MAIN_LUA)
        z.writestr("conf.lua", "function love.conf(t) t.identity = 'Balatro' end\n")
        z.writestr("resources/", b"")
        z.writestr("resources/textures/1x/dummy.png", os.urandom(2048))
        z.writestr("version.jkr", "1.0.1o-FULL\n")
    stub = b"MZ" + b"\x90" * 50000
    with open(path, "wb") as fh:
        fh.write(stub + buf.getvalue())
    return stub


def run_patcher(*args):
    return subprocess.run([sys.executable, PATCHER, *args], capture_output=True, text=True)


def find_7z():
    for name in ("7z", "7za", "7zz"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in (r"C:\Program Files\7-Zip\7z.exe", r"C:\Program Files (x86)\7-Zip\7z.exe"):
        if os.path.exists(candidate):
            return candidate
    return None


def main():
    work = tempfile.mkdtemp(prefix="bsync_patcher_")
    try:
        src = os.path.join(work, "steam", "Balatro.exe")
        os.makedirs(os.path.dirname(src))
        stub = make_fake_exe(src)
        maker = os.path.join(work, "maker")
        os.makedirs(maker)

        config = os.path.join(work, "config.lua")
        with open(CONFIG, encoding="utf-8") as fh:
            text = fh.read()
        text = text.replace('"http://192.168.1.10:8765"', '"http://10.0.0.5:8765"').replace('"CHANGE_ME"', '"testtoken"')
        with open(config, "w", encoding="utf-8") as fh:
            fh.write(text)

        print("1) Patch a fresh Balatro.exe")
        res = run_patcher("--exe", src, "--out", maker, "--device", "Phone", "--config", config)
        check(res.returncode == 0, "patcher exits with 0" + ("" if res.returncode == 0 else ": " + res.stderr))
        out = os.path.join(maker, "Balatro.exe")
        with open(out, "rb") as fh:
            data = fh.read()
        check(data.startswith(stub), "executable stub is unchanged")
        with zipfile.ZipFile(out) as z:
            names = set(z.namelist())
            main_lua = z.read("main.lua").decode("utf-8")
            client = z.read("balatro_sync.lua")
            cfg = z.read("balatro_sync_config.lua").decode("utf-8")
            check(z.testzip() is None, "archive is valid")
        check(main_lua.startswith(BOOT_LINE + "\n"), "main.lua starts with the sync hook")
        check(main_lua.endswith(MAIN_LUA), "rest of main.lua is untouched")
        with open(CLIENT, "rb") as fh:
            check(client == fh.read(), "balatro_sync.lua is included unchanged")
        check('device_name = "Phone"' in cfg, "device name is set from --device")
        check('"http://10.0.0.5:8765"' in cfg and '"testtoken"' in cfg, "server and token come from the config")
        check({"conf.lua", "version.jkr", "resources/textures/1x/dummy.png"} <= names, "game files are kept")
        check(not os.path.exists(os.path.join(maker, "Balatro.unpatched.exe")), "no backup when nothing was replaced")

        seven = find_7z()
        if seven:
            print("2) Extract with 7-Zip like balatro-mobile-maker")
            target = os.path.join(work, "extracted")
            res7 = subprocess.run([seven, "x", "-y", out, "-o" + target], capture_output=True, text=True)
            check(res7.returncode == 0, "7-Zip extracts the patched exe")
            with open(os.path.join(target, "main.lua"), encoding="utf-8") as fh:
                check(fh.readline().strip() == BOOT_LINE, "extracted main.lua has the hook")
            check(os.path.exists(os.path.join(target, "balatro_sync.lua")), "extracted sync code exists")
            check(os.path.exists(os.path.join(target, "balatro_sync_config.lua")), "extracted config exists")
        else:
            print("2) 7-Zip not installed - skipping extraction test")

        print("3) Patch again from an already patched exe")
        again_dir = os.path.join(work, "maker2")
        os.makedirs(again_dir)
        shutil.copy(out, os.path.join(again_dir, "Balatro.exe"))
        res = run_patcher("--exe", out, "--out", again_dir, "--device", "Tablet", "--config", config)
        check(res.returncode == 0, "second run exits with 0")
        with zipfile.ZipFile(os.path.join(again_dir, "Balatro.exe")) as z:
            main_lua = z.read("main.lua").decode("utf-8")
            check(main_lua.count(BOOT_LINE) == 1, "hook is not added twice")
            check(z.namelist().count("balatro_sync.lua") == 1, "sync code is not duplicated")
            check('device_name = "Tablet"' in z.read("balatro_sync_config.lua").decode("utf-8"), "device name is updated")
        check(not os.path.exists(os.path.join(again_dir, "Balatro.unpatched.exe")),
              "a patched target is not saved as 'unpatched' backup")

        print("4) Replacing an unpatched exe keeps a backup")
        maker3 = os.path.join(work, "maker3")
        os.makedirs(maker3)
        shutil.copy(src, os.path.join(maker3, "Balatro.exe"))
        res = run_patcher("--exe", src, "--out", maker3, "--config", config)
        check(res.returncode == 0, "patcher exits with 0")
        backup = os.path.join(maker3, "Balatro.unpatched.exe")
        with open(src, "rb") as a, open(backup, "rb") as b:
            check(a.read() == b.read(), "original is kept as Balatro.unpatched.exe")

        print("5) Refuse to overwrite the source exe")
        res = run_patcher("--exe", src, "--out", os.path.dirname(src), "--config", config)
        check(res.returncode != 0 and "refusing" in (res.stderr + res.stdout), "source exe is never overwritten")
        with open(src, "rb") as fh:
            check(b"balatro_sync" not in fh.read(), "source exe is still unpatched")

        print("6) Warn about an unconfigured config")
        os.makedirs(os.path.join(work, "maker4"), exist_ok=True)
        res = run_patcher("--exe", src, "--out", os.path.join(work, "maker4"), "--config", CONFIG)
        check("example IP" in res.stdout and "token" in res.stdout, "placeholder IP and token are reported")

        print("7) Reject something that is not Balatro")
        bogus = os.path.join(work, "bogus.exe")
        with open(bogus, "wb") as fh:
            fh.write(b"MZ" + b"\0" * 1000)
        res = run_patcher("--exe", bogus, "--out", os.path.join(work, "maker4"), "--config", config)
        check(res.returncode != 0, "file without a game archive is rejected")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("\n%d passed, %d failed" % (sum(results), len(results) - sum(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
