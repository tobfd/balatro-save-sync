#!/usr/bin/env python3
"""
Builds a copy of Balatro.exe with BalatroSync built in, for balatro-mobile-maker.

balatro-mobile-maker uses a Balatro.exe that sits in its own folder instead of
the one in your Steam folder. This script puts a patched copy there; then you
run balatro-mobile-maker as usual and it builds, signs and installs the APK.
Your Steam installation is never modified.

Usage:
  Windows:      python make_patched_exe.py --out "C:\\path\\to\\balatro-mobile-maker"
  Linux/macOS:  python3 make_patched_exe.py --out ~/balatro-mobile-maker
Options:
  --exe PATH      Balatro.exe to start from (default: found via Steam)
  --out DIR       balatro-mobile-maker folder (asked if missing)
  --device NAME   device name shown in sync messages (default: Tablet)
  --config PATH   BalatroSync config.lua to use. Default: the config of the installed
                  PC mod (%APPDATA%\\Balatro\\Mods\\BalatroSync, or inside the Proton
                  prefix on Linux), else ../pc-mod/BalatroSync/config.lua
"""

import argparse
import io
import os
import re
import shutil
import struct
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
MOD_DIR = os.path.join(HERE, "..", "pc-mod", "BalatroSync")
BOOT_LINE = 'require("balatro_sync").install(require("balatro_sync_config"))'
EOCD_SIG = b"PK\x05\x06"

# Placeholders shipped in the release's config.lua.
EXAMPLE_HOST = "192.168.1.10:"
PLACEHOLDER_TOKEN = "CHANGE_ME"
BALATRO_APP_ID = "2379780"

# Where Steam lives on Linux, relative to the home folder. Debian/Ubuntu packages
# use ~/.steam/debian-installation, ~/.steam/steam and ~/.steam/root are symlinks
# most distributions create, Flatpak keeps everything inside its sandbox folder.
LINUX_STEAM_ROOTS = (
    ".steam/steam",
    ".steam/root",
    ".steam/debian-installation",
    ".local/share/Steam",
    ".var/app/com.valvesoftware.Steam/.local/share/Steam",
    ".var/app/com.valvesoftware.Steam/.steam/steam",
)


def steam_libraries():
    """Steam install folders plus all library folders listed in libraryfolders.vdf."""
    steam_roots = []
    if sys.platform == "win32":
        try:
            import winreg
            for hive, key in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                              (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam")):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        for value in ("SteamPath", "InstallPath"):
                            try:
                                steam_roots.append(winreg.QueryValueEx(k, value)[0])
                            except OSError:
                                pass
                except OSError:
                    pass
        except ImportError:
            pass
        steam_roots.append(r"C:\Program Files (x86)\Steam")
    else:
        home = os.path.expanduser("~")
        steam_roots.extend(os.path.join(home, rel) for rel in LINUX_STEAM_ROOTS)

    libraries = []
    for root in steam_roots:
        root = os.path.normpath(root)
        if root in libraries:
            continue
        libraries.append(root)
        vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
        if os.path.exists(vdf):
            with open(vdf, encoding="utf-8", errors="ignore") as fh:
                for match in re.finditer(r'"path"\s+"([^"]+)"', fh.read()):
                    library = os.path.normpath(match.group(1).replace("\\\\", "\\"))
                    if library not in libraries:
                        libraries.append(library)
    return libraries


def find_steam_balatro():
    """Locate Balatro.exe through the Steam install and its library folders."""
    for lib in steam_libraries():
        path = os.path.join(lib, "steamapps", "common", "Balatro", "Balatro.exe")
        if os.path.isfile(path):
            return path
    return None


def installed_mod_configs():
    """Where the installed PC mod keeps its config.lua - the one the PC syncs with."""
    paths = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        paths.append(os.path.join(appdata, "Balatro", "Mods", "BalatroSync", "config.lua"))
    if sys.platform != "win32":
        # Proton: %AppData% lives in the game's prefix inside its Steam library.
        for lib in steam_libraries():
            paths.append(os.path.join(lib, "steamapps", "compatdata", BALATRO_APP_ID, "pfx", "drive_c",
                                      "users", "steamuser", "AppData", "Roaming", "Balatro", "Mods",
                                      "BalatroSync", "config.lua"))
    return paths


def config_values(text):
    url = re.search(r'server_url\s*=\s*"([^"]*)"', text)
    token = re.search(r'token\s*=\s*"([^"]*)"', text)
    return (url.group(1) if url else ""), (token.group(1) if token else "")


def config_problems(url, token):
    problems = []
    if not url:
        problems.append("server_url is empty")
    elif EXAMPLE_HOST in url:
        problems.append("server_url still has the example IP")
    if not token or token == PLACEHOLDER_TOKEN:
        problems.append("token is not set")
    return problems


def choose_config(explicit):
    """Returns (path, where it came from). Prefers the installed PC mod's config."""
    if explicit:
        return explicit, "given with --config"
    for path in installed_mod_configs():
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                if not config_problems(*config_values(fh.read())):
                    return path, "installed PC mod"
    return os.path.join(MOD_DIR, "config.lua"), "next to this script"


def zip_layout(data):
    """Return (prefix_length, relative_offsets) for a zip appended to an exe."""
    pos = data.rfind(EOCD_SIG)
    if pos < 0:
        raise ValueError("no zip archive found inside the file")
    cd_size, cd_offset = struct.unpack("<II", data[pos + 12:pos + 20])
    zip_start = pos - cd_size - cd_offset
    if zip_start > 0:
        return zip_start, True  # offsets count from the start of the zip part
    # offsets already count from the start of the file
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        first = min(info.header_offset for info in zf.infolist())
    return first, False


def read_config(path, device):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    new_text, count = re.subn(r'(device_name\s*=\s*)"[^"]*"', r'\g<1>"%s"' % device.replace('"', ""), text)
    if count != 1:
        raise ValueError("could not find device_name in " + path)
    url, token = config_values(new_text)
    return new_text, url, token


def patch_main(source):
    text = source.decode("utf-8")
    if BOOT_LINE in text:
        return source, False
    bom = ""
    if text.startswith("\ufeff"):
        bom, text = "\ufeff", text[1:]
    return (bom + BOOT_LINE + "\n" + text).encode("utf-8"), True


def build(exe_path, out_dir, device, config_path):
    config_text, url, token = read_config(config_path, device)
    problems = config_problems(url, token)
    if problems:
        raise ValueError("%s in %s - nothing was written. Edit that file, or point to your configured "
                         "one with --config (usually %%APPDATA%%\\Balatro\\Mods\\BalatroSync\\config.lua)."
                         % (" and ".join(problems), os.path.abspath(config_path)))

    with open(exe_path, "rb") as fh:
        data = fh.read()
    prefix_len, relative = zip_layout(data)
    prefix = data[:prefix_len]

    with open(os.path.join(MOD_DIR, "balatro_sync.lua"), "rb") as fh:
        client = fh.read()

    out = io.BytesIO()
    if not relative:
        out.write(prefix)
    names = set()
    patched_main = False
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "a" if not relative else "w",
                                                                     zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            names.add(info.filename)
            if info.filename in ("balatro_sync.lua", "balatro_sync_config.lua"):
                continue  # replaced below (re-running the script is fine)
            content = src.read(info.filename)
            if info.filename == "main.lua":
                content, patched_main = patch_main(content)
            new_info = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            new_info.external_attr = info.external_attr
            new_info.compress_type = zipfile.ZIP_STORED if info.is_dir() else zipfile.ZIP_DEFLATED
            dst.writestr(new_info, content)
        if "main.lua" not in names:
            raise ValueError("main.lua not found - is this really Balatro.exe?")
        dst.writestr("balatro_sync.lua", client)
        dst.writestr("balatro_sync_config.lua", config_text.encode("utf-8"))

    result = (prefix + out.getvalue()) if relative else out.getvalue()

    # Sanity check before writing anything.
    with zipfile.ZipFile(io.BytesIO(result)) as check:
        main = check.read("main.lua").decode("utf-8").lstrip("\ufeff")
        assert main.startswith(BOOT_LINE), "main.lua was not patched"
        assert check.read("balatro_sync.lua") == client
        assert check.testzip() is None

    os.makedirs(out_dir, exist_ok=True)
    target = os.path.join(out_dir, "Balatro.exe")
    if os.path.abspath(target) == os.path.abspath(exe_path):
        raise ValueError("refusing to overwrite the source Balatro.exe - choose another --out folder")
    if os.path.exists(target):
        backup = os.path.join(out_dir, "Balatro.unpatched.exe")
        with open(target, "rb") as fh:
            already_patched = b"balatro_sync.lua" in fh.read()
        if not os.path.exists(backup) and not already_patched:
            shutil.copy2(target, backup)
    with open(target, "wb") as fh:
        fh.write(result)
    return target, url, token, patched_main


def main():
    parser = argparse.ArgumentParser(description="Patch Balatro.exe with BalatroSync for balatro-mobile-maker")
    parser.add_argument("--exe", help="source Balatro.exe (default: Steam install)")
    parser.add_argument("--out", help="balatro-mobile-maker folder")
    parser.add_argument("--device", default="Tablet", help="device name (default: Tablet)")
    parser.add_argument("--config", help="BalatroSync config.lua (default: the installed PC mod's config)")
    args = parser.parse_args()

    exe = args.exe or find_steam_balatro()
    if not exe or not os.path.isfile(exe):
        example = ("...\\steamapps\\common\\Balatro\\Balatro.exe" if sys.platform == "win32"
                   else ".../steamapps/common/Balatro/Balatro.exe")
        sys.exit('Balatro.exe not found. Pass it with --exe "%s"' % example)
    out_dir = args.out
    if not out_dir:
        out_dir = input("Folder of balatro-mobile-maker: ").strip().strip('"')
    out_dir = os.path.expanduser(out_dir)
    if not out_dir or not os.path.isdir(out_dir):
        sys.exit("Folder not found: %r" % out_dir)

    config_path, config_source = choose_config(args.config)
    target, url, token, patched = build(exe, out_dir, args.device, config_path)
    print("Source:       " + exe)
    print("Config:       %s (%s)" % (os.path.abspath(config_path), config_source))
    print("Written:      " + target + ("" if patched else "  (main.lua was already patched)"))
    print("Device name:  " + args.device)
    print("Server:       " + url)
    print("\nNext: run balatro-mobile-maker in that folder as usual.")
    print("  - answer 'N' when it offers to transfer saves (sync does that now)")
    print("  - let it install the APK, or copy balatro.apk to the tablet and install it as an update")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, zipfile.BadZipFile) as exc:
        sys.exit("Error: %s" % exc)
