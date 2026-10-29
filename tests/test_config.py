import pytest

from dda.config import Config, ConfigError, load_dotenv, parse_resolution


def make(tmp_path, **env):
    base = {"DDA_DATA_DIR": str(tmp_path / "data"), "HOME": str(tmp_path)}
    base.update(env)
    return Config.from_env(base, root=tmp_path)


def test_defaults(tmp_path):
    cfg = make(tmp_path)
    assert cfg.channel == "ptb"
    assert (cfg.port, cfg.host) == (6080, "127.0.0.1")
    assert str(cfg.resolution) == "960x540x24"
    assert cfg.audio and cfg.audio_format == "mp3"
    assert not cfg.devtools and cfg.screenshot


def test_password_generated_and_persisted(tmp_path):
    first = make(tmp_path)
    assert first.password_generated and len(first.password) >= 12
    assert make(tmp_path).password == first.password
    assert ((tmp_path / "data" / "password").stat().st_mode & 0o777) == 0o600


def test_explicit_password_and_no_auth(tmp_path):
    assert make(tmp_path, VNC_PASSWORD="s3cret").password == "s3cret"
    open_cfg = make(tmp_path, ALLOW_NO_AUTH="1")
    assert not open_cfg.auth_enabled


def test_allow_cross_site_flag(tmp_path):
    assert not make(tmp_path).allow_cross_site
    assert make(tmp_path, ALLOW_CROSS_SITE="1").allow_cross_site


def test_replit_defaults(tmp_path):
    cfg = make(tmp_path, REPL_ID="x", VNC_PASSWORD="p")
    assert cfg.on_replit and cfg.host == "0.0.0.0" and cfg.trust_proxy


@pytest.mark.parametrize("value", ["abc", "100x100", "1280x720x8", "99999x99999"])
def test_bad_resolution(value):
    with pytest.raises(ConfigError):
        parse_resolution(value)


def test_resolution_ok():
    assert str(parse_resolution("1280x720")) == "1280x720x24"


@pytest.mark.parametrize(
    "key,val", [("DISCORD_CHANNEL", "nope"), ("AUDIO_FORMAT", "wav"), ("PORT", "70000"), ("DEVTOOLS", "maybe")]
)
def test_invalid_values(tmp_path, key, val):
    with pytest.raises(ConfigError):
        make(tmp_path, **{key: val})


def test_dotenv(tmp_path):
    (tmp_path / ".env").write_text("# c\nexport RESOLUTION='1280x720'\nDEVTOOLS=1 # on\nVNC_PASSWORD=fromfile\n")
    assert load_dotenv(tmp_path / ".env")["DEVTOOLS"] == "1"
    cfg = Config.from_env({"DDA_DATA_DIR": str(tmp_path / "d"), "VNC_PASSWORD": "real"}, root=tmp_path)
    assert str(cfg.resolution) == "1280x720x24" and cfg.devtools
    assert cfg.password == "real"  # real environment beats .env
