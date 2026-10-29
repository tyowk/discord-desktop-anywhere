from dda.config import Config
from dda.system import find_novnc


def cfg_for(tmp_path, **env):
    base = {"DDA_DATA_DIR": str(tmp_path / "data"), "HOME": str(tmp_path), "ALLOW_NO_AUTH": "1"}
    base.update(env)
    return Config.from_env(base, root=tmp_path)


def test_find_novnc_honours_override(tmp_path):
    d = tmp_path / "nv"
    d.mkdir()
    (d / "vnc.html").write_text("x")
    assert find_novnc(cfg_for(tmp_path, NOVNC_DIR=str(d))) == d


def test_find_novnc_in_data_dir(tmp_path):
    d = tmp_path / "data" / "novnc"
    d.mkdir(parents=True)
    (d / "vnc.html").write_text("x")
    assert find_novnc(cfg_for(tmp_path, NOVNC_DIR="/nonexistent")) in (d, find_novnc(cfg_for(tmp_path)))
