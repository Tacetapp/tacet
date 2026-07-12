"""Tests for env-driven Whisper config: decode params + quality presets."""

from web.backend import whisper_backend as wb


def test_decode_config_defaults(monkeypatch):
    for key in (
        "WHISPER_LANGUAGE", "WHISPER_NO_SPEECH_THRESHOLD",
        "WHISPER_COMPRESSION_RATIO_THRESHOLD", "WHISPER_LOGPROB_THRESHOLD",
        "WHISPER_TEMPERATURE", "WHISPER_BEAM_SIZE",
        "WHISPER_CONDITION_ON_PREVIOUS_TEXT",
    ):
        monkeypatch.delenv(key, raising=False)
    cfg = wb.DecodeConfig.from_env()
    assert cfg.language == "en"
    assert cfg.no_speech_threshold == 0.6
    assert cfg.compression_ratio_threshold == 2.4
    assert cfg.logprob_threshold == -1.0
    assert cfg.temperature == 0.0
    assert cfg.beam_size == 1
    assert cfg.condition_on_previous_text is False


def test_decode_config_from_env(monkeypatch):
    monkeypatch.setenv("WHISPER_LANGUAGE", "es")
    monkeypatch.setenv("WHISPER_TEMPERATURE", "0.2")
    monkeypatch.setenv("WHISPER_BEAM_SIZE", "5")
    monkeypatch.setenv("WHISPER_CONDITION_ON_PREVIOUS_TEXT", "true")
    cfg = wb.DecodeConfig.from_env()
    assert cfg.language == "es"
    assert cfg.temperature == 0.2
    assert cfg.beam_size == 5
    assert cfg.condition_on_previous_text is True


def test_bad_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("WHISPER_TEMPERATURE", "not-a-number")
    monkeypatch.setenv("WHISPER_BEAM_SIZE", "")
    cfg = wb.DecodeConfig.from_env()
    assert cfg.temperature == 0.0
    assert cfg.beam_size == 1


def test_preset_model_unset_is_none(monkeypatch):
    monkeypatch.delenv("TRANSCRIPTION_QUALITY", raising=False)
    assert wb._preset_model("mlx") is None
    assert wb._preset_model("faster-whisper") is None


def test_preset_model_maps_per_backend(monkeypatch):
    monkeypatch.setenv("TRANSCRIPTION_QUALITY", "accurate")
    assert wb._preset_model("mlx") == "mlx-community/whisper-large-v3-mlx"
    assert wb._preset_model("faster-whisper") == "large-v3"
    monkeypatch.setenv("TRANSCRIPTION_QUALITY", "FAST")  # case-insensitive
    assert wb._preset_model("faster-whisper") == "distil-small.en"


def test_resolve_model_precedence(monkeypatch):
    monkeypatch.delenv("WHISPER_MLX_MODEL", raising=False)
    monkeypatch.delenv("WHISPER_MODEL", raising=False)
    # preset beats default, explicit beats preset, backend env beats both
    assert wb._resolve_model(None, "WHISPER_MLX_MODEL", "DEF", preset="PRE") == "PRE"
    assert wb._resolve_model("X", "WHISPER_MLX_MODEL", "DEF", preset="PRE") == "X"
    assert wb._resolve_model(None, "WHISPER_MLX_MODEL", "DEF", preset=None) == "DEF"
    monkeypatch.setenv("WHISPER_MLX_MODEL", "ENV")
    assert wb._resolve_model(None, "WHISPER_MLX_MODEL", "DEF", preset="PRE") == "ENV"
