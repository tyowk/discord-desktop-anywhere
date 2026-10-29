"""Audio: a private PulseAudio server with a virtual speaker, streamed to the browser.

Discord (Electron) plays into a null sink called ``dda``. Each listener gets its own
``ffmpeg`` process that captures ``dda.monitor`` and encodes it as a live MP3 or
Ogg/Opus stream, which the web UI plays with a plain ``<audio>`` element.

Having a real (virtual) sound card is also what makes video playback work inside
Discord: without any audio device Chromium's media pipeline stalls on videos.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from dda.config import Config
from dda.procs import Service, path_ready, wait_for
from dda.system import ffmpeg_capabilities

log = logging.getLogger("dda.audio")

SINK = "dda"
MONITOR = "dda.monitor"
CONTENT_TYPES = {"mp3": "audio/mpeg", "opus": "audio/ogg"}


class AudioBusy(RuntimeError):
    """Too many simultaneous listeners."""


class AudioStream:
    """A running capture/encode pipeline; iterate to receive encoded chunks."""

    def __init__(self, procs: List[subprocess.Popen], release) -> None:
        self._procs = procs
        self._release = release
        self._closed = False

    def __iter__(self) -> Iterator[bytes]:
        fd = self._procs[-1].stdout.fileno()  # type: ignore[union-attr]
        while True:
            try:
                chunk = os.read(fd, 8192)
            except OSError:
                return
            if not chunk:
                return
            yield chunk

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for p in reversed(self._procs):
            if p.poll() is None:
                p.terminate()
        for p in self._procs:
            try:
                p.wait(2)
            except subprocess.TimeoutExpired:
                p.kill()
            if p.stdout:
                p.stdout.close()
        self._release()


def build_encoder_args(fmt: str, bitrate: int) -> List[str]:
    if fmt == "opus":
        return [
            "-c:a",
            "libopus",
            "-b:a",
            f"{bitrate}k",
            "-application",
            "lowdelay",
            "-frame_duration",
            "20",
            "-page_duration",
            "20000",
            "-f",
            "ogg",
        ]
    return ["-c:a", "libmp3lame", "-b:a", f"{bitrate}k", "-write_xing", "0", "-f", "mp3"]


class AudioManager:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.pulse_dir = cfg.runtime_dir / "pulse"
        self.socket_path = str(self.pulse_dir / "native")
        self.server = f"unix:{self.socket_path}"
        self.available = False
        self.reason = "audio disabled" if not cfg.audio else "not started"
        self.fmt = cfg.audio_format
        self._caps: Dict[str, bool] = {}
        self._ffmpeg = shutil.which("ffmpeg")
        self._parec = shutil.which("parec")
        self._slots = threading.BoundedSemaphore(cfg.max_listeners)
        self._listeners = 0
        self._lock = threading.Lock()

    @property
    def listeners(self) -> int:
        return self._listeners

    def env(self) -> Dict[str, str]:
        """Environment that points clients (Discord, ffmpeg, pactl) at our server."""
        return {"PULSE_SERVER": self.server, "PULSE_SINK": SINK} if self.available else {}

    def _check_tools(self) -> Optional[str]:
        if not self.cfg.audio:
            return "audio disabled (AUDIO=0)"
        for tool in ("pulseaudio", "pactl"):
            if not shutil.which(tool):
                return f"{tool} not installed"
        if not self._ffmpeg:
            return "ffmpeg not installed"
        self._caps = ffmpeg_capabilities(self._ffmpeg)
        if not (self._caps["pulse"] or self._parec):
            return "ffmpeg lacks PulseAudio input and parec is missing"
        if self.fmt == "mp3" and not self._caps["mp3"]:
            if self._caps["opus"]:
                log.warning("ffmpeg has no MP3 encoder; using Opus")
                self.fmt = "opus"
            else:
                return "ffmpeg has no MP3 or Opus encoder"
        if self.fmt == "opus" and not self._caps["opus"]:
            return "ffmpeg has no Opus encoder"
        return None

    def build_service(self) -> Optional[Service]:
        """The PulseAudio daemon service, or None when audio cannot be provided."""
        problem = self._check_tools()
        if problem:
            self.reason = problem
            log.warning("audio unavailable: %s", problem)
            return None
        argv = [
            "pulseaudio",
            "-n",
            "--daemonize=no",
            "--exit-idle-time=-1",
            "--disable-shm=yes",
            "--use-pid-file=no",
            "--high-priority=no",
            "--realtime=no",
            "--log-target=stderr",
            "-L",
            f"module-native-protocol-unix socket={self.socket_path} auth-anonymous=1",
            "-L",
            f"module-null-sink sink_name={SINK} rate=48000 channels=2 "
            "sink_properties=device.description=DDA_Virtual_Speaker",
        ]
        # PulseAudio wants a private runtime dir and home that belong to the user it runs as.
        home = self.pulse_dir / "home"
        home.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env.pop("PULSE_SERVER", None)
        env.update({"HOME": str(home), "XDG_CONFIG_HOME": str(home / "config"), "XDG_RUNTIME_DIR": str(self.pulse_dir)})
        os.makedirs(env["XDG_CONFIG_HOME"], exist_ok=True)
        if os.getuid() == 0:
            # PulseAudio refuses to run as root: drop to an unprivileged user.
            if not shutil.which("setpriv"):
                self.reason = "running as root and setpriv is missing"
                return None
            for path in (self.pulse_dir, home, Path(env["XDG_CONFIG_HOME"])):
                os.chown(path, 65534, 65534)
            os.chmod(self.pulse_dir, 0o755)
            argv = ["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups"] + argv
        else:
            os.chmod(self.pulse_dir, 0o700)
        return Service(
            "pulseaudio",
            argv,
            env=env,
            log_path=self.cfg.log_dir / "pulseaudio.log",
            restart="always",
            ready=path_ready(self.socket_path),
        )

    def finish_setup(self) -> None:
        """Called once the daemon is up: select the virtual speaker and mark audio usable."""
        env = dict(os.environ, PULSE_SERVER=self.server)
        ok = wait_for(
            lambda: subprocess.run(["pactl", "info"], env=env, capture_output=True, timeout=5).returncode == 0, 10
        )
        if not ok:
            self.reason = "PulseAudio did not answer"
            log.warning("audio unavailable: %s", self.reason)
            return
        subprocess.run(["pactl", "set-default-sink", SINK], env=env, capture_output=True, timeout=5)
        self.available = True
        self.reason = ""
        log.info("audio ready (%s, %d kbps, virtual speaker '%s')", self.fmt, self.cfg.audio_bitrate, SINK)

    # ---- streaming ------------------------------------------------------
    def encoder_command(self, fmt: str) -> List[List[str]]:
        """Return the pipeline as a list of argv lists (one per process)."""
        enc = build_encoder_args(fmt, self.cfg.audio_bitrate)
        common = ["-hide_banner", "-loglevel", "error", "-nostdin"]
        tail = ["-flush_packets", "1", "pipe:1"]
        if self._caps.get("pulse"):
            return [
                [
                    self._ffmpeg or "ffmpeg",
                    *common,
                    "-fflags",
                    "+nobuffer",
                    "-flags",
                    "low_delay",
                    "-f",
                    "pulse",
                    "-fragment_size",
                    "2048",
                    "-i",
                    MONITOR,
                    "-ac",
                    "2",
                    "-ar",
                    "48000",
                    *enc,
                    *tail,
                ]
            ]
        return [
            [
                self._parec or "parec",
                f"--device={MONITOR}",
                "--format=s16le",
                "--rate=48000",
                "--channels=2",
                "--latency-msec=30",
            ],
            [self._ffmpeg or "ffmpeg", *common, "-f", "s16le", "-ar", "48000", "-ac", "2", "-i", "pipe:0", *enc, *tail],
        ]

    def open_stream(self, fmt: Optional[str] = None) -> AudioStream:
        if not self.available:
            raise RuntimeError(self.reason or "audio unavailable")
        if fmt not in CONTENT_TYPES or not self._caps.get(fmt):
            fmt = self.fmt
        if not self._slots.acquire(blocking=False):
            raise AudioBusy("too many listeners")
        procs: List[subprocess.Popen] = []
        try:
            env = dict(os.environ, PULSE_SERVER=self.server)
            prev = None
            for argv in self.encoder_command(fmt):
                p = subprocess.Popen(
                    argv,
                    env=env,
                    stdin=prev.stdout if prev else subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                if prev and prev.stdout:
                    prev.stdout.close()
                procs.append(p)
                prev = p
        except Exception:
            for p in procs:
                p.kill()
            self._slots.release()
            raise
        with self._lock:
            self._listeners += 1

        def release() -> None:
            with self._lock:
                self._listeners -= 1
            self._slots.release()

        stream = AudioStream(procs, release)
        stream.content_type = CONTENT_TYPES[fmt]  # type: ignore[attr-defined]
        return stream
