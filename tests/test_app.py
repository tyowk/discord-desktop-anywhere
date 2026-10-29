import io
import json
import os
import tarfile

import pytest

from dda.app import DEVTOOLS_KEY, DiscordApp, InstallError, default_installer_url
from dda.config import Config


def cfg_for(tmp_path, **env):
    base = {"DDA_DATA_DIR": str(tmp_path / "data"), "HOME": str(tmp_path), "ALLOW_NO_AUTH": "1"}
    base.update(env)
    return Config.from_env(base, root=tmp_path)


def make_installer(path, padding=400_000):
    """A tarball shaped like Discord's installer: it holds an updater_bootstrap program."""
    script = b"""#!/bin/sh
# args: --no-zenity <config dir> <channel> <url>
mkdir -p "$2/app-1.0.0"
printf '#!/bin/sh\\nexit 0\\n' > "$2/app-1.0.0/DiscordPTB"
chmod +x "$2/app-1.0.0/DiscordPTB"
echo "app-1.0.0"
"""
    with tarfile.open(path, "w:gz") as tar:
        for name, data, mode in (
            ("DiscordPTB/updater_bootstrap", script, 0o755),
            ("DiscordPTB/padding.bin", os.urandom(padding), 0o644),
        ):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            tar.addfile(info, io.BytesIO(data))


def test_installer_urls_per_channel(tmp_path):
    assert "ptb.discord.com" in default_installer_url(cfg_for(tmp_path))
    assert "channel=canary" in default_installer_url(cfg_for(tmp_path, DISCORD_CHANNEL="canary"))


def test_full_install_from_local_tarball(tmp_path):
    tar = tmp_path / "installer.tar.gz"
    make_installer(tar)
    cfg = cfg_for(tmp_path, LOCAL_TARBALL=str(tar))
    app = DiscordApp(cfg, dict(os.environ), lambda *_: None)
    binary = app.ensure_installed()
    assert binary.name == "DiscordPTB" and binary.exists()
    assert os.access(binary, os.X_OK)
    # second run finds the existing install without re-extracting
    again = DiscordApp(cfg, dict(os.environ), lambda *_: None).ensure_installed()
    assert again == binary


def test_tiny_installer_is_rejected(tmp_path):
    tar = tmp_path / "bad.tar.gz"
    tar.write_bytes(b"<html>blocked</html>")
    app = DiscordApp(cfg_for(tmp_path, LOCAL_TARBALL=str(tar)), dict(os.environ), lambda *_: None)
    with pytest.raises(InstallError, match="too small"):
        app.ensure_installed()


def test_devtools_setting_toggles_and_preserves_other_keys(tmp_path):
    cfg = cfg_for(tmp_path)
    settings = cfg.discord_config_dir / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"keep": 1, DEVTOOLS_KEY: True}))
    DiscordApp(cfg, {}, lambda *_: None).write_settings()
    assert json.loads(settings.read_text()) == {"keep": 1}  # off by default: stale flag removed
    DiscordApp(cfg_for(tmp_path, DEVTOOLS="1"), {}, lambda *_: None).write_settings()
    assert json.loads(settings.read_text())[DEVTOOLS_KEY] is True


def test_launch_args(tmp_path):
    app = DiscordApp(cfg_for(tmp_path, DISCORD_ARGS="--foo=1"), {}, lambda *_: None)
    args = app.args()
    assert "--no-sandbox" in args and "--autoplay-policy=no-user-gesture-required" in args
    assert "--remote-debugging-port=9222" in args and "--foo=1" in args
    assert not any(
        "remote-debugging" in a for a in DiscordApp(cfg_for(tmp_path, SCREENSHOT="0"), {}, lambda *_: None).args()
    )
