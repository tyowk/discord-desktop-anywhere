"""Command line entry point: ``python3 -m dda [run|doctor|password|version]``."""

from __future__ import annotations

import argparse
import fcntl
import logging
import logging.handlers
import os
import shutil
import signal
import sys
import threading
from typing import List, Optional

from dda import __version__
from dda.app import DiscordApp, InstallError, fit_window_later
from dda.audio import AudioManager
from dda.auth import Auth
from dda.config import Config, ConfigError
from dda.desktop import base_env, openbox_service, pick_display, start_dbus, x11vnc_service, xvfb_service
from dda.procs import Supervisor, reap_stale
from dda.server import Context, make_server
from dda.shots import ShotStore
from dda.state import AppState
from dda.system import ensure_novnc, format_checks, install_hint, run_doctor

log = logging.getLogger("dda")


def setup_logging(cfg: Config) -> None:
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(getattr(logging, cfg.log_level, logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    try:
        fileh = logging.handlers.RotatingFileHandler(cfg.log_dir / "dda.log", maxBytes=2_000_000, backupCount=2)
        fileh.setFormatter(fmt)
        root.addHandler(fileh)
    except OSError:
        pass


def banner(cfg: Config) -> None:
    line = "=" * 62
    print(line)
    print(f" Discord Desktop Anywhere {__version__}")
    print(f" Open:      {cfg.public_url}")
    if cfg.auth_enabled:
        if cfg.password_generated:
            print(f" Password:  {cfg.password}   (generated; set VNC_PASSWORD to choose your own)")
        else:
            print(" Password:  taken from VNC_PASSWORD")
    else:
        print(" WARNING:   authentication is OFF (ALLOW_NO_AUTH=1). Anyone with the URL controls Discord!")
    if cfg.host not in ("127.0.0.1", "localhost") and not cfg.on_replit:
        print(" NOTE:      plain HTTP on a public interface: put a TLS reverse proxy in front (see README).")
    print(line, flush=True)


def acquire_lock(cfg: Config):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    fh = open(cfg.data_dir / "dda.lock", "w")  # noqa: SIM115 - held for the process lifetime
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Another instance is already running for this data directory.", file=sys.stderr)
        sys.exit(1)
    return fh


def cmd_run(cfg: Config) -> int:
    setup_logging(cfg)
    lock = acquire_lock(cfg)  # noqa: F841
    checks = run_doctor(cfg)
    failed = [c for c in checks if c.required and not c.ok]
    if failed:
        print("Missing requirements:\n" + format_checks(failed) + "\n\n" + install_hint(), file=sys.stderr)
        return 2
    for d in (cfg.app_dir, cfg.profile_dir, cfg.config_home, cfg.log_dir):
        d.mkdir(parents=True, exist_ok=True)
    pid_file = cfg.runtime_dir / "children.pids"
    cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(cfg.runtime_dir, 0o711)  # traversable: PulseAudio may run as another user
    stale = reap_stale(pid_file)
    if stale:
        log.info("cleaned up %d process(es) left behind by a previous run", stale)

    state = AppState()
    stop = threading.Event()
    sup = Supervisor(pid_file)
    display_holder = {"value": f":{cfg.display_num}"}
    audio = AudioManager(cfg)
    novnc = ensure_novnc(cfg)
    shots = ShotStore(cfg.shot_dir, cfg.max_shots, cfg.cdp_port, display_holder["value"], cfg.max_shot_width)
    ctx = Context(cfg, Auth(cfg.password), state, audio, shots, novnc, lambda: display_holder["value"])
    server = make_server(ctx)
    threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
    banner(cfg)

    def on_signal(signum, _frame) -> None:
        log.info("received %s, shutting down", signal.Signals(signum).name)
        sup.stop_event.set()
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, on_signal)

    dbus_pid: Optional[int] = None
    app: Optional[DiscordApp] = None
    exit_code = 0
    try:
        state.set("starting", "Starting the virtual desktop...")
        display = f":{pick_display(cfg.display_num)}"
        display_holder["value"] = display
        shots.display = display
        env = base_env(cfg, display)
        sup.add(xvfb_service(cfg, display, env))
        dbus_pid = start_dbus(env)

        pulse = audio.build_service()
        if pulse:
            try:
                sup.add(pulse)
                audio.finish_setup()
            except RuntimeError as exc:
                audio.reason = str(exc)
                log.warning("audio disabled: %s", exc)
        env.update(audio.env())

        sup.add(openbox_service(cfg, env))
        sup.add(x11vnc_service(cfg, display, env))
        if shutil.which("xsetroot"):
            os.system(f"DISPLAY={display} xsetroot -solid '#1e1f22' 2>/dev/null")

        app = DiscordApp(cfg, env, state.set)
        app.write_settings()
        state.set("downloading", "Preparing Discord...")
        try:
            app.ensure_installed()
        except InstallError as exc:
            state.set("error", str(exc))
            log.error("%s", exc)
            return 3
        state.set("launching", "Launching Discord...")
        service = app.service()
        sup.add(service)
        fit_window_later(cfg, env, stop)
        state.set("running", "")
        log.info("Discord is running; open %s", cfg.public_url)

        def watch() -> None:
            while not stop.wait(2):
                state.discord_running = service.alive()

        threading.Thread(target=watch, name="state-watch", daemon=True).start()
        sup.run_forever()
        if sup.fatal:
            log.error("%s", sup.fatal)
            state.set("error", sup.fatal)
            exit_code = 1
    except RuntimeError as exc:
        log.error("%s", exc)
        exit_code = 1
    finally:
        stop.set()
        server.shutdown()
        sup.shutdown()
        if app and app.patcher:
            app.patcher.stop()
        if dbus_pid:
            try:
                os.kill(dbus_pid, signal.SIGTERM)
            except OSError:
                pass
        try:
            pid_file.unlink()
        except OSError:
            pass
    return exit_code


def cmd_doctor(cfg: Config) -> int:
    checks = run_doctor(cfg)
    print(f"Discord Desktop Anywhere {__version__}: environment check\n")
    print(format_checks(checks))
    bad = [c for c in checks if c.required and not c.ok]
    print(
        "\nAll required components are present."
        if not bad
        else "\nSome required components are missing.\n" + install_hint()
    )
    return 1 if bad else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dda", description="Discord desktop client in your browser (noVNC + audio).")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("run", help="start everything (default)")
    sub.add_parser("doctor", help="check that the host has everything needed")
    sub.add_parser("password", help="print the login password")
    sub.add_parser("version", help="print the version")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command or "run"
    if command == "version":
        print(__version__)
        return 0
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    if command == "doctor":
        return cmd_doctor(cfg)
    if command == "password":
        print(cfg.password if cfg.auth_enabled else "(authentication is disabled)")
        return 0
    return cmd_run(cfg)
