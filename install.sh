#!/usr/bin/env bash
# Install the system packages Discord Desktop Anywhere needs on Debian/Ubuntu (and derivatives),
# Fedora/RHEL, or Arch. Run it once, as root or with sudo:
#
#   sudo ./install.sh              everything, including audio
#   sudo ./install.sh --no-audio   skip PulseAudio and ffmpeg
#
# Replit does not need this script: replit.nix provides the same packages.
set -euo pipefail

WITH_AUDIO=1
for arg in "$@"; do
  case "$arg" in
    --no-audio) WITH_AUDIO=0 ;;
    -h | --help)
      sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "Unknown option: $arg (try --help)" >&2
      exit 2
      ;;
  esac
done

if [ "$(uname -s)" != "Linux" ]; then
  echo "This project only supports Linux." >&2
  exit 1
fi
case "$(uname -m)" in
  x86_64 | amd64) ;;
  *) echo "Warning: Discord only ships Linux x86_64 builds; this machine is $(uname -m)." >&2 ;;
esac

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
  else
    echo "Run this script as root or install sudo." >&2
    exit 1
  fi
fi

log() { printf '\n==> %s\n' "$*"; }

# ---------------------------------------------------------------- Debian / Ubuntu
apt_first_available() { # print the first package name that exists in the repositories
  local pkg
  for pkg in "$@"; do
    if apt-cache show "$pkg" >/dev/null 2>&1; then
      echo "$pkg"
      return 0
    fi
  done
  return 1
}

install_apt() {
  export DEBIAN_FRONTEND=noninteractive
  log "Updating package lists"
  $SUDO apt-get update -qq

  local pkgs=(python3 curl ca-certificates tar gzip xvfb x11vnc openbox xdotool x11-xserver-utils
    dbus-x11 imagemagick util-linux fonts-dejavu-core fonts-noto-color-emoji
    libnss3 libnspr4 libdrm2 libgbm1 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2
    libxss1 libxtst6 libnotify4 libpulse0 libxshmfence1 libx11-xcb1 libuuid1 libexpat1 libdbus-1-3)

  # Packages renamed with a t64 suffix on Ubuntu 24.04 / Debian 13.
  local name
  for group in "libasound2t64 libasound2" "libatk1.0-0t64 libatk1.0-0" "libatk-bridge2.0-0t64 libatk-bridge2.0-0"
  do
    # shellcheck disable=SC2086
    if name="$(apt_first_available $group)"; then pkgs+=("$name"); fi
  done
  for group in "libcups2t64 libcups2" "libgtk-3-0t64 libgtk-3-0" "libglib2.0-0t64 libglib2.0-0"
  do
    # shellcheck disable=SC2086
    if name="$(apt_first_available $group)"; then pkgs+=("$name"); fi
  done
  if [ "$WITH_AUDIO" = 1 ]; then
    pkgs+=(pulseaudio pulseaudio-utils ffmpeg)
  fi

  log "Installing packages"
  $SUDO apt-get install -y --no-install-recommends "${pkgs[@]}"
}

# ---------------------------------------------------------------- Fedora / RHEL
install_dnf() {
  local pkgs=(python3 curl tar gzip xorg-x11-server-Xvfb x11vnc openbox xdotool xorg-x11-server-utils
    dbus-x11 ImageMagick util-linux nss nspr atk at-spi2-atk cups-libs libdrm mesa-libgbm gtk3 alsa-lib
    libxkbcommon libXcomposite libXdamage libXrandr libXScrnSaver libXtst libnotify pulseaudio-libs
    libxshmfence dejavu-sans-fonts google-noto-emoji-color-fonts)
  log "Installing packages (RHEL/Alma/Rocky need EPEL enabled for x11vnc and openbox)"
  $SUDO dnf install -y "${pkgs[@]}"
  if [ "$WITH_AUDIO" = 1 ]; then
    log "Installing audio packages (Fedora ships PipeWire; pulseaudio may conflict with it)"
    $SUDO dnf install -y pulseaudio pulseaudio-utils ffmpeg-free ||
      echo "Audio packages could not be installed; the web UI will report audio as unavailable." >&2
  fi
}

# ---------------------------------------------------------------- Arch
install_pacman() {
  local pkgs=(python curl tar gzip xorg-server-xvfb x11vnc openbox xdotool xorg-xsetroot dbus imagemagick
    util-linux nss nspr gtk3 alsa-lib libxss libxtst libnotify libpulse libxkbcommon libxshmfence
    at-spi2-core libcups libdrm mesa ttf-dejavu noto-fonts-emoji)
  log "Installing packages"
  $SUDO pacman -Sy --needed --noconfirm "${pkgs[@]}"
  if [ "$WITH_AUDIO" = 1 ]; then
    $SUDO pacman -S --needed --noconfirm pulseaudio ffmpeg ||
      echo "Audio packages could not be installed (pipewire-pulse conflict?); audio will be unavailable." >&2
  fi
}

if command -v apt-get >/dev/null 2>&1; then
  install_apt
elif command -v dnf >/dev/null 2>&1; then
  install_dnf
elif command -v pacman >/dev/null 2>&1; then
  install_pacman
else
  cat >&2 <<'MSG'
Unsupported distribution. Install these with your package manager, then run `bash start.sh doctor`:
  python3 (3.8+), Xvfb, x11vnc, openbox, xdotool, xsetroot, dbus-launch, ImageMagick, util-linux,
  the Electron/Chromium runtime libraries (nss, gtk3, alsa, libXScrnSaver, libXtst, libnotify, ...),
  and for audio: pulseaudio (+ pactl) and ffmpeg.
MSG
  exit 1
fi

log "Checking the installation"
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
bash start.sh doctor || true

cat <<'MSG'

Done. Start it with:   bash start.sh
Do NOT run it as root unless you are inside a container (Discord then needs --no-sandbox, which is already set).
MSG
