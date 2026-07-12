"""Whisper transcription backends with automatic chip detection.

Apple Silicon Macs use mlx-whisper (Metal GPU acceleration). Intel Macs and
other platforms fall back to faster-whisper (CTranslate2, CPU). The backend is
chosen automatically from the CPU architecture; set WHISPER_BACKEND to "mlx" or
"faster-whisper" to override.

Model selection is configurable per backend (env vars, highest precedence first):
  - explicit `model` argument passed to create_whisper_backend()
  - WHISPER_MLX_MODEL / WHISPER_FASTER_MODEL  (backend-specific)
  - WHISPER_MODEL                             (applies to whichever backend runs)
  - TRANSCRIPTION_QUALITY preset (fast/balanced/accurate, mapped per backend)
  - the backend default below
Keeping the override per backend matters because the model names differ: mlx
wants an `mlx-community/...` repo, faster-whisper wants a plain size name like
`distil-small.en`. Other faster-whisper knobs: WHISPER_COMPUTE_TYPE (default
int8), WHISPER_DEVICE (default cpu), WHISPER_CPU_THREADS (default 0 = auto).

Decode behaviour is env-driven too (read once per backend instance; the desktop
app restarts the server on a settings change, so import-time/instance-time reads
are fine): WHISPER_LANGUAGE, WHISPER_NO_SPEECH_THRESHOLD,
WHISPER_COMPRESSION_RATIO_THRESHOLD, WHISPER_LOGPROB_THRESHOLD,
WHISPER_TEMPERATURE, WHISPER_BEAM_SIZE (faster-whisper only),
WHISPER_CONDITION_ON_PREVIOUS_TEXT. Every key here has a matching field in
electron/config.js so the Settings UI can surface it.
"""

from __future__ import annotations

import logging
import os
import platform
import threading
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)

# Per-backend default models. mlx runs on the GPU so it can afford large-v3-turbo;
# faster-whisper runs on the CPU, where large models fall far behind real-time, so
# it defaults to the distilled English model for a usable live transcription rate.
MLX_DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"
FASTER_DEFAULT_MODEL = "distil-small.en"

# Friendly quality presets surfaced in the UI (Fast / Balanced / Accurate),
# mapped to a concrete model per backend. "balanced" / "fast" mirror today's
# defaults so an unset (Auto) preset is a no-op; "accurate" trades speed for the
# full large model. Power users can still pin an exact model via the
# WHISPER_*_MODEL env vars, which take precedence over the preset.
QUALITY_PRESETS: dict[str, dict[str, str]] = {
    "mlx": {
        "fast": "mlx-community/whisper-small.en-mlx",
        "balanced": MLX_DEFAULT_MODEL,
        "accurate": "mlx-community/whisper-large-v3-mlx",
    },
    "faster-whisper": {
        "fast": FASTER_DEFAULT_MODEL,
        "balanced": "distil-large-v3",
        "accurate": "large-v3",
    },
}


def _float_env(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, "").strip() or default)
    except (TypeError, ValueError):
        return default


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "").strip() or default)
    except (TypeError, ValueError):
        return default


def _bool_env(key: str, default: bool) -> bool:
    raw = os.environ.get(key, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass
class DecodeConfig:
    """Env-driven Whisper decode parameters, shared by both backends."""

    language: str
    no_speech_threshold: float
    compression_ratio_threshold: float
    logprob_threshold: float
    temperature: float
    beam_size: int
    condition_on_previous_text: bool
    word_timestamps: bool

    @classmethod
    def from_env(cls) -> "DecodeConfig":
        return cls(
            language=(os.environ.get("WHISPER_LANGUAGE", "").strip() or "en"),
            no_speech_threshold=_float_env("WHISPER_NO_SPEECH_THRESHOLD", 0.6),
            compression_ratio_threshold=_float_env("WHISPER_COMPRESSION_RATIO_THRESHOLD", 2.4),
            logprob_threshold=_float_env("WHISPER_LOGPROB_THRESHOLD", -1.0),
            temperature=_float_env("WHISPER_TEMPERATURE", 0.0),
            beam_size=_int_env("WHISPER_BEAM_SIZE", 1),
            # Default OFF: feeding Whisper's own prior output re-introduces
            # repetition/hallucination loops, and for our discrete VAD buffers
            # (each ≤ one 30s window) it does nothing anyway. Exposed for parity.
            condition_on_previous_text=_bool_env("WHISPER_CONDITION_ON_PREVIOUS_TEXT", False),
            # Default OFF: groundwork for word-level features (click-a-word →
            # audio position, tighter dedup alignment). Costs some decode speed.
            # We still return the same {text,start,end} segment dicts — the extra
            # per-word data is decoded but not yet surfaced.
            word_timestamps=_bool_env("WHISPER_WORD_TIMESTAMPS", False),
        )


def _preset_model(backend: str) -> str | None:
    """Resolve TRANSCRIPTION_QUALITY (fast/balanced/accurate) to a model for this
    backend, or None when unset / unrecognised (→ fall through to the default)."""
    preset = os.environ.get("TRANSCRIPTION_QUALITY", "").strip().lower()
    if not preset:
        return None
    return QUALITY_PRESETS.get(backend, {}).get(preset)


def _resolve_model(explicit: str | None, backend_env: str, default: str,
                   preset: str | None = None) -> str:
    """Pick the model name by precedence: explicit arg > backend env >
    global WHISPER_MODEL > quality preset > backend default."""
    return (
        explicit
        or os.environ.get(backend_env)
        or os.environ.get("WHISPER_MODEL")
        or preset
        or default
    )


def detect_backend() -> str:
    """Return the transcription backend for this machine.

    WHISPER_BACKEND overrides auto-detection ("mlx" or "faster-whisper").
    Apple Silicon → mlx; everything else → faster-whisper.
    """
    override = os.environ.get("WHISPER_BACKEND", "").strip().lower().replace("_", "-")
    if override in ("mlx", "faster-whisper"):
        return override

    is_apple_silicon = (
        platform.system() == "Darwin" and platform.machine().lower() == "arm64"
    )
    return "mlx" if is_apple_silicon else "faster-whisper"


class WhisperBackend:
    """Common interface: turn a float32 16kHz mono buffer into raw segments.

    transcribe() returns a list of {"text", "start", "end"} dicts where start/end
    are seconds relative to the start of the audio buffer. `initial_prompt`
    biases decoding toward supplied vocabulary (names/jargon); `language` falls
    back to the env-configured default when None.
    """

    name = "base"

    def transcribe(self, audio: np.ndarray, language: str | None = None,
                   initial_prompt: str | None = None) -> list[dict]:
        raise NotImplementedError


class MLXWhisperBackend(WhisperBackend):
    """mlx-whisper on Apple Silicon (Metal GPU)."""

    name = "mlx"

    def __init__(self, model: str | None = None):
        self._model = _resolve_model(
            model, "WHISPER_MLX_MODEL", MLX_DEFAULT_MODEL, preset=_preset_model("mlx")
        )
        self._local_model_path: str | None = None
        self._decode = DecodeConfig.from_env()

    def _resolve_model_path(self) -> None:
        """Resolve HuggingFace repo to local cached path (one-time)."""
        if self._local_model_path is not None:
            return
        try:
            from huggingface_hub import snapshot_download
            self._local_model_path = snapshot_download(
                self._model, local_files_only=True
            )
            logger.info(f"Whisper model resolved to: {self._local_model_path}")
        except Exception:
            self._local_model_path = self._model
            logger.info(f"Using HuggingFace repo: {self._model}")

    def transcribe(self, audio: np.ndarray, language: str | None = None,
                   initial_prompt: str | None = None) -> list[dict]:
        import mlx_whisper

        self._resolve_model_path()
        result = mlx_whisper.transcribe(
            audio,
            path_or_hf_repo=self._local_model_path or self._model,
            language=language or self._decode.language,
            initial_prompt=initial_prompt or None,
            condition_on_previous_text=self._decode.condition_on_previous_text,
            no_speech_threshold=self._decode.no_speech_threshold,
            compression_ratio_threshold=self._decode.compression_ratio_threshold,
            logprob_threshold=self._decode.logprob_threshold,
            word_timestamps=self._decode.word_timestamps,
            temperature=self._decode.temperature,
            verbose=False,
        )
        return [
            {"text": s["text"], "start": s["start"], "end": s["end"]}
            for s in result.get("segments", [])
        ]


# faster-whisper models are expensive to load, so share one per
# (model, compute_type) across the mic and system transcribers rather than
# loading the same model into memory twice.
_FASTER_MODELS: dict[tuple[str, str], object] = {}
_FASTER_MODELS_LOCK = threading.Lock()


class FasterWhisperBackend(WhisperBackend):
    """faster-whisper (CTranslate2) for Intel Macs and other CPU-only hosts."""

    name = "faster-whisper"

    def __init__(self, model: str | None = None):
        self._model_name = _resolve_model(
            model, "WHISPER_FASTER_MODEL", FASTER_DEFAULT_MODEL,
            preset=_preset_model("faster-whisper"),
        )
        self._compute_type = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")
        self._device = os.environ.get("WHISPER_DEVICE", "cpu")
        # 0 lets CTranslate2 pick (≈ physical core count); override for tuning.
        self._cpu_threads = int(os.environ.get("WHISPER_CPU_THREADS", "0"))
        self._decode = DecodeConfig.from_env()

    def _get_model(self):
        key = (self._model_name, self._compute_type)
        model = _FASTER_MODELS.get(key)
        if model is not None:
            return model
        with _FASTER_MODELS_LOCK:
            model = _FASTER_MODELS.get(key)
            if model is None:
                from faster_whisper import WhisperModel
                logger.info(
                    f"Loading faster-whisper model {self._model_name!r} "
                    f"(device={self._device}, compute_type={self._compute_type})"
                )
                model = WhisperModel(
                    self._model_name,
                    device=self._device,
                    compute_type=self._compute_type,
                    cpu_threads=self._cpu_threads,
                )
                _FASTER_MODELS[key] = model
        return model

    def transcribe(self, audio: np.ndarray, language: str | None = None,
                   initial_prompt: str | None = None) -> list[dict]:
        model = self._get_model()
        # beam_size defaults to 1 (greedy) to keep latency low on CPU.
        segments, _info = model.transcribe(
            audio,
            language=language or self._decode.language,
            initial_prompt=initial_prompt or None,
            beam_size=self._decode.beam_size,
            condition_on_previous_text=self._decode.condition_on_previous_text,
            no_speech_threshold=self._decode.no_speech_threshold,
            compression_ratio_threshold=self._decode.compression_ratio_threshold,
            log_prob_threshold=self._decode.logprob_threshold,
            word_timestamps=self._decode.word_timestamps,
            temperature=self._decode.temperature,
        )
        return [{"text": s.text, "start": s.start, "end": s.end} for s in segments]


def create_whisper_backend(model: str | None = None) -> WhisperBackend:
    """Build the transcription backend for this machine (chip-auto-detected)."""
    backend = detect_backend()
    if backend == "mlx":
        logger.info("Using mlx-whisper backend (Apple Silicon)")
        return MLXWhisperBackend(model)
    logger.info("Using faster-whisper backend (CPU)")
    return FasterWhisperBackend(model)
