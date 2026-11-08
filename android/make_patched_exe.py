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
  --config PATH   BalatroSync config.lua to use (default: ../pc-mod/BalatroSync/config.lua)
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


def find_steam_balatro():
    """Locate Balatro.exe through the Steam install and its library folders."""
    candidates = []
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
                    libraries.append(match.group(1).replace("\\\\", "\\"))
    for lib in libraries:
        candidates.append(os.path.join(lib, "steamapps", "common", "Balatro", "Balatro.exe"))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


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
    url = re.search(r'server_url\s*=\s*"([^"]*)"', new_text)
    token = re.search(r'token\s*=\s*"([^"]*)"', new_text)
    return new_text, (url.group(1) if url else ""), (token.group(1) if token else "")


def patch_main(source):
    text = source.decode("utf-8")
    if BOOT_LINE in text:
        return source, False
    bom = ""
    if text.startswith("\ufeff"):
        bom, text = "\ufeff", text[1:]
    return (bom + BOOT_LINE + "\n" + text).encode("utf-8"), True


def build(exe_path, out_dir, device, config_path):
    with open(exe_path, "rb") as fh:
        data = fh.read()
    prefix_len, relative = zip_layout(data)
    prefix = data[:prefix_len]

    with open(os.path.join(MOD_DIR, "balatro_sync.lua"), "rb") as fh:
        client = fh.read()
    config_text, url, token = read_config(config_path, device)

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
    parser.add_argument("--config", default=os.path.join(MOD_DIR, "config.lua"), help="BalatroSync config.lua")
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

    target, url, token, patched = build(exe, out_dir, args.device, args.config)
    print("Source:       " + exe)
    print("Written:      " + target + ("" if patched else "  (main.lua was already patched)"))
    print("Device name:  " + args.device)
    print("Server:       " + url)
    if "192.168.1.10:" in url:
        print("\nNOTE: server_url still has the example IP - edit config.lua and run this again!")
    if not token or token == "CHANGE_ME":
        print("\nNOTE: token in config.lua is not set - edit it and run this again!")
    print("\nNext: run balatro-mobile-maker in that folder as usual.")
    print("  - answer 'N' when it offers to transfer saves (sync does that now)")
    print("  - let it install the APK, or copy balatro.apk to the tablet and install it as an update")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, zipfile.BadZipFile) as exc:
        sys.exit("Error: %s" % exc)
