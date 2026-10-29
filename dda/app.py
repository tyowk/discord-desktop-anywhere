"""Installing and launching the Discord desktop client."""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional

from dda.config import Config
from dda.procs import Service
from dda.system import USER_AGENT, glibc_loader, is_nixlike, which

log = logging.getLogger("dda.app")

DEVTOOLS_KEY = "DANGEROUS_ENABLE_DEVTOOLS_ONLY_ENABLE_IF_YOU_KNOW_WHAT_YOURE_DOING"
MIN_INSTALLER_BYTES = 300_000


class InstallError(RuntimeError):
    pass


def default_installer_url(cfg: Config) -> str:
    host, _binary, _cfgdir, channel = cfg.channel_info
    return (
        f"https://{host}/api/downloads/distributions/app/installers/latest"
        f"?channel={channel}&platform=linux&arch=x64&format=tar.gz"
    )


def download(url: str, dest: Path, progress: Optional[Callable[[str], None]] = None, retries: int = 3) -> int:
    """Download ``url`` to ``dest`` atomically, with retries. Returns the size in bytes."""
    part = dest.with_suffix(dest.suffix + ".part")
    last: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp, open(part, "wb") as out:
                total = int(resp.headers.get("Content-Length") or 0)
                done, last_pct = 0, -10
                while True:
                    block = resp.read(256 * 1024)
                    if not block:
                        break
                    out.write(block)
                    done += len(block)
                    if progress and total:
                        pct = done * 100 // total
                        if pct >= last_pct + 10:
                            last_pct = pct
                            progress(f"{pct}% ({done // 1024} KiB)")
            os.replace(part, dest)
            return done
        except (urllib.error.URLError, OSError) as exc:
            last = exc
            log.warning("download attempt %d/%d failed: %s", attempt, retries, exc)
            time.sleep(2 * attempt)
    raise InstallError(f"could not download {url}: {last}")


class ElfPatcher(threading.Thread):
    """Nix/Replit only: give freshly downloaded ELF binaries a loader that exists here."""

    def __init__(self, root: Path, loader: str, duration: float = 900.0, interval: float = 3.0) -> None:
        super().__init__(name="elf-patcher", daemon=True)
        self.root, self.loader, self.duration, self.interval = root, loader, duration, interval
        self._seen: Dict[str, float] = {}
        self._halt = threading.Event()

    def patch_once(self) -> int:
        patched = 0
        for dirpath, _dirs, files in os.walk(self.root):
            for fname in files:
                if ".so" in fname:
                    continue
                path = os.path.join(dirpath, fname)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if not st.st_mode & 0o100 or self._seen.get(path) == st.st_mtime:
                    continue
                self._seen[path] = st.st_mtime
                try:
                    with open(path, "rb") as fh:
                        if fh.read(4) != b"\x7fELF":
                            continue
                except OSError:
                    continue
                interp = subprocess.run(["patchelf", "--print-interpreter", path], capture_output=True, text=True)
                if interp.returncode == 0 and interp.stdout.strip() and not os.path.exists(interp.stdout.strip()):
                    res = subprocess.run(["patchelf", "--set-interpreter", self.loader, path], capture_output=True)
                    if res.returncode == 0:
                        self._seen[path] = os.stat(path).st_mtime
                        patched += 1
        return patched

    def run(self) -> None:
        deadline = time.monotonic() + self.duration
        while time.monotonic() < deadline and not self._halt.wait(self.interval):
            self.patch_once()

    def stop(self) -> None:
        self._halt.set()


class DiscordApp:
    def __init__(self, cfg: Config, env: Dict[str, str], set_status: Callable[[str, str], None]) -> None:
        self.cfg = cfg
        self.env = env
        self.set_status = set_status
        self.binary: Optional[Path] = None
        self.patcher: Optional[ElfPatcher] = None
        self.loader = glibc_loader() if (is_nixlike() and which("patchelf")) else None

    # ---- installation ---------------------------------------------------
    def _find_binary(self) -> Optional[Path]:
        name = self.cfg.channel_info[1]
        hits = sorted(self.cfg.discord_config_dir.glob(f"*/{name}")) if self.cfg.discord_config_dir.is_dir() else []
        hits += sorted(self.cfg.discord_config_dir.glob(f"*/*/{name}")) if self.cfg.discord_config_dir.is_dir() else []
        return hits[-1] if hits else None

    def _patch(self, path: Path) -> None:
        if self.loader and path.exists():
            subprocess.run(["patchelf", "--set-interpreter", self.loader, str(path)], capture_output=True)

    def ensure_installed(self) -> Path:
        if self.cfg.app_cmd:
            return Path(self.cfg.app_cmd)
        if platform.machine().lower() not in ("x86_64", "amd64"):
            raise InstallError(f"Discord ships Linux x86_64 builds only; this machine is {platform.machine()}")
        if self.loader:
            self.patcher = ElfPatcher(self.cfg.data_dir, self.loader)
        existing = self._find_binary()
        if existing:
            self.binary = existing
            self._start_patcher()
            return existing
        self._extract_installer()
        boot = next(iter(sorted(self.cfg.app_dir.rglob("updater_bootstrap"))), None)
        if not boot:
            raise InstallError(f"updater_bootstrap not found in {self.cfg.app_dir}")
        boot.chmod(0o755)
        self._patch(boot)
        self.cfg.discord_config_dir.mkdir(parents=True, exist_ok=True)
        self.set_status("downloading", "Downloading Discord (this can take several minutes)...")
        log.info("running updater_bootstrap (downloads the full client)")
        self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.cfg.log_dir / "bootstrap.err"
        with open(log_path, "wb") as err:
            res = subprocess.run(
                [
                    str(boot),
                    "--no-zenity",
                    str(self.cfg.discord_config_dir),
                    self.cfg.channel_info[3],
                    "https://updates.discord.com/",
                ],
                env=self.env,
                stdout=subprocess.PIPE,
                stderr=err,
                text=True,
                timeout=1800,
            )
        out = res.stdout.strip().splitlines()
        log.info("bootstrap exit=%s output=%r", res.returncode, out[-1] if out else "")
        self._start_patcher()
        self._patch_tree_once()
        candidate = self.cfg.discord_config_dir / out[-1] / self.cfg.channel_info[1] if out else None
        binary = candidate if candidate and candidate.exists() else self._find_binary()
        if not binary:
            raise InstallError(f"Discord binary not found after bootstrap; see {log_path}")
        self.binary = binary
        return binary

    def _start_patcher(self) -> None:
        if self.patcher and not self.patcher.is_alive():
            self.patcher.start()

    def _patch_tree_once(self) -> None:
        if self.patcher:
            self.patcher.patch_once()

    def _extract_installer(self) -> None:
        cfg = self.cfg
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        tarball = cfg.data_dir / "installer.tar.gz"
        if tarball.exists():
            tarball.unlink()
        if cfg.local_tarball and cfg.local_tarball.is_file():
            log.info("using local tarball %s", cfg.local_tarball)
            shutil.copyfile(cfg.local_tarball, tarball)
        else:
            url = cfg.installer_url or default_installer_url(cfg)
            self.set_status("downloading", "Downloading the Discord installer...")
            log.info("downloading installer from %s", url)
            download(url, tarball, progress=lambda m: log.info("installer: %s", m))
        size = tarball.stat().st_size
        if size < MIN_INSTALLER_BYTES:
            head = tarball.read_bytes()[:300]
            tarball.unlink()
            raise InstallError(f"installer is too small ({size} bytes); server said: {head!r}")
        if cfg.app_dir.exists():
            shutil.rmtree(cfg.app_dir)
        cfg.app_dir.mkdir(parents=True)
        res = subprocess.run(["tar", "-xzf", str(tarball), "-C", str(cfg.app_dir)], capture_output=True, text=True)
        if tarball.exists():
            tarball.unlink()
        if res.returncode != 0:
            raise InstallError(f"extracting the installer failed: {res.stderr.strip()}")

    # ---- settings & launch ----------------------------------------------
    def write_settings(self) -> None:
        """Enable/disable the Discord DevTools flag (and clear flags from older versions)."""
        path = self.cfg.discord_config_dir / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        if self.cfg.devtools:
            data[DEVTOOLS_KEY] = True
        else:
            data.pop(DEVTOOLS_KEY, None)
        path.write_text(json.dumps(data, indent=2))

    def args(self) -> List[str]:
        args = [
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--disable-software-rasterizer",
            "--autoplay-policy=no-user-gesture-required",
        ]
        if self.cfg.screenshot:
            args.append(f"--remote-debugging-port={self.cfg.cdp_port}")
        args.extend(self.cfg.extra_args)
        return args

    def service(self) -> Service:
        if self.cfg.app_cmd:
            argv = [*self.cfg.app_cmd.split()]
        else:
            assert self.binary is not None
            argv = [str(self.binary), *self.args()]
            lib_dir = str(self.binary.parent)
            self.env["LD_LIBRARY_PATH"] = lib_dir + (
                ":" + self.env["LD_LIBRARY_PATH"] if self.env.get("LD_LIBRARY_PATH") else ""
            )
            missing = self._missing_libs()
            if missing:
                log.warning("missing shared libraries (run install.sh):\n%s", missing)
        return Service("discord", argv, env=self.env, log_path=self.cfg.log_dir / "discord.log", restart="always")

    def _missing_libs(self) -> str:
        if not which("ldd") or not self.binary:
            return ""
        out = subprocess.run(["ldd", str(self.binary)], env=self.env, capture_output=True, text=True).stdout
        return "\n".join(line.strip() for line in out.splitlines() if "not found" in line)


def fit_window_later(cfg: Config, env: Dict[str, str], stop: threading.Event) -> threading.Thread:
    """Move/resize the Discord window to the virtual screen once it appears (openbox rule is the main mechanism)."""

    def work() -> None:
        if not which("xdotool"):
            return
        wid = ""
        for _ in range(180):
            if stop.is_set():
                return
            out = subprocess.run(
                ["xdotool", "search", "--onlyvisible", "--name", "Discord"], env=env, capture_output=True, text=True
            ).stdout.split()
            if out:
                wid = out[-1]
                break
            time.sleep(1)
        if not wid:
            return
        time.sleep(4)
        subprocess.run(
            [
                "xdotool",
                "windowmove",
                wid,
                "0",
                "0",
                "windowsize",
                wid,
                str(cfg.resolution.width),
                str(cfg.resolution.height),
            ],
            env=env,
            capture_output=True,
        )
        if cfg.devtools and cfg.auto_devtools:
            time.sleep(8)
            subprocess.run(["xdotool", "windowfocus", "--sync", wid], env=env, capture_output=True)
            subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+shift+i"], env=env, capture_output=True)

    t = threading.Thread(target=work, name="window-fit", daemon=True)
    t.start()
    return t
