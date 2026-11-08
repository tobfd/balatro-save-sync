#!/usr/bin/env python3
"""
Balatro Save Sync - a tiny self-hosted sync server.

Keeps the latest version of every synced Balatro save file plus a version
history, so the PC mod and the patched mobile build can sync automatically
inside the home network.

Only the Python standard library is used. Configuration via environment:
  SYNC_TOKEN          shared secret, required (clients send it as X-Sync-Token)
  SYNC_PORT           listen port (default 8765)
  SYNC_DATA_DIR       storage folder (default /data)
  SYNC_HISTORY_KEEP   versions kept per file (default 50)

Sync API (devices send the token in the X-Sync-Token header):
  GET    /api/manifest                    one line per file: path, rev, md5 or "-", mtime, device
  GET    /api/file?path=P                 current content of P
  PUT    /api/file?path&base&hash&mtime&device[&force=1]   upload new content
  DELETE /api/file?path&base&mtime&device[&force=1]        mark P as deleted
  GET    /api/history?path=P              list of stored versions

Web page (log in once with the token, then a session cookie is used; the
token never appears in a URL):
  GET    /                                status page, or the login form
  POST   /login                           form field "token"; sets the cookie
  POST   /logout                          removes the cookie
  GET    /api/history/file?path=P&rev=R   download an old version (cookie or header)
  POST   /api/restore?path=P&rev=R        make an old version current (cookie or header)
  GET    /health                          "ok", no login needed (Docker healthcheck)
  An old bookmark like /?token=T still works: it logs in and redirects to /.

Uploads and deletes use optimistic concurrency: "base" must equal the
current revision of the file, otherwise the server answers 409 so the client
can re-evaluate instead of silently overwriting newer progress.
"""

import hashlib
import hmac
import html
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import CookieError, SimpleCookie
from urllib.parse import parse_qs, quote, urlparse

DATA_DIR = os.environ.get("SYNC_DATA_DIR", "/data")
TOKEN = os.environ.get("SYNC_TOKEN", "")
PORT = int(os.environ.get("SYNC_PORT", "8765"))
HISTORY_KEEP = max(1, int(os.environ.get("SYNC_HISTORY_KEEP", "50")))
MAX_BODY = 16 * 1024 * 1024
MAX_FORM = 4096

# The web page logs in with a cookie. Its value is derived from the token, so
# the cookie alone can't be used as the sync token, and a new token logs
# everyone out automatically.
COOKIE_NAME = "bsync_session"
COOKIE_MAX_AGE = 30 * 24 * 3600

# Only real Balatro save files can be synced: profile slots 1-9 and settings.
PATH_RE = re.compile(r"^(?:[1-9]/(?:profile|meta|save)\.jkr|settings\.jkr)$")
DEVICE_STRIP_RE = re.compile(r"[^A-Za-z0-9 ._-]")
HIST_NAME_RE = re.compile(r"^(\d{8})_(.*)\.jkr$")

LOCK = threading.Lock()


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def clean_device(name):
    name = DEVICE_STRIP_RE.sub("", name or "")[:32].strip()
    return name or "unknown"


def to_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


class Store:
    """File storage with a JSON index (meta.json) and per-file history."""

    def __init__(self, root):
        self.root = root
        self.files_dir = os.path.join(root, "files")
        self.hist_dir = os.path.join(root, "history")
        self.meta_path = os.path.join(root, "meta.json")
        os.makedirs(self.files_dir, exist_ok=True)
        os.makedirs(self.hist_dir, exist_ok=True)
        self.meta = {"seq": 0, "files": {}}
        if os.path.exists(self.meta_path):
            with open(self.meta_path, "r", encoding="utf-8") as fh:
                self.meta = json.load(fh)

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _write_atomic(target, data):
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)

    def _save_meta(self):
        data = json.dumps(self.meta, indent=1, sort_keys=True).encode("utf-8")
        self._write_atomic(self.meta_path, data)

    def _current_path(self, path):
        return os.path.join(self.files_dir, *path.split("/"))

    def _hist_folder(self, path):
        return os.path.join(self.hist_dir, path.replace("/", "_"))

    def _next_rev(self):
        self.meta["seq"] = int(self.meta.get("seq", 0)) + 1
        return self.meta["seq"]

    def _prune_history(self, folder):
        names = sorted(n for n in os.listdir(folder) if HIST_NAME_RE.match(n))
        for name in names[:-HISTORY_KEEP]:
            os.remove(os.path.join(folder, name))

    # -- public API ------------------------------------------------------
    def entry(self, path):
        return self.meta["files"].get(path)

    def all_entries(self):
        return sorted(self.meta["files"].items())

    def read_current(self, path):
        e = self.entry(path)
        if not e or not e.get("hash"):
            return None
        with open(self._current_path(path), "rb") as fh:
            return fh.read()

    def put(self, path, data, mtime, device):
        rev = self._next_rev()
        self._write_atomic(self._current_path(path), data)
        folder = self._hist_folder(path)
        slug = device.replace(" ", "-")
        self._write_atomic(os.path.join(folder, "%08d_%s.jkr" % (rev, slug)), data)
        self._prune_history(folder)
        entry = {
            "rev": rev,
            "hash": hashlib.md5(data).hexdigest(),
            "mtime": mtime,
            "device": device,
            "updated": int(time.time()),
            "size": len(data),
        }
        self.meta["files"][path] = entry
        self._save_meta()
        return entry

    def delete(self, path, mtime, device):
        rev = self._next_rev()
        current = self._current_path(path)
        if os.path.exists(current):
            os.remove(current)
        entry = {
            "rev": rev,
            "hash": None,
            "mtime": mtime,
            "device": device,
            "updated": int(time.time()),
            "size": 0,
        }
        self.meta["files"][path] = entry
        self._save_meta()
        return entry

    def history(self, path):
        folder = self._hist_folder(path)
        if not os.path.isdir(folder):
            return []
        result = []
        for name in sorted(os.listdir(folder), reverse=True):
            m = HIST_NAME_RE.match(name)
            if not m:
                continue
            full = os.path.join(folder, name)
            result.append({
                "rev": int(m.group(1)),
                "device": m.group(2),
                "size": os.path.getsize(full),
                "time": int(os.path.getmtime(full)),
            })
        return result

    def read_history(self, path, rev):
        folder = self._hist_folder(path)
        if not os.path.isdir(folder):
            return None
        prefix = "%08d_" % rev
        for name in os.listdir(folder):
            if name.startswith(prefix) and HIST_NAME_RE.match(name):
                with open(os.path.join(folder, name), "rb") as fh:
                    return fh.read()
        return None


def manifest_line(path, entry):
    if not entry:
        return "%s\t0\t-\t0\t\n" % path
    return "%s\t%d\t%s\t%d\t%s\n" % (
        path,
        entry["rev"],
        entry.get("hash") or "-",
        to_int(entry.get("mtime")),
        entry.get("device") or "",
    )


STORE = None  # set in main()


def session_value():
    return hmac.new(TOKEN.encode(), b"balatro-sync web session v1", hashlib.sha256).hexdigest()


def token_ok(given):
    return bool(TOKEN) and bool(given) and hmac.compare_digest(given.encode(), TOKEN.encode())


class Handler(BaseHTTPRequestHandler):
    server_version = "BalatroSync/1.1"

    # Silence the default per-request logging; we log meaningful events.
    def log_message(self, fmt, *args):
        pass

    # -- plumbing ---------------------------------------------------------
    def _query(self):
        parsed = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        return {k: v[0] for k, v in parsed.items()}

    def _route(self):
        return urlparse(self.path).path.rstrip("/") or "/"

    def _cookie_ok(self):
        try:
            jar = SimpleCookie(self.headers.get("Cookie") or "")
        except CookieError:
            return False
        morsel = jar.get(COOKIE_NAME)
        return bool(TOKEN) and morsel is not None and hmac.compare_digest(
            morsel.value.encode(), session_value().encode())

    def _authorized(self, allow_cookie=False):
        if token_ok(self.headers.get("X-Sync-Token") or ""):
            return True
        return allow_cookie and self._cookie_ok()

    def _login_cookie(self, max_age=COOKIE_MAX_AGE):
        value = session_value() if max_age else ""
        return "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict" % (COOKIE_NAME, value, max_age)

    def _redirect_home(self, cookie=None):
        headers = {"Location": "/"}
        if cookie:
            headers["Set-Cookie"] = cookie
        self._send(303, "", headers=headers)

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _read_body(self):
        length = to_int(self.headers.get("Content-Length"), -1)
        if length < 0 or length > MAX_BODY:
            return None
        data = b""
        while len(data) < length:
            chunk = self.rfile.read(length - len(data))
            if not chunk:
                return None
            data += chunk
        return data

    def _check(self, q, need_path=True, allow_cookie=False):
        if not self._authorized(allow_cookie):
            self._send(401, "unauthorized\n")
            return None
        if need_path:
            path = q.get("path", "")
            if not PATH_RE.match(path):
                self._send(400, "invalid path\n")
                return None
            return path
        return ""

    # -- GET ------------------------------------------------------------
    def do_GET(self):
        q = self._query()
        route = self._route()
        if route == "/":
            return self._status_page(q)
        if route == "/api/manifest":
            if self._check(q, need_path=False) is None:
                return
            with LOCK:
                body = "".join(manifest_line(p, e) for p, e in STORE.all_entries())
            return self._send(200, body)
        if route == "/api/file":
            path = self._check(q)
            if path is None:
                return
            with LOCK:
                entry = STORE.entry(path)
                data = STORE.read_current(path)
            if data is None:
                return self._send(404, "not found\n")
            return self._send(200, data, "application/octet-stream",
                              {"X-Rev": str(entry["rev"]), "X-Hash": entry["hash"]})
        if route == "/api/history":
            path = self._check(q)
            if path is None:
                return
            with LOCK:
                items = STORE.history(path)
            body = "".join("%d\t%s\t%d\t%d\n" % (i["rev"], i["device"], i["size"], i["time"]) for i in items)
            return self._send(200, body)
        if route == "/api/history/file":
            path = self._check(q, allow_cookie=True)
            if path is None:
                return
            with LOCK:
                data = STORE.read_history(path, to_int(q.get("rev"), -1))
            if data is None:
                return self._send(404, "not found\n")
            name = "%s_rev%s.jkr" % (path.replace("/", "_").replace(".jkr", ""), q.get("rev"))
            return self._send(200, data, "application/octet-stream",
                              {"Content-Disposition": 'attachment; filename="%s"' % name})
        if route == "/health":
            return self._send(200, "ok\n")
        self._send(404, "not found\n")

    # -- PUT / DELETE -------------------------------------------------------
    def do_PUT(self):
        q = self._query()
        if self._route() != "/api/file":
            return self._send(404, "not found\n")
        path = self._check(q)
        if path is None:
            return
        data = self._read_body()
        if data is None:
            return self._send(400, "missing or oversized body\n")
        digest = hashlib.md5(data).hexdigest()
        if q.get("hash") and q["hash"].lower() != digest:
            return self._send(400, "hash mismatch\n")
        base = to_int(q.get("base"), -1)
        force = q.get("force") == "1"
        device = clean_device(q.get("device"))
        mtime = to_int(q.get("mtime"), int(time.time()))
        with LOCK:
            entry = STORE.entry(path)
            current_rev = entry["rev"] if entry else 0
            if not force and base != current_rev:
                return self._send(409, manifest_line(path, entry))
            if entry and entry.get("hash") == digest:
                return self._send(200, "%d\n" % current_rev)
            entry = STORE.put(path, data, mtime, device)
        log("upload  %-14s rev %-5d %6d bytes from %s" % (path, entry["rev"], len(data), device))
        self._send(200, "%d\n" % entry["rev"])

    def do_DELETE(self):
        q = self._query()
        if self._route() != "/api/file":
            return self._send(404, "not found\n")
        path = self._check(q)
        if path is None:
            return
        base = to_int(q.get("base"), -1)
        force = q.get("force") == "1"
        device = clean_device(q.get("device"))
        mtime = to_int(q.get("mtime"), int(time.time()))
        with LOCK:
            entry = STORE.entry(path)
            current_rev = entry["rev"] if entry else 0
            if not force and base != current_rev:
                return self._send(409, manifest_line(path, entry))
            if not entry or not entry.get("hash"):
                return self._send(200, "%d\n" % current_rev)
            entry = STORE.delete(path, mtime, device)
        log("delete  %-14s rev %-5d from %s" % (path, entry["rev"], device))
        self._send(200, "%d\n" % entry["rev"])

    # -- POST -----------------------------------------------------------
    def do_POST(self):
        q = self._query()
        route = self._route()
        if route == "/login":
            return self._login()
        if route == "/logout":
            return self._redirect_home(self._login_cookie(max_age=0))
        if route != "/api/restore":
            return self._send(404, "not found\n")
        path = self._check(q, allow_cookie=True)
        if path is None:
            return
        rev = to_int(q.get("rev"), -1)
        with LOCK:
            data = STORE.read_history(path, rev)
            if data is None:
                return self._send(404, "version not found\n")
            entry = STORE.put(path, data, int(time.time()), "restore")
        log("restore %-14s rev %d -> new rev %d" % (path, rev, entry["rev"]))
        if q.get("redirect") == "1":
            return self._redirect_home()
        self._send(200, "%d\n" % entry["rev"])

    # -- login ------------------------------------------------------------
    def _login_page(self, code=200, error=""):
        message = '<p class="error">%s</p>' % html.escape(error) if error else ""
        body = PAGE_TEMPLATE % {"body": LOGIN_FORM % {"error": message}}
        self._send(code, body, "text/html; charset=utf-8")

    def _login(self):
        length = to_int(self.headers.get("Content-Length"), -1)
        if length < 0 or length > MAX_FORM:
            return self._send(400, "invalid form\n")
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        if token_ok((form.get("token") or [""])[0]):
            log("web login from %s" % self.client_address[0])
            return self._redirect_home(self._login_cookie())
        log("failed web login from %s" % self.client_address[0])
        self._login_page(401, "Wrong token.")

    # -- status page ----------------------------------------------------------
    def _status_page(self, q):
        if "token" in q:
            # Old bookmark with the token in the URL: log in and drop it from the address.
            if token_ok(q.get("token")):
                return self._redirect_home(self._login_cookie())
            return self._login_page(401, "Wrong token.")
        if not self._authorized(allow_cookie=True):
            return self._login_page()
        with LOCK:
            entries = STORE.all_entries()
            histories = {p: STORE.history(p) for p, _ in entries}
        rows = []
        for path, e in entries:
            state = "deleted" if not e.get("hash") else "%d bytes" % e.get("size", 0)
            hist_rows = []
            for h in histories[path]:
                restore = (
                    '<form method="post" action="%s"><button>Restore</button></form>'
                    % html.escape("/api/restore?path=%s&rev=%d&redirect=1" % (quote(path), h["rev"]))
                    if h["rev"] != e["rev"] else "<em>current</em>"
                )
                hist_rows.append(
                    "<tr><td>%d</td><td>%s</td><td>%s</td><td>%d bytes</td>"
                    '<td><a href="%s">download</a></td><td>%s</td></tr>'
                    % (h["rev"], html.escape(h["device"]), fmt_time(h["time"]), h["size"],
                       html.escape("/api/history/file?path=%s&rev=%d" % (quote(path), h["rev"])), restore)
                )
            rows.append(
                "<tr><td><code>%s</code></td><td>%d</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                '<tr class="hist"><td colspan="5"><details><summary>History (%d)</summary>'
                "<table>%s</table></details></td></tr>"
                % (html.escape(path), e["rev"], html.escape(e.get("device") or ""),
                   fmt_time(e.get("mtime")), state, len(hist_rows), "".join(hist_rows))
            )
        table = (
            "<table><tr><th>File</th><th>Rev</th><th>Device</th><th>Saved</th><th>State</th></tr>%s</table>"
            % "".join(rows) if rows else "<p>No saves uploaded yet.</p>"
        )
        self._send(200, PAGE_TEMPLATE % {"body": STATUS_HEADER + table},
                   "text/html; charset=utf-8")


def fmt_time(ts):
    ts = to_int(ts)
    if ts <= 0:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


PAGE_TEMPLATE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Balatro Save Sync</title>
<style>
body{font-family:system-ui,sans-serif;background:#1e2a2e;color:#e8ecec;margin:0;padding:16px}
h1{font-size:20px} table{border-collapse:collapse;width:100%%;max-width:900px}
td,th{padding:6px 8px;text-align:left;border-bottom:1px solid #35464b;font-size:14px}
tr.hist td{border-bottom:2px solid #4a5f66} details table td{font-size:13px}
a{color:#7fc4ff} button{background:#e05a47;color:#fff;border:0;border-radius:4px;padding:3px 8px;cursor:pointer}
form{margin:0} input{padding:6px} .error{color:#ff8a7a}
.top{display:flex;justify-content:space-between;align-items:center;max-width:900px}
.top button{background:#35464b}
</style></head><body>%(body)s</body></html>"""

LOGIN_FORM = """<h1>Balatro Save Sync</h1>%(error)s<form method="post" action="/login">
<input type="password" name="token" placeholder="Sync token" size="36" autocomplete="current-password" autofocus>
<button>Log in</button></form>"""

STATUS_HEADER = """<div class="top"><h1>Balatro Save Sync</h1>
<form method="post" action="/logout"><button>Log out</button></form></div>"""


def main():
    global STORE
    if not TOKEN or TOKEN == "CHANGE_ME":
        log("SYNC_TOKEN is not set - set your own token in compose.yaml.")
        sys.exit(1)
    STORE = Store(DATA_DIR)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    log("Balatro Save Sync listening on port %d, data in %s" % (PORT, DATA_DIR))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
