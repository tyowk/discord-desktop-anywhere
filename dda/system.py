"""Host introspection: binaries, noVNC location, distro hints, the ``doctor`` report."""

from __future__ import annotations

import glob
import logging
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from dda.config import Config

log = logging.getLogger("dda.system")

NOVNC_FALLBACK_URL = "https://github.com/novnc/noVNC/archive/refs/tags/v1.5.0.tar.gz"
FHS_LOADER = "/lib64/ld-linux-x86-64.so.2"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def which(name: str) -> Optional[str]:
    return shutil.which(name)


def is_nixlike() -> bool:
    """True where the dynamic loader is not at the standard FHS path (Replit/Nix)."""
    return not os.path.exists(FHS_LOADER)


def glibc_loader() -> Optional[str]:
    explicit = os.environ.get("LOADER")
    if explicit and os.path.exists(explicit):
        return explicit
    hits = sorted(glob.glob("/nix/store/*-glibc-2.*/lib/ld-linux-x86-64.so.2"))
    return hits[-1] if hits else None


def detect_distro() -> str:
    try:
        data = {}
        for line in Path("/etc/os-release").read_text().splitlines():
            k, _, v = line.partition("=")
            data[k] = v.strip().strip('"')
        ids = f"{data.get('ID', '')} {data.get('ID_LIKE', '')}".lower()
    except OSError:
        return "unknown"
    if "nixos" in ids:
        return "nix"
    for key, name in (
        ("debian", "debian"),
        ("ubuntu", "debian"),
        ("fedora", "fedora"),
        ("rhel", "fedora"),
        ("arch", "arch"),
        ("suse", "suse"),
    ):
        if key in ids:
            return name
    return "unknown"


def install_hint() -> str:
    if os.environ.get("REPL_ID") or os.environ.get("REPLIT_DEV_DOMAIN"):
        return "On Replit the packages come from replit.nix: restart the Repl or reopen the Shell, then press Run."
    return "Install the dependencies with:  sudo ./install.sh"


# ------------------------------------------------------------------ noVNC
def _novnc_candidates(cfg: Config) -> List[str]:
    paths = []
    if cfg.novnc_dir:
        paths.append(cfg.novnc_dir)
    paths += ["/usr/share/novnc", "/usr/share/webapps/novnc", "/opt/novnc", str(cfg.data_dir / "novnc")]
    paths += sorted(glob.glob("/nix/store/*-novnc-*/share/webapps/novnc"), reverse=True)
    return paths


def find_novnc(cfg: Config) -> Optional[Path]:
    for p in _novnc_candidates(cfg):
        if os.path.isfile(os.path.join(p, "vnc.html")):
            return Path(p)
    return None


def ensure_novnc(cfg: Config) -> Path:
    """Locate noVNC, downloading a pinned release when the system has none."""
    found = find_novnc(cfg)
    if found:
        return found
    dest = cfg.data_dir / "novnc"
    log.info("noVNC not found on this system; downloading %s", NOVNC_FALLBACK_URL)
    dest.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(NOVNC_FALLBACK_URL, headers={"User-Agent": USER_AGENT})
    with tempfile.TemporaryDirectory() as tmp:
        archive = os.path.join(tmp, "novnc.tar.gz")
        with urllib.request.urlopen(req, timeout=60) as resp, open(archive, "wb") as out:
            shutil.copyfileobj(resp, out)
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                parts = Path(member.name).parts[1:]  # strip the top-level directory
                target = dest.joinpath(*parts).resolve()
                if not parts or not str(target).startswith(str(dest.resolve())):
                    continue
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    src = tar.extractfile(member)
                    if src:
                        with src, open(target, "wb") as fh:
                            shutil.copyfileobj(src, fh)
    if not (dest / "vnc.html").is_file():
        raise RuntimeError("noVNC download did not contain vnc.html; set NOVNC_DIR manually")
    return dest


# ------------------------------------------------------------------ doctor
@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    required: bool = True


def ffmpeg_capabilities(ffmpeg: str) -> dict:
    caps = {"pulse": False, "mp3": False, "opus": False}
    try:
        devices = subprocess.run(
            [ffmpeg, "-hide_banner", "-devices"], capture_output=True, text=True, timeout=10
        ).stdout
        encoders = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return caps
    caps["pulse"] = any(" pulse " in line for line in devices.splitlines())
    caps["mp3"] = "libmp3lame" in encoders
    caps["opus"] = "libopus" in encoders
    return caps


def run_doctor(cfg: Config) -> List[Check]:
    checks: List[Check] = []
    machine = platform.machine().lower()
    checks.append(
        Check(
            "CPU architecture is x86_64",
            machine in ("x86_64", "amd64"),
            f"found {machine}; Discord only ships Linux x86_64 builds",
        )
    )
    checks.append(
        Check(
            "Python >= 3.8", tuple(map(int, platform.python_version_tuple()[:2])) >= (3, 8), platform.python_version()
        )
    )
    for binary, why in (
        ("Xvfb", "virtual display"),
        ("x11vnc", "VNC server"),
        ("openbox", "window manager"),
        ("tar", "extracting the installer"),
    ):
        path = which(binary)
        checks.append(Check(f"{binary} ({why})", bool(path), path or "not found"))
    novnc = find_novnc(cfg)
    checks.append(
        Check("noVNC web client", True, str(novnc) if novnc else "will be downloaded on first start", required=False)
    )
    for binary, why in (
        ("xdotool", "keyboard shortcuts and window fitting"),
        ("dbus-launch", "session bus (notifications)"),
        ("import", "ImageMagick: native screenshots"),
        ("patchelf", "Nix/Replit loader patching"),
    ):
        path = which(binary)
        needed = binary == "patchelf" and is_nixlike()
        detail = path or ("not found" if needed or binary != "patchelf" else "not needed on this system")
        checks.append(Check(f"{binary} ({why})", bool(path) or not needed, detail, required=needed))
    if cfg.audio:
        for binary in ("pulseaudio", "pactl"):
            path = which(binary)
            checks.append(
                Check(f"{binary} (audio)", bool(path), path or "not found; audio will be disabled", required=False)
            )
        ff = which("ffmpeg")
        if ff:
            caps = ffmpeg_capabilities(ff)
            checks.append(
                Check(
                    "ffmpeg can capture PulseAudio",
                    caps["pulse"] or bool(which("parec")),
                    "ok" if caps["pulse"] else "no pulse input; falling back to parec",
                    required=False,
                )
            )
            checks.append(
                Check(
                    "ffmpeg has an MP3 or Opus encoder",
                    caps["mp3"] or caps["opus"],
                    f"mp3={caps['mp3']} opus={caps['opus']}",
                    required=False,
                )
            )
        else:
            checks.append(Check("ffmpeg (audio encoding)", False, "not found; audio will be disabled", required=False))
    if is_nixlike():
        checks.append(Check("glibc loader (Nix)", bool(glibc_loader()), glibc_loader() or "not found"))
    return checks


def format_checks(checks: List[Check]) -> str:
    lines = []
    for c in checks:
        mark = "ok  " if c.ok else ("FAIL" if c.required else "warn")
        lines.append(f"  [{mark}] {c.name}" + (f"  ({c.detail})" if c.detail else ""))
    return "\n".join(lines)
