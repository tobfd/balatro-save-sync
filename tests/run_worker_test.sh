#!/usr/bin/env bash
# Starts a throwaway sync server, runs the sync logic tests against it, stops it.
# Then runs the server's web login tests (tests/server_test.py, own server).
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA="$(mktemp -d)"
PORT="${PORT:-18766}"
PYTHON="${PYTHON:-python3}"
URL="http://127.0.0.1:$PORT"

"$PYTHON" --version
SYNC_TOKEN=testtoken SYNC_PORT="$PORT" SYNC_DATA_DIR="$DATA" "$PYTHON" -u "$ROOT/server/server.py" > "$DATA/server.log" 2>&1 &
SERVER_PID=$!

# Wait until the server answers (up to 20 s) instead of guessing a delay.
ready=0
for _ in $(seq 1 200); do
  if luajit -e "local h=require('socket.http'); h.TIMEOUT=0.5; local _, c = h.request('$URL/health'); os.exit(c == 200 and 0 or 1)" 2>/dev/null; then
    ready=1
    break
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    break
  fi
  sleep 0.1
done
if [ "$ready" != 1 ]; then
  echo "Server did not start. Log:"
  cat "$DATA/server.log"
  kill "$SERVER_PID" 2>/dev/null
  rm -rf "$DATA"
  exit 1
fi

luajit "$ROOT/tests/worker_test.lua" "$ROOT/pc-mod/BalatroSync/balatro_sync.lua" "$URL" testtoken
STATUS=$?
kill "$SERVER_PID" 2>/dev/null
echo "--- server log (tail)"
tail -n 12 "$DATA/server.log"
rm -rf "$DATA"

echo "--- web login tests"
"$PYTHON" "$ROOT/tests/server_test.py" || STATUS=1
exit $STATUS
