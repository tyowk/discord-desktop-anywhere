from dda.audio import AudioManager, build_encoder_args
from dda.config import Config


def mgr(tmp_path, **env):
    base = {"DDA_DATA_DIR": str(tmp_path / "d"), "HOME": str(tmp_path), "ALLOW_NO_AUTH": "1"}
    base.update(env)
    return AudioManager(Config.from_env(base, root=tmp_path))


def test_encoder_args():
    assert "libmp3lame" in build_encoder_args("mp3", 96) and "96k" in build_encoder_args("mp3", 96)
    opus = build_encoder_args("opus", 64)
    assert "libopus" in opus and "20000" in opus


def test_pipeline_uses_ffmpeg_pulse_when_available(tmp_path):
    m = mgr(tmp_path)
    m._caps = {"pulse": True, "mp3": True, "opus": True}
    (cmd,) = m.encoder_command("mp3")
    assert "pulse" in cmd and "dda.monitor" in cmd and cmd[-1] == "pipe:1"


def test_pipeline_falls_back_to_parec(tmp_path):
    m = mgr(tmp_path)
    m._caps = {"pulse": False, "mp3": True, "opus": False}
    parec, ffmpeg = m.encoder_command("mp3")
    assert "--device=dda.monitor" in parec and "pipe:0" in ffmpeg


def test_disabled_audio_reports_reason(tmp_path):
    m = mgr(tmp_path, AUDIO="0")
    assert m.build_service() is None and not m.available and "disabled" in m.reason
