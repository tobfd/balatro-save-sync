#!/usr/bin/env python3
"""
Tests the sync server's web login and authentication rules.

Starts its own server on a free port with a temporary data folder.
Usage: python tests/server_test.py
"""

import http.client
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "server", "server.py")
TOKEN = "s3cret-test-token"

results = []


def check(cond, label):
    results.append(bool(cond))
    print(("  ok   " if cond else "  FAIL ") + label, flush=True)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Client:
    def __init__(self, port):
        self.port = port

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        res = conn.getresponse()
        data = res.read()
        conn.close()
        return res.status, {k.lower(): v for k, v in res.getheaders()}, data


def session_cookie(set_cookie):
    match = re.match(r"bsync_session=([^;]*)", set_cookie or "")
    return match.group(1) if match else None


def main():
    port = free_port()
    data_dir = tempfile.mkdtemp(prefix="bsync_server_")
    env = dict(os.environ, SYNC_TOKEN=TOKEN, SYNC_PORT=str(port), SYNC_DATA_DIR=data_dir)
    proc = subprocess.Popen([sys.executable, "-u", SERVER], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    c = Client(port)
    try:
        for _ in range(200):
            try:
                if c.request("GET", "/health")[0] == 200:
                    break
            except OSError:
                time.sleep(0.1)
        else:
            print(proc.stdout.read())
            raise SystemExit("server did not start")

        token_header = {"X-Sync-Token": TOKEN}
        form = {"Content-Type": "application/x-www-form-urlencoded"}

        print("1) Login page")
        status, _, body = c.request("GET", "/")
        page = body.decode()
        check(status == 200 and 'action="/login"' in page and 'type="password"' in page,
              "without login / shows a POST login form with a password field")

        print("2) Wrong token")
        status, headers, body = c.request("POST", "/login", urlencode({"token": "nope"}), form)
        check(status == 401 and "set-cookie" not in headers, "wrong token: 401 and no cookie")
        check("Wrong token" in body.decode(), "wrong token: error message shown")

        print("3) Right token")
        status, headers, _ = c.request("POST", "/login", urlencode({"token": TOKEN}), form)
        cookie = session_cookie(headers.get("set-cookie"))
        check(status == 303 and headers.get("location") == "/", "login redirects to /")
        check(cookie and cookie != TOKEN and TOKEN not in headers.get("set-cookie", ""),
              "cookie is set and does not contain the token")
        attrs = headers.get("set-cookie", "")
        check("HttpOnly" in attrs and "SameSite=Strict" in attrs and "Path=/" in attrs,
              "cookie is HttpOnly and SameSite=Strict")
        jar = {"Cookie": "bsync_session=" + (cookie or "")}

        print("4) Status page with the cookie")
        c.request("PUT", "/api/file?path=1%2Fprofile.jkr&base=0&mtime=1&device=PC", b"return {v=1}", token_header)
        c.request("PUT", "/api/file?path=1%2Fprofile.jkr&base=1&mtime=2&device=PC", b"return {v=2}", token_header)
        status, _, body = c.request("GET", "/", headers=jar)
        page = body.decode()
        check(status == 200 and "1/profile.jkr" in page and "Log out" in page, "status page lists the files")
        check(TOKEN not in page and "token=" not in page, "the page contains no token and no token= links")
        check('action="/api/restore?path=1/profile.jkr&amp;rev=1&amp;redirect=1"' in page, "restore form without token")
        check('href="/api/history/file?path=1/profile.jkr&amp;rev=1"' in page, "download link without token")

        print("5) Old bookmark with ?token=")
        status, headers, _ = c.request("GET", "/?token=" + TOKEN)
        check(status == 303 and headers.get("location") == "/" and session_cookie(headers.get("set-cookie")) == cookie,
              "/?token= logs in and redirects to / (token leaves the address bar)")
        status, headers, _ = c.request("GET", "/?token=wrong")
        check(status == 401 and "set-cookie" not in headers, "/?token=wrong is refused")

        print("6) Sync API accepts only the header")
        check(c.request("GET", "/api/manifest", headers=token_header)[0] == 200, "header works")
        check(c.request("GET", "/api/manifest", headers=jar)[0] == 401, "cookie is not enough for the sync API")
        check(c.request("GET", "/api/manifest?token=" + TOKEN)[0] == 401, "?token= no longer works for the API")
        check(c.request("PUT", "/api/file?path=1%2Fmeta.jkr&base=0", b"return {}", jar)[0] == 401,
              "cookie cannot upload")

        print("7) Download and restore from the page")
        status, _, body = c.request("GET", "/api/history/file?path=1%2Fprofile.jkr&rev=1", headers=jar)
        check(status == 200 and body == b"return {v=1}", "download of an old version with the cookie")
        check(c.request("GET", "/api/history/file?path=1%2Fprofile.jkr&rev=1")[0] == 401, "download without login: 401")
        status, headers, _ = c.request("POST", "/api/restore?path=1%2Fprofile.jkr&rev=1&redirect=1", b"", jar)
        check(status == 303 and headers.get("location") == "/", "restore redirects to / without token")
        status, _, body = c.request("GET", "/api/file?path=1%2Fprofile.jkr", headers=token_header)
        check(body == b"return {v=1}", "restored version is current")
        check(c.request("POST", "/api/restore?path=1%2Fprofile.jkr&rev=2&redirect=1", b"")[0] == 401,
              "restore without login: 401")

        print("8) Forged cookie and logout")
        fake = {"Cookie": "bsync_session=" + "0" * 64}
        status, _, body = c.request("GET", "/", headers=fake)
        check(status == 200 and 'action="/login"' in body.decode(), "forged cookie gets the login form")
        status, headers, _ = c.request("POST", "/logout", b"", jar)
        check(status == 303 and "Max-Age=0" in headers.get("set-cookie", ""), "logout clears the cookie")

        print("9) Health check")
        check(c.request("GET", "/health")[0] == 200, "/health answers without login")
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        shutil.rmtree(data_dir, ignore_errors=True)

    print("\n%d passed, %d failed" % (sum(results), len(results) - sum(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
