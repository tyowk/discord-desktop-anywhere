#!/usr/bin/env bash
# Run Discord PTB in a browser via noVNC (without Docker) - for Replit (Linux x86_64)
set -u

BASE="$HOME/workspace/.discord-novnc"
[ -d "$HOME/workspace" ] || BASE="$PWD/.discord-novnc"
APP_DIR="$BASE/app"          # Extracted lightweight installer (updater_bootstrap)
PROFILE="$BASE/profile"
LOG="$BASE/logs"
mkdir -p "$APP_DIR" "$PROFILE" "$LOG"

export DISPLAY=:1
export XDG_CONFIG_HOME="$PROFILE/config"
export XDG_RUNTIME_DIR="$BASE/runtime"
mkdir -p "$XDG_CONFIG_HOME" "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"
DISC_DIR="$XDG_CONFIG_HOME/discordptb"   # Location where the bootstrap downloads Discord

# Feature toggles (set in Replit Secrets)
export DEVTOOLS="${DEVTOOLS:-0}"           # 1 = enable Discord DevTools (OFF by default)
export SCREENSHOT="${SCREENSHOT:-1}"       # 1 = HD screenshots through the local debug port
export CDP_PORT=9222                       # Chrome DevTools Protocol, localhost only (not exposed)
export SHOT_DIR="$(dirname "$BASE")/screenshots"   # Screenshots (visible in the Replit Files panel)
mkdir -p "$SHOT_DIR"

RES="${RESOLUTION:-960x540x24}"
VNC_PORT=5900
WEB_PORT=6080
VNC_PASS="${VNC_PASSWORD:-}"   # Set in Replit Secrets: VNC_PASSWORD

UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
PTB_URL="${PTB_URL:-https://ptb.discord.com/api/downloads/distributions/app/installers/latest?channel=ptb&platform=linux&arch=x64&format=tar.gz}"
LOCAL_TAR="$HOME/workspace/discord-ptb.tar.gz"   # Manual option: place the installer tarball here

# ---------- 1. Download the lightweight installer (~2 MB): contains updater_bootstrap ----------
if [ ! -f "$APP_DIR/.extracted" ]; then
  TAR="$BASE/ptb.tar.gz"; rm -f "$TAR"
  if [ -f "$LOCAL_TAR" ]; then
    echo "[*] Using local tarball: $LOCAL_TAR"; cp "$LOCAL_TAR" "$TAR"
  else
    echo "[*] Downloading installer from: $PTB_URL"
    INFO="$(curl -L --retry 3 --connect-timeout 20 -A "$UA" -sS -o "$TAR" \
      -w 'HTTP %{http_code} | %{size_download} bytes' "$PTB_URL" 2>&1)"
    echo "[*] curl: $INFO"
  fi
  SZ=$(stat -c %s "$TAR" 2>/dev/null || echo 0)
  if [ "$SZ" -lt 300000 ]; then
    echo "[!] Installer file is too small ($SZ bytes). Initial contents:"
    head -c 300 "$TAR" | cat -v; echo
    rm -f "$TAR"; exit 1
  fi
  rm -rf "$APP_DIR"; mkdir -p "$APP_DIR"
  tar -xzf "$TAR" -C "$APP_DIR" || { echo "[!] Extraction failed"; exit 1; }
  touch "$APP_DIR/.extracted"; rm -f "$TAR"
fi

# ---------- 2. Clean up previous processes ----------
pkill -f "Xvfb :1" 2>/dev/null
pkill -f x11vnc 2>/dev/null
pkill -f websockify 2>/dev/null
pkill -f openbox 2>/dev/null
pkill -f control.py 2>/dev/null
rm -f /tmp/.X1-lock /tmp/.X11-unix/X1 2>/dev/null
sleep 1

# ---------- 3. Virtual display + window manager + VNC + noVNC ----------
Xvfb :1 -screen 0 "$RES" -ac +extension GLX +render -noreset >"$LOG/xvfb.log" 2>&1 &
for i in $(seq 1 20); do
  [ -e /tmp/.X11-unix/X1 ] && break
  sleep 0.5
done
eval "$(dbus-launch --sh-syntax 2>/dev/null)" || true
openbox >"$LOG/openbox.log" 2>&1 &
xsetroot -solid "#1e1f22" 2>/dev/null

if [ -n "$VNC_PASS" ]; then
  x11vnc -storepasswd "$VNC_PASS" "$BASE/vncpass" >/dev/null 2>&1
  AUTH="-rfbauth $BASE/vncpass"
else
  AUTH="-nopw"
  echo "[!] VNC_PASSWORD is not set. Anyone with the URL can access the desktop. Set it in Secrets!"
fi
x11vnc -display :1 -forever -shared -rfbport $VNC_PORT $AUTH -noxdamage -quiet >"$LOG/x11vnc.log" 2>&1 &
sleep 1

NOVNC_DIR="$(ls -d /nix/store/*-novnc-*/share/webapps/novnc 2>/dev/null | head -1)"
[ -z "$NOVNC_DIR" ] && { echo "[!] noVNC directory not found in /nix/store"; exit 1; }
cp -f "$NOVNC_DIR/vnc.html" "$NOVNC_DIR/index.html" 2>/dev/null || true
websockify --web "$NOVNC_DIR" 0.0.0.0:$WEB_PORT localhost:$VNC_PORT >"$LOG/websockify.log" 2>&1 &

echo "======================================================"
echo " noVNC is ready on port $WEB_PORT -> open the Replit Webview tab"
echo " Append /vnc.html?autoconnect=true&resize=scale"
echo " Mobile control panel (screenshots, reload, etc.): port 6081 (external port 3000)"
echo "======================================================"

# ---------- 4. glibc loader (Nix does not provide /lib64/ld-linux) ----------
LOADER="${LOADER:-$(ls /nix/store/*-glibc-2.*/lib/ld-linux-x86-64.so.2 2>/dev/null | head -1)}"
if [ -z "$LOADER" ] || [ ! -e "$LOADER" ]; then
  echo "[!] glibc loader not found. Close and reopen the Replit Shell, then click Run again."
  exit 1
fi
echo "[*] Loader: $LOADER"

patch_all() {  # Patch ELF executables in $BASE whose interpreter is missing
  find "$BASE" -type f -perm -u+x ! -name '*.so*' 2>/dev/null | while read -r f; do
    I="$(patchelf --print-interpreter "$f" 2>/dev/null)" || continue
    [ -n "$I" ] && [ ! -e "$I" ] && patchelf --set-interpreter "$LOADER" "$f" 2>>"$LOG/patchelf.log"
  done
}
patch_all

# ---------- 5. Discord DevTools (OFF by default; set DEVTOOLS=1 to enable) ----------
mkdir -p "$DISC_DIR"
python3 - "$DISC_DIR/settings.json" "$DEVTOOLS" <<'PY'
import json, sys, os
p, enabled = sys.argv[1], sys.argv[2] == "1"
KEY = "DANGEROUS_ENABLE_DEVTOOLS_ONLY_ENABLE_IF_YOU_KNOW_WHAT_YOURE_DOING"
try:
    data = json.load(open(p)) if os.path.exists(p) else {}
except Exception:
    data = {}
if enabled:
    data[KEY] = True
else:
    data.pop(KEY, None)   # also clears the flag saved by older versions
json.dump(data, open(p, "w"), indent=2)
PY
[ "$DEVTOOLS" = "1" ] && echo "[*] DevTools: enabled" || echo "[*] DevTools: disabled (set DEVTOOLS=1 to enable)"

# ---------- 6. Mobile control panel (port 6081) ----------
python3 "$(dirname "$(readlink -f "$0")")/control.py" >"$LOG/control.log" 2>&1 &

# ---------- 7. Download the full Discord app using updater_bootstrap ----------
find_app_bin() {  # The Electron binary is inside an app-* subdirectory, not at the root
  find "$DISC_DIR" -mindepth 2 -maxdepth 3 -type f -name DiscordPTB 2>/dev/null | sort | tail -1
}
BIN="$(find_app_bin)"
if [ -z "$BIN" ]; then
  BOOT="$(find "$APP_DIR" -maxdepth 3 -type f -name updater_bootstrap | head -1)"
  [ -z "$BOOT" ] && { echo "[!] updater_bootstrap not found. Contents of $APP_DIR:"; find "$APP_DIR" | head -30; exit 1; }
  chmod +x "$BOOT"
  patchelf --set-interpreter "$LOADER" "$BOOT" 2>>"$LOG/patchelf.log"
  echo "[*] Running updater_bootstrap (downloading Discord; this may take several minutes)..."
  OUT="$("$BOOT" --no-zenity "$DISC_DIR" ptb "https://updates.discord.com/" 2>"$LOG/bootstrap.err")"
  RC=$?
  echo "[*] Bootstrap exit=$RC  output='$OUT'"
  tail -n 8 "$LOG/bootstrap.err"
  patch_all
  BIN="$DISC_DIR/$(echo "$OUT" | tail -n1)/DiscordPTB"
  [ -x "$BIN" ] || BIN="$(find_app_bin)"
  if [ -z "$BIN" ] || [ ! -e "$BIN" ]; then
    echo "[!] Discord binary not found after bootstrap. Contents of $DISC_DIR:"
    find "$DISC_DIR" -maxdepth 3 | head -40
    echo "[!] Send the output above and $LOG/bootstrap.err for debugging."
    exit 1
  fi
fi

DIR="$(dirname "$BIN")"
export LD_LIBRARY_PATH="$DIR:${LD_LIBRARY_PATH:-}"
echo "[*] Binary: $BIN"
echo "[*] Interpreter: $(patchelf --print-interpreter "$BIN" 2>/dev/null)"
MISSING="$(ldd "$BIN" 2>/dev/null | grep 'not found')"
[ -n "$MISSING" ] && echo "[!] Missing libraries:" && echo "$MISSING"

# Continue patching for 15 minutes so newly downloaded Discord modules/binaries are also fixed
( for i in $(seq 1 300); do sleep 3; patch_all; done ) &

# ---------- 8. Automatically open DevTools (needs DEVTOOLS=1 and AUTO_DEVTOOLS=1; both OFF by default) ----------
if [ "$DEVTOOLS" = "1" ] && [ "${AUTO_DEVTOOLS:-0}" = "1" ]; then
  (
    for i in $(seq 1 180); do
      WID="$(xdotool search --onlyvisible --name "Discord" 2>/dev/null | tail -1)"
      [ -n "$WID" ] && break
      sleep 1
    done
    if [ -n "${WID:-}" ]; then
      sleep 12
      xdotool windowfocus --sync "$WID" 2>/dev/null
      xdotool key --clearmodifiers ctrl+shift+i 2>/dev/null
    fi
  ) &
fi

# ---------- 8b. Fit the Discord window to the virtual screen ----------
W="${RES%%x*}"; H="$(echo "$RES" | cut -d x -f2)"
(
  for i in $(seq 1 180); do
    WID="$(xdotool search --onlyvisible --name "Discord" 2>/dev/null | tail -1)"
    [ -n "$WID" ] && break
    sleep 1
  done
  if [ -n "${WID:-}" ]; then
    sleep 4
    xdotool windowmove "$WID" 0 0 windowsize "$WID" "$W" "$H" 2>/dev/null
  fi
) &

# ---------- 9. Run Discord PTB (automatically restart if it crashes) ----------
ARGS=(--no-sandbox --disable-gpu --disable-dev-shm-usage --disable-software-rasterizer)
# Local debug port, only used by the screenshot feature (bound to localhost)
[ "$SCREENSHOT" = "1" ] && ARGS+=(--remote-debugging-port="$CDP_PORT")

while true; do
  "$BIN" "${ARGS[@]}" >>"$LOG/discord.log" 2>&1
  RC=$?
  echo "[!] Discord exited (code $RC). Restarting in 3 seconds..."
  tail -n 5 "$LOG/discord.log"
  sleep 3
done
