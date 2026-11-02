#!/usr/bin/env python3
"""
Real-engine test: runs a fake Balatro in LOVE 11.5 (under Xvfb) with the
BalatroSync client injected exactly like the patcher does, against a real
sync server. Scenarios cover first upload, pulling on start, stale detection
while running, force pull after restart, and the mobile auto-reload.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
E2E = os.path.join(ROOT, "tests", "love_e2e")
CLIENT = os.path.join(ROOT, "pc-mod", "BalatroSync", "balatro_sync.lua")
BOOT = 'require("balatro_sync").install(require("balatro_sync_config"))\n'
TOKEN = "e2etoken"
PORT = 18767
URL = "http://127.0.0.1:%d" % PORT

work = tempfile.mkdtemp(prefix="bsync_e2e_")
game = os.path.join(work, "game")
data_home = os.path.join(work, "xdg")
os.makedirs(game)
shutil.copy(CLIENT, game)
shutil.copy(os.path.join(E2E, "conf.lua"), game)
shutil.copy(os.path.join(E2E, "balatro_sync_config.lua"), game)
with open(os.path.join(E2E, "game_main.lua")) as fh:
    main_src = fh.read()
with open(os.path.join(game, "main.lua"), "w") as fh:
    fh.write(BOOT + main_src)

results = []


def check(cond, label):
    results.append(bool(cond))
    print(("  ok   " if cond else "  FAIL ") + label, flush=True)


def save_dir(identity):
    return os.path.join(data_home, "love", identity)


def seed(identity, progress):
    d = os.path.join(save_dir(identity), "1")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "profile.jkr"), "w") as fh:
        fh.write("return {progress=%d}" % progress)


def server_profile():
    req = urllib.request.Request(URL + "/api/file?path=1%2Fprofile.jkr", headers={"X-Sync-Token": TOKEN})
    body = urllib.request.urlopen(req, timeout=3).read().decode()
    return int(re.search(r"progress=(\d+)", body).group(1))


def server_manifest():
    req = urllib.request.Request(URL + "/api/manifest", headers={"X-Sync-Token": TOKEN})
    return urllib.request.urlopen(req, timeout=3).read().decode()


def run_game(identity, device, seconds, android=False, shot_at=None, shot_file=None, mode="active", focus_at=None):
    env = dict(os.environ)
    env.update({
        "DISPLAY": ":97",
        "XDG_DATA_HOME": data_home,
        "FB_IDENTITY": identity,
        "DEVICE": device,
        "SYNC_URL": URL,
        "SYNC_TOKEN": TOKEN,
        "QUIT_AT": str(int(time.time()) + seconds),
        "LIBGL_ALWAYS_SOFTWARE": "1",
        "MODE": mode,
    })
    if android:
        env["FAKE_ANDROID"] = "1"
    if focus_at:
        env["FOCUS_AT"] = str(int(time.time()) + focus_at)
    if shot_at:
        env["SHOT_AT"] = str(int(time.time()) + shot_at)
        env["SHOT_FILE"] = shot_file
    return subprocess.Popen(["love", game], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def finish(proc, timeout=40):
    out, _ = proc.communicate(timeout=timeout)
    return out


def loaded(out):
    return [int(x) for x in re.findall(r"\[Fake\] loaded progress (\d+)", out)]


def quit_progress(out):
    vals = [int(x) for x in re.findall(r"\[Fake\] quitting with progress (\d+)", out)]
    return vals[-1] if vals else None


xvfb = subprocess.Popen(["Xvfb", ":97", "-screen", "0", "1024x768x24"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
server_data = os.path.join(work, "server")
server = subprocess.Popen(
    [sys.executable, os.path.join(ROOT, "server", "server.py")],
    env=dict(os.environ, SYNC_TOKEN=TOKEN, SYNC_PORT=str(PORT), SYNC_DATA_DIR=server_data),
    stdout=open(os.path.join(work, "server.log"), "w"), stderr=subprocess.STDOUT)
time.sleep(1.5)

try:
    print("A) PC with existing progress uploads it, final save is flushed on quit")
    seed("fb_pc", 100)
    out = finish(run_game("fb_pc", "PC", 5))
    print("   " + "\n   ".join(l for l in out.splitlines() if "BalatroSync" in l or "[Fake]" in l))
    check(loaded(out) == [100], "PC loaded its local progress 100")
    pc_final = quit_progress(out)
    check(pc_final and server_profile() == pc_final, "server has PC's final progress (%s)" % pc_final)

    print("B) Empty tablet pulls PC progress before the game reads it")
    out = finish(run_game("fb_tab", "Tablet", 4))
    check(loaded(out) == [pc_final], "tablet loaded %s at start (got %s)" % (pc_final, loaded(out)))
    tab_final = quit_progress(out)
    check(server_profile() == tab_final, "server has tablet's final progress (%s)" % tab_final)

    print("C) PC left open (rewriting idle saves) while tablet plays: PC goes stale, never overwrites")
    shot = os.path.join(work, "pc_stale.png")
    pc = run_game("fb_pc", "PC", 14, shot_at=12, shot_file=shot, mode="idle_rewrite")
    time.sleep(3)
    tab = run_game("fb_tab", "Tablet", 5)
    tab_out = finish(tab)
    tab_final = quit_progress(tab_out)
    pc_out = finish(pc)
    check("newer save from Tablet" in pc_out, "PC detected newer save from Tablet")
    check(server_profile() == tab_final, "server still has tablet's progress %s (PC did not overwrite)" % tab_final)
    check(os.path.exists(shot), "overlay screenshot captured")
    if os.path.exists(shot):
        shutil.copy(shot, os.path.join(ROOT, "tests", "pc_stale_overlay.png"))

    print("D) PC restart pulls the tablet progress (force pull)")
    out = finish(run_game("fb_pc", "PC", 3))
    check(loaded(out) == [tab_final], "PC loaded tablet progress %s (got %s)" % (tab_final, loaded(out)))
    pc_final = quit_progress(out)

    print("E) Mobile: tablet left open (idle rewrites), PC played -> tablet auto-reloads with PC progress")
    tab = run_game("fb_tab", "Tablet", 24, android=True, mode="idle_rewrite", focus_at=1)
    time.sleep(3)
    pc_out = finish(run_game("fb_pc", "PC", 4))
    pc_final = quit_progress(pc_out)
    tab_out = finish(tab)
    lp = loaded(tab_out)
    print("   tablet loads:", lp, "pc final:", pc_final, "server:", server_profile())
    if os.environ.get("E2E_VERBOSE"):
        print("---- PC output\n" + pc_out + "\n---- tablet output\n" + tab_out)
    check(len(lp) >= 2, "tablet reloaded itself (loads: %s)" % lp)
    check(lp and lp[-1] == pc_final, "after reload tablet has PC's final progress %s" % pc_final)
    check(server_profile() == pc_final, "server keeps PC progress (idle tablet never overwrote it)")
    check(quit_progress(tab_out) == pc_final, "tablet ends the session on PC progress")
    print("   server manifest:", server_manifest().strip())
finally:
    server.terminate()
    xvfb.terminate()
    print("\nserver log:\n" + open(os.path.join(work, "server.log")).read()[-1500:])

print("\n%d passed, %d failed" % (sum(results), len(results) - sum(results)))
sys.exit(0 if all(results) else 1)
