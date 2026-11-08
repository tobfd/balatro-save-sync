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

        print("6) Refuse an unconfigured config")
        os.makedirs(os.path.join(work, "maker4"), exist_ok=True)
        res = run_patcher("--exe", src, "--out", os.path.join(work, "maker4"), "--config", CONFIG)
        check(res.returncode != 0, "placeholder config stops the patcher")
        check("example IP" in res.stderr and "token" in res.stderr and "--config" in res.stderr,
              "error names the problems and how to fix them")
        check(not os.path.exists(os.path.join(work, "maker4", "Balatro.exe")), "nothing is written")

        print("7) Reject something that is not Balatro")
        bogus = os.path.join(work, "bogus.exe")
        with open(bogus, "wb") as fh:
            fh.write(b"MZ" + b"\0" * 1000)
        res = run_patcher("--exe", bogus, "--out", os.path.join(work, "maker4"), "--config", config)
        check(res.returncode != 0, "file without a game archive is rejected")

        if sys.platform != "win32":
            linux_steam_tests(work, config)
        config_lookup_tests(work, src, config)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("\n%d passed, %d failed" % (sum(results), len(results) - sum(results)))
    return 0 if all(results) else 1


def write_config(path, url, token):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(CONFIG, encoding="utf-8") as fh:
        text = fh.read()
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text.replace('"http://192.168.1.10:8765"', '"%s"' % url).replace('"CHANGE_ME"', '"%s"' % token))


def built_config(maker):
    with zipfile.ZipFile(os.path.join(maker, "Balatro.exe")) as z:
        return z.read("balatro_sync_config.lua").decode("utf-8")


def config_lookup_tests(work, src, config):
    # Without --config the patcher must use the installed PC mod's config
    # (the one the PC actually syncs with), not the placeholder next to it.
    home = os.path.join(work, "home_cfg")
    os.makedirs(home)
    appdata = os.path.join(work, "appdata")
    installed = os.path.join(appdata, "Balatro", "Mods", "BalatroSync", "config.lua")
    env = dict(os.environ, HOME=home, APPDATA=appdata)

    print("12) Use the installed PC mod's config by default")
    write_config(installed, "http://10.0.0.7:8765", "installedtoken")
    out = os.path.join(work, "maker_cfg1")
    os.makedirs(out)
    res = subprocess.run([sys.executable, PATCHER, "--exe", src, "--out", out],
                         capture_output=True, text=True, env=env)
    check(res.returncode == 0, "patcher succeeds without --config" + ("" if res.returncode == 0 else ": " + res.stderr))
    check(os.path.exists(os.path.join(out, "Balatro.exe")) and '"http://10.0.0.7:8765"' in built_config(out)
          and '"installedtoken"' in built_config(out), "settings come from the installed mod's config")
    check("installed PC mod" in res.stdout and installed in res.stdout, "output says which config was used")

    print("13) --config wins over the installed config")
    out = os.path.join(work, "maker_cfg2")
    os.makedirs(out)
    res = subprocess.run([sys.executable, PATCHER, "--exe", src, "--out", out, "--config", config],
                         capture_output=True, text=True, env=env)
    check(res.returncode == 0 and '"http://10.0.0.5:8765"' in built_config(out), "explicit --config is used")

    print("14) Unconfigured installed config: refuse")
    write_config(installed, "http://192.168.1.10:8765", "CHANGE_ME")
    out = os.path.join(work, "maker_cfg3")
    os.makedirs(out)
    res = subprocess.run([sys.executable, PATCHER, "--exe", src, "--out", out],
                         capture_output=True, text=True, env=env)
    check(res.returncode != 0 and not os.path.exists(os.path.join(out, "Balatro.exe")),
          "placeholders everywhere: nothing is written")

    if sys.platform != "win32":
        print("15) Find the installed config inside the Proton prefix")
        proton_home = os.path.join(work, "home_proton")
        prefix_cfg = os.path.join(proton_home, ".steam", "steam", "steamapps", "compatdata", "2379780", "pfx",
                                  "drive_c", "users", "steamuser", "AppData", "Roaming", "Balatro", "Mods",
                                  "BalatroSync", "config.lua")
        write_config(prefix_cfg, "http://10.0.0.9:8765", "protontoken")
        out = os.path.join(work, "maker_cfg4")
        os.makedirs(out)
        env = dict(os.environ, HOME=proton_home)
        env.pop("APPDATA", None)
        res = subprocess.run([sys.executable, PATCHER, "--exe", src, "--out", out],
                             capture_output=True, text=True, env=env)
        check(res.returncode == 0 and '"protontoken"' in built_config(out), "Proton prefix config is used")


def find_with_home(home):
    """Run the patcher's Steam lookup with a fake home folder."""
    code = ("import importlib.util, sys; "
            "spec = importlib.util.spec_from_file_location('p', sys.argv[1]); "
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); "
            "print(m.find_steam_balatro() or '')")
    env = dict(os.environ, HOME=home)
    res = subprocess.run([sys.executable, "-c", code, PATCHER], capture_output=True, text=True, env=env)
    return res.stdout.strip()


def place_game(steam_root):
    path = os.path.join(steam_root, "steamapps", "common", "Balatro", "Balatro.exe")
    os.makedirs(os.path.dirname(path))
    make_fake_exe(path)
    return path


def linux_steam_tests(work, config):
    print("8) Find Balatro.exe in the usual Linux Steam folders")
    layouts = {
        "Debian/Ubuntu package": ".steam/debian-installation",
        "~/.steam/steam": ".steam/steam",
        "~/.local/share/Steam": ".local/share/Steam",
        "Flatpak": ".var/app/com.valvesoftware.Steam/.local/share/Steam",
    }
    for i, (label, rel) in enumerate(layouts.items()):
        home = os.path.join(work, "home%d" % i)
        expected = place_game(os.path.join(home, rel))
        found = find_with_home(home)
        check(os.path.realpath(found) == os.path.realpath(expected), "%s layout is found" % label)

    print("9) Follow libraryfolders.vdf to a second Steam library")
    home = os.path.join(work, "home_lib")
    root = os.path.join(home, ".local", "share", "Steam")
    os.makedirs(os.path.join(root, "steamapps"))
    library = os.path.join(work, "games_disk", "SteamLibrary")
    expected = place_game(library)
    with open(os.path.join(root, "steamapps", "libraryfolders.vdf"), "w", encoding="utf-8") as fh:
        fh.write('"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n'
                 '\t"1"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n}\n' % (root, library))
    check(os.path.realpath(find_with_home(home)) == os.path.realpath(expected), "game on a second library is found")

    print("10) Run without --exe and without Steam")
    empty_home = os.path.join(work, "home_empty")
    os.makedirs(empty_home)
    out = os.path.join(work, "maker5")
    os.makedirs(out)
    env = dict(os.environ, HOME=empty_home)
    res = subprocess.run([sys.executable, PATCHER, "--out", out, "--config", config],
                         capture_output=True, text=True, env=env)
    check(res.returncode != 0 and "--exe" in res.stderr and "/steamapps/" in res.stderr,
          "clear error with a Linux path example")

    print("11) Shell launcher")
    launcher = os.path.join(ROOT, "android", "make_patched_exe.sh")
    home = os.path.join(work, "home0")
    out = os.path.join(work, "maker6")
    os.makedirs(out)
    res = subprocess.run(["sh", launcher, "--out", out, "--device", "Deck", "--config", config],
                         capture_output=True, text=True, env=dict(os.environ, HOME=home))
    check(res.returncode == 0 and os.path.exists(os.path.join(out, "Balatro.exe")),
          "make_patched_exe.sh finds the game and patches it")


if __name__ == "__main__":
    sys.exit(main())
