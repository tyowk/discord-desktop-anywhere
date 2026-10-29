"""Child-process supervision: ordered start-up, restart with back-off, clean shutdown."""

from __future__ import annotations

import logging
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

log = logging.getLogger("dda.procs")

MAX_LOG_BYTES = 5 * 1024 * 1024


def wait_for(predicate: Callable[[], bool], timeout: float, interval: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def tcp_ready(host: str, port: int) -> Callable[[], bool]:
    def check() -> bool:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            return False

    return check


def path_ready(path: str) -> Callable[[], bool]:
    return lambda: os.path.exists(path)


def _rotate(path: Path) -> None:
    try:
        if path.stat().st_size > MAX_LOG_BYTES:
            os.replace(path, path.with_suffix(path.suffix + ".1"))
    except OSError:
        pass


class Service:
    """One supervised child process."""

    def __init__(
        self,
        name: str,
        argv: Sequence[str],
        env: Optional[Dict[str, str]] = None,
        log_path: Optional[Path] = None,
        restart: str = "never",  # never | always
        critical: bool = False,
        ready: Optional[Callable[[], bool]] = None,
        ready_timeout: float = 20.0,
    ) -> None:
        self.name = name
        self.argv = list(argv)
        self.env = env
        self.log_path = log_path
        self.restart = restart
        self.critical = critical
        self.ready = ready
        self.ready_timeout = ready_timeout
        self.proc: Optional[subprocess.Popen] = None
        self.started_at = 0.0
        self.failures = 0
        self.restarts = 0
        self.next_start = 0.0

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        argv = self.argv
        setpriv = shutil.which("setpriv")
        if setpriv:  # die together with the supervisor, even after SIGKILL
            argv = [setpriv, "--pdeathsig", "SIGKILL"] + argv
        out = subprocess.DEVNULL
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            _rotate(self.log_path)
            out = open(self.log_path, "ab")  # noqa: SIM115 - handed to Popen
        try:
            self.proc = subprocess.Popen(
                argv,
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            if out is not subprocess.DEVNULL:
                out.close()
        self.started_at = time.monotonic()
        log.debug("started %s (pid %s)", self.name, self.proc.pid)

    def start_and_wait(self) -> None:
        self.start()
        if self.ready and not wait_for(lambda: self.alive() and self.ready(), self.ready_timeout):  # type: ignore[misc]
            code = self.proc.poll() if self.proc else None
            raise RuntimeError(f"{self.name} did not become ready (exit code {code}); see {self.log_path}")

    def stop(self, timeout: float = 5.0) -> None:
        if not self.proc:
            return
        pgid = None
        try:
            pgid = os.getpgid(self.proc.pid)
        except OSError:
            pass
        if self.proc.poll() is None:
            self._signal(pgid, signal.SIGTERM)
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                self._signal(pgid, signal.SIGKILL)
                try:
                    self.proc.wait(2)
                except subprocess.TimeoutExpired:
                    pass
        elif pgid:
            self._signal(pgid, signal.SIGKILL)  # reap stragglers of the group

    def _signal(self, pgid: Optional[int], sig: int) -> None:
        try:
            if pgid and pgid != os.getpgrp():
                os.killpg(pgid, sig)
            elif self.proc:
                self.proc.send_signal(sig)
        except (OSError, ProcessLookupError):
            pass


class Supervisor:
    def __init__(self, pid_file: Optional[Path] = None) -> None:
        self.services: List[Service] = []
        self.stop_event = threading.Event()
        self.fatal: Optional[str] = None
        self.pid_file = pid_file

    def add(self, service: Service) -> Service:
        log.info("starting %s", service.name)
        service.start_and_wait()
        self.services.append(service)
        self._record_pids()
        return service

    def _record_pids(self) -> None:
        if not self.pid_file:
            return
        rows = [f"{s.proc.pid} {os.path.basename(s.argv[0])}" for s in self.services if s.proc]
        try:
            self.pid_file.parent.mkdir(parents=True, exist_ok=True)
            self.pid_file.write_text("\n".join(rows) + "\n")
        except OSError:
            pass

    def run_forever(self) -> None:
        while not self.stop_event.wait(1.0):
            for svc in list(self.services):
                self._tick(svc)

    def _tick(self, svc: Service) -> None:
        now = time.monotonic()
        if svc.alive():
            if now - svc.started_at > 60:
                svc.failures = 0  # stable for a minute: forgive earlier crashes
            return
        if svc.proc is None:
            return
        code = svc.proc.poll()
        if svc.restart != "always":
            if svc.critical:
                self.fatal = f"{svc.name} exited unexpectedly (code {code}); see {svc.log_path}"
                self.stop_event.set()
            return
        if svc.next_start == 0.0:  # first time we notice this exit: schedule a restart
            svc.failures += 1
            if svc.critical and svc.failures > 8:
                self.fatal = f"{svc.name} keeps crashing (last exit code {code}); see {svc.log_path}"
                self.stop_event.set()
                return
            delay = min(30.0, 2.0 ** min(svc.failures, 5)) if now - svc.started_at <= 60 else 3.0
            log.warning("%s exited (code %s); restarting in %.0fs", svc.name, code, delay)
            svc.next_start = now + delay
        elif now >= svc.next_start:
            svc.next_start = 0.0
            svc.restarts += 1
            try:
                svc.start()
                self._record_pids()
            except OSError as exc:
                log.error("could not restart %s: %s", svc.name, exc)

    def shutdown(self) -> None:
        self.stop_event.set()
        for svc in reversed(self.services):
            log.info("stopping %s", svc.name)
            svc.stop()


def reap_stale(pid_file: Path) -> int:
    """Kill leftovers of a previous run that died without cleaning up (e.g. SIGKILL)."""
    killed = 0
    try:
        rows = pid_file.read_text().splitlines()
    except OSError:
        return 0
    for row in rows:
        pid_s, _, name = row.partition(" ")
        if not pid_s.isdigit() or not name:
            continue
        pid = int(pid_s)
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        flat = b" ".join(cmd)
        if name.encode() in flat or (cmd and os.path.basename(cmd[0].decode(errors="ignore")) == name):
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
                killed += 1
            except OSError:
                pass
    try:
        pid_file.unlink()
    except OSError:
        pass
    return killed
