#!/usr/bin/env bash
# End-to-end smoke test WITHOUT Discord: starts the real stack (Xvfb, PulseAudio, openbox, x11vnc,
# web gateway) with a fake app that opens a window and plays a 440 Hz tone, then checks login,
# the VNC WebSocket proxy, the live audio stream and shutdown. Needs the dependencies from install.sh.
set -euo pipefail

PORT="${SMOKE_PORT:-6180}"
ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
WORK="$(mktemp -d)"
chmod 755 "$WORK"
BASE="http://127.0.0.1:$PORT"
PASS=0
FAIL=0
DDA_PID=""

check() { # check "description" command...
  local desc="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    echo "  ok    $desc"
    PASS=$((PASS + 1))
  else
    echo "  FAIL  $desc"
    FAIL=$((FAIL + 1))
  fi
}

cleanup() {
  if [ -n "$DDA_PID" ] && kill -0 "$DDA_PID" 2>/dev/null; then
    kill -TERM "$DDA_PID" 2>/dev/null || true
    wait "$DDA_PID" 2>/dev/null || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

cat >"$WORK/fakeapp.sh" <<'APP'
#!/usr/bin/env bash
command -v xeyes >/dev/null 2>&1 && xeyes &
exec ffmpeg -hide_banner -loglevel error -re -f lavfi -i "sine=frequency=440" -f pulse -device dda "fake-discord"
APP
chmod +x "$WORK/fakeapp.sh"

echo "Starting the stack on port $PORT ..."
(
  cd "$ROOT"
  DDA_DATA_DIR="$WORK/data" PORT="$PORT" HOST=127.0.0.1 VNC_PASSWORD=smoke SCREENSHOT=0 \
    APP_CMD="$WORK/fakeapp.sh" DISPLAY_NUM="${SMOKE_DISPLAY:-98}" exec python3 -m dda run >"$WORK/run.log" 2>&1
) &
DDA_PID=$!

for _ in $(seq 1 60); do
  curl -sf -m 2 "$BASE/healthz" >/dev/null 2>&1 && break
  sleep 0.5
done
sleep 3

echo "Checks:"
check "health endpoint is public" curl -sf -m 5 "$BASE/healthz"
check "API refuses anonymous requests" test "$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$BASE/api/status")" = 401
check "wrong password is rejected" test "$(curl -s -m 5 -o /dev/null -w '%{http_code}' -d 'password=nope' "$BASE/login")" = 401
curl -s -m 5 -c "$WORK/cookies" -o /dev/null -d 'password=smoke&next=/' "$BASE/login"
check "login sets a session cookie" grep -q dda_session "$WORK/cookies"
check "desktop page is served" curl -sf -m 5 -b "$WORK/cookies" "$BASE/"
check "noVNC client is served" curl -sf -m 5 -b "$WORK/cookies" "$BASE/novnc/vnc.html"
check "status reports Discord running" bash -c "curl -sf -m 5 -b '$WORK/cookies' '$BASE/api/status' | grep -q '\"discord\": true'"
check "audio is reported available" bash -c "curl -sf -m 5 -b '$WORK/cookies' '$BASE/api/status' | grep -q '\"available\": true'"

COOKIE="$(awk '/dda_session/ {print $NF}' "$WORK/cookies")"
check "WebSocket proxy reaches the real VNC server (RFB banner)" env COOKIE="$COOKIE" PORT="$PORT" ROOT="$ROOT" \
  timeout 20 python3 - <<'PY'
import base64, os, socket, sys
sys.path.insert(0, os.environ["ROOT"])
from dda import wsproto
port = int(os.environ["PORT"])
s = socket.create_connection(("127.0.0.1", port), timeout=5)
key = base64.b64encode(os.urandom(16)).decode()
s.sendall((f"GET /websockify HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: binary\r\n"
           f"Cookie: dda_session={os.environ['COOKIE']}\r\n\r\n").encode())
buf = b""
while b"\r\n\r\n" not in buf:
    buf += s.recv(4096)
head, _, rest = buf.partition(b"\r\n\r\n")
assert b" 101 " in head.split(b"\r\n")[0], head
banner = wsproto.WebSocket(s, client=True, initial=rest).recv_data()[1]
assert banner.startswith(b"RFB 003."), banner
PY

curl -s -m 4 -b "$WORK/cookies" -o "$WORK/audio.mp3" "$BASE/audio" || true
check "audio stream returns data" test "$(stat -c %s "$WORK/audio.mp3")" -gt 10000
check "audio stream decodes as MP3" bash -c "ffprobe -v error -show_entries stream=codec_name -of default=nw=1:nk=1 '$WORK/audio.mp3' | grep -q mp3"
check "audio contains the 440 Hz test tone" bash -c "ffmpeg -v error -i '$WORK/audio.mp3' -ac 1 -ar 8000 -t 3 -f s16le - | python3 -c \"
import sys, struct, cmath, math
d = sys.stdin.buffer.read(); s = struct.unpack('<%dh' % (len(d) // 2), d)[4000:8000]
best = max(range(100, 2000, 10), key=lambda f: abs(sum(s[i] * cmath.exp(-2j * math.pi * f * i / 8000) for i in range(0, len(s), 2))))
sys.exit(0 if abs(best - 440) <= 20 else 1)\""

kill -TERM "$DDA_PID"
for _ in $(seq 1 20); do kill -0 "$DDA_PID" 2>/dev/null || break; sleep 0.5; done
check "supervisor exits after SIGTERM" bash -c "! kill -0 $DDA_PID 2>/dev/null"
sleep 1
check "no orphaned Xvfb remains" bash -c "! pgrep -ax Xvfb | grep -q ':${SMOKE_DISPLAY:-98} '"
DDA_PID=""

echo
echo "Passed: $PASS   Failed: $FAIL"
if [ "$FAIL" -ne 0 ]; then
  echo "--- run.log ---"
  tail -n 40 "$WORK/run.log" || true
  exit 1
fi
