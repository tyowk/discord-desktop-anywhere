"""Configuration: one immutable object built from environment variables.

Every setting is documented in the README. Real environment variables always win
over values from an optional ``.env`` file in the project root.
"""

from __future__ import annotations

import os
import re
import secrets
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple

TRUE = {"1", "true", "yes", "on", "y"}
FALSE = {"0", "false", "no", "off", "n", ""}

#: channel -> (download host, binary name, config dir name, bootstrap channel argument)
CHANNELS: Dict[str, Tuple[str, str, str, str]] = {
    "ptb": ("ptb.discord.com", "DiscordPTB", "discordptb", "ptb"),
    "stable": ("discord.com", "Discord", "discord", "stable"),
    "canary": ("canary.discord.com", "DiscordCanary", "discordcanary", "canary"),
}


class ConfigError(ValueError):
    """Raised when a setting has an invalid value."""


@dataclass(frozen=True)
class Resolution:
    width: int
    height: int
    depth: int

    def __str__(self) -> str:  # the format Xvfb expects
        return f"{self.width}x{self.height}x{self.depth}"


def parse_resolution(value: str) -> Resolution:
    m = re.fullmatch(r"(\d{3,4})x(\d{3,4})(?:x(\d{2}))?", value.strip().lower())
    if not m:
        raise ConfigError(f"RESOLUTION must look like 1280x720 or 1280x720x24, got {value!r}")
    width, height, depth = int(m.group(1)), int(m.group(2)), int(m.group(3) or 24)
    if not (320 <= width <= 7680 and 240 <= height <= 4320):
        raise ConfigError("RESOLUTION is out of range (320x240 .. 7680x4320)")
    if depth not in (16, 24, 30):
        raise ConfigError("RESOLUTION colour depth must be 16, 24 or 30")
    return Resolution(width, height, depth)


def parse_bool(name: str, value: Optional[str], default: bool) -> bool:
    if value is None:
        return default
    v = value.strip().lower()
    if v in TRUE:
        return True
    if v in FALSE:
        return False
    raise ConfigError(f"{name} must be 0/1 (or true/false), got {value!r}")


def parse_int(name: str, value: Optional[str], default: int, lo: int, hi: int) -> int:
    if value is None or value.strip() == "":
        return default
    try:
        n = int(value)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from None
    if not lo <= n <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi}, got {n}")
    return n


def load_dotenv(path: Path) -> Dict[str, str]:
    """Tiny ``KEY=value`` parser (comments, quotes and ``export`` are supported)."""
    out: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, val = line.partition("=")
        if not sep:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        elif " #" in val:
            val = val.split(" #", 1)[0].rstrip()
        out[key.strip()] = val
    return out


def _default_data_dir(root: Path, env: Mapping[str, str]) -> Path:
    # Backwards compatible with v1: Replit keeps data in $HOME/workspace/.discord-novnc
    home = env.get("HOME")
    if home and (Path(home) / "workspace").is_dir():
        return Path(home) / "workspace" / ".discord-novnc"
    return root / ".discord-novnc"


def _on_replit(env: Mapping[str, str]) -> bool:
    return any(k in env for k in ("REPL_ID", "REPLIT_DEV_DOMAIN", "REPL_SLUG"))


@dataclass(frozen=True)
class Config:
    root: Path
    data_dir: Path
    channel: str
    resolution: Resolution
    display_num: int
    host: str
    port: int
    password: str
    password_generated: bool
    allow_no_auth: bool
    allow_cross_site: bool
    trust_proxy: bool
    allowed_origins: Tuple[str, ...]
    devtools: bool
    auto_devtools: bool
    screenshot: bool
    cdp_port: int
    vnc_port: int
    audio: bool
    audio_format: str
    audio_bitrate: int
    max_listeners: int
    max_shots: int
    max_shot_width: int
    installer_url: Optional[str]
    local_tarball: Optional[Path]
    extra_args: Tuple[str, ...]
    app_cmd: Optional[str]
    novnc_dir: Optional[str]
    on_replit: bool
    log_level: str

    # ---- derived paths -------------------------------------------------
    @property
    def app_dir(self) -> Path:
        return self.data_dir / "app"

    @property
    def profile_dir(self) -> Path:
        return self.data_dir / "profile"

    @property
    def config_home(self) -> Path:
        return self.profile_dir / "config"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def shot_dir(self) -> Path:
        return self.data_dir.parent / "screenshots" if self.on_replit else self.data_dir / "screenshots"

    @property
    def runtime_dir(self) -> Path:
        # Unix sockets are limited to ~108 bytes: fall back to /tmp for deep paths.
        p = self.data_dir / "runtime"
        if len(str(p)) > 70:
            p = Path("/tmp") / f"dda-{os.getuid()}"
        return p

    @property
    def channel_info(self) -> Tuple[str, str, str, str]:
        return CHANNELS[self.channel]

    @property
    def discord_config_dir(self) -> Path:
        return self.config_home / self.channel_info[2]

    @property
    def auth_enabled(self) -> bool:
        return bool(self.password)

    @property
    def public_url(self) -> str:
        env_domain = os.environ.get("REPLIT_DEV_DOMAIN")
        if self.on_replit and env_domain:
            return f"https://{env_domain}/"
        host = "localhost" if self.host in ("0.0.0.0", "::", "") else self.host
        return f"http://{host}:{self.port}/"

    # ---- construction --------------------------------------------------
    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None, root: Optional[Path] = None) -> "Config":
        root = (root or Path(__file__).resolve().parent.parent).resolve()
        merged: Dict[str, str] = dict(load_dotenv(root / ".env"))
        merged.update(os.environ if env is None else env)
        e = merged.get

        channel = (e("DISCORD_CHANNEL") or "ptb").strip().lower()
        if channel not in CHANNELS:
            raise ConfigError(f"DISCORD_CHANNEL must be one of {', '.join(CHANNELS)}, got {channel!r}")

        data_dir = Path(e("DDA_DATA_DIR") or _default_data_dir(root, merged)).expanduser().resolve()
        replit = _on_replit(merged)

        audio_format = (e("AUDIO_FORMAT") or "mp3").strip().lower()
        if audio_format not in ("mp3", "opus"):
            raise ConfigError("AUDIO_FORMAT must be mp3 or opus")

        allow_no_auth = parse_bool("ALLOW_NO_AUTH", e("ALLOW_NO_AUTH"), False)
        password = (e("VNC_PASSWORD") or e("DDA_PASSWORD") or "").strip()
        generated = False
        if not password and not allow_no_auth:
            password, generated = _load_or_create_password(data_dir)

        local_tar = e("LOCAL_TARBALL")
        if not local_tar:
            legacy = Path(merged.get("HOME", "~")).expanduser() / "workspace" / "discord-ptb.tar.gz"
            local_tar = str(legacy) if legacy.is_file() else None

        return cls(
            root=root,
            data_dir=data_dir,
            channel=channel,
            resolution=parse_resolution(e("RESOLUTION") or "960x540x24"),
            display_num=parse_int("DISPLAY_NUM", e("DISPLAY_NUM"), 99, 1, 999),
            host=(e("HOST") or ("0.0.0.0" if replit else "127.0.0.1")).strip(),
            port=parse_int("PORT", e("DDA_PORT") or e("PORT"), 6080, 1, 65535),
            password=password,
            password_generated=generated,
            allow_no_auth=allow_no_auth,
            allow_cross_site=parse_bool("ALLOW_CROSS_SITE", e("ALLOW_CROSS_SITE"), False),
            trust_proxy=parse_bool("TRUST_PROXY", e("TRUST_PROXY"), replit),
            allowed_origins=tuple(o.strip().rstrip("/") for o in (e("ALLOWED_ORIGINS") or "").split(",") if o.strip()),
            devtools=parse_bool("DEVTOOLS", e("DEVTOOLS"), False),
            auto_devtools=parse_bool("AUTO_DEVTOOLS", e("AUTO_DEVTOOLS"), False),
            screenshot=parse_bool("SCREENSHOT", e("SCREENSHOT"), True),
            cdp_port=parse_int("CDP_PORT", e("CDP_PORT"), 9222, 1024, 65535),
            vnc_port=parse_int("VNC_PORT", e("VNC_PORT"), 5900, 1024, 65535),
            audio=parse_bool("AUDIO", e("AUDIO"), True),
            audio_format=audio_format,
            audio_bitrate=parse_int("AUDIO_BITRATE", e("AUDIO_BITRATE"), 96, 16, 320),
            max_listeners=parse_int("MAX_LISTENERS", e("MAX_LISTENERS"), 3, 1, 16),
            max_shots=parse_int("MAX_SCREENSHOTS", e("MAX_SCREENSHOTS"), 40, 1, 1000),
            max_shot_width=parse_int("MAX_SHOT_WIDTH", e("MAX_SHOT_WIDTH"), 7680, 640, 15360),
            installer_url=e("PTB_URL") or e("INSTALLER_URL") or None,
            local_tarball=Path(local_tar).expanduser() if local_tar else None,
            extra_args=tuple(shlex.split(e("DISCORD_ARGS") or "")),
            app_cmd=e("APP_CMD") or None,
            novnc_dir=e("NOVNC_DIR") or None,
            on_replit=replit,
            log_level=(e("LOG_LEVEL") or "INFO").upper(),
        )


def _load_or_create_password(data_dir: Path) -> Tuple[str, bool]:
    """Secure by default: when no password is configured, create one and keep it."""
    path = data_dir / "password"
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing, True
    except OSError:
        pass
    pw = secrets.token_urlsafe(12)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(pw + "\n")
    except OSError:
        pass  # still usable for this run, just not persisted
    return pw, True
