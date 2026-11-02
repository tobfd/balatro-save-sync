#!/usr/bin/env bash
# Starts a throwaway sync server, runs the worker test against it, stops it.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA="$(mktemp -d)"
PORT="${PORT:-18766}"
SYNC_TOKEN=testtoken SYNC_PORT="$PORT" SYNC_DATA_DIR="$DATA" "${PYTHON:-python3}" "$ROOT/server/server.py" > "$DATA/server.log" 2>&1 &
SERVER_PID=$!
sleep 1
luajit "$ROOT/tests/worker_test.lua" "$ROOT/pc-mod/BalatroSync/balatro_sync.lua" "http://127.0.0.1:$PORT" testtoken
STATUS=$?
kill "$SERVER_PID" 2>/dev/null
echo "--- server log (tail)"
tail -n 12 "$DATA/server.log"
rm -rf "$DATA"
exit $STATUS
