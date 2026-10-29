"""The virtual desktop: Xvfb, session D-Bus, Openbox and x11vnc."""

from __future__ import annotations

import logging
import os
import re
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Dict, Optional

from dda.config import Config
from dda.procs import Service, path_ready, tcp_ready

log = logging.getLogger("dda.desktop")

OPENBOX_RC = """<?xml version="1.0" encoding="UTF-8"?>
<openbox_config xmlns="http://openbox.org/3.4/rc">
  <focus><focusNew>yes</focusNew><followMouse>no</followMouse></focus>
  <applications>
    <!-- Kiosk behaviour: normal windows (Discord) fill the screen and have no extra title bar. -->
    <application class="*" type="normal">
      <maximized>yes</maximized>
      <decor>no</decor>
    </application>
  </applications>
</openbox_config>
"""


def _x_socket_alive(n: int) -> bool:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(1)
    try:
        sock.connect(f"/tmp/.X11-unix/X{n}")
        return True
    except OSError:
        return False
    finally:
        sock.close()


def pick_display(preferred: int) -> int:
    """First free X display number >= preferred; clears stale lock and socket files."""
    for n in range(preferred, preferred + 50):
        lock, xsock = Path(f"/tmp/.X{n}-lock"), Path(f"/tmp/.X11-unix/X{n}")
        if lock.exists():
            try:
                os.kill(int(lock.read_text().strip()), 0)
                continue  # a live X server owns this display
            except (ValueError, OSError):
                pass
        elif xsock.exists() and _x_socket_alive(n):
            continue  # someone is listening without a lock file
        for stale in (lock, xsock):
            try:
                stale.unlink()
            except OSError:
                pass
        return n
    raise RuntimeError("no free X display number found")


def base_env(cfg: Config, display: str) -> Dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "DISPLAY": display,
            "XDG_CONFIG_HOME": str(cfg.config_home),
            "XDG_RUNTIME_DIR": str(cfg.runtime_dir),
        }
    )
    return env


def start_dbus(env: Dict[str, str]) -> Optional[int]:
    """Start a session bus and add it to ``env``. Returns its PID (for cleanup)."""
    launcher = shutil.which("dbus-launch")
    if not launcher or env.get("DBUS_SESSION_BUS_ADDRESS"):
        return None
    try:
        out = subprocess.run([launcher, "--sh-syntax"], env=env, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    addr = re.search(r"DBUS_SESSION_BUS_ADDRESS='([^']+)'", out)
    pid = re.search(r"DBUS_SESSION_BUS_PID=(\d+)", out)
    if addr:
        env["DBUS_SESSION_BUS_ADDRESS"] = addr.group(1)
    return int(pid.group(1)) if pid else None


def xvfb_service(cfg: Config, display: str, env: Dict[str, str]) -> Service:
    num = display.lstrip(":")
    return Service(
        "xvfb",
        [
            "Xvfb",
            display,
            "-screen",
            "0",
            str(cfg.resolution),
            "-ac",
            "+extension",
            "GLX",
            "+render",
            "-noreset",
            "-nolisten",
            "tcp",
        ],
        env=env,
        log_path=cfg.log_dir / "xvfb.log",
        critical=True,
        ready=path_ready(f"/tmp/.X11-unix/X{num}"),
    )


def openbox_service(cfg: Config, env: Dict[str, str]) -> Service:
    rc_dir = cfg.config_home / "openbox"
    rc_dir.mkdir(parents=True, exist_ok=True)
    rc = rc_dir / "rc.xml"
    rc.write_text(OPENBOX_RC)
    return Service(
        "openbox",
        ["openbox", "--config-file", str(rc)],
        env=env,
        log_path=cfg.log_dir / "openbox.log",
        restart="always",
    )


def x11vnc_service(cfg: Config, display: str, env: Dict[str, str]) -> Service:
    # Bound to localhost: the only way in is our authenticated web gateway.
    argv = [
        "x11vnc",
        "-display",
        display,
        "-forever",
        "-shared",
        "-rfbport",
        str(cfg.vnc_port),
        "-localhost",
        "-nopw",
        "-noxdamage",
        "-noipv6",
        "-nolookup",
        "-quiet",
    ]
    return Service(
        "x11vnc",
        argv,
        env=env,
        log_path=cfg.log_dir / "x11vnc.log",
        restart="always",
        ready=tcp_ready("127.0.0.1", cfg.vnc_port),
    )
