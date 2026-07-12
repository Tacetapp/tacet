"""Optional voice fingerprint for mic bleed elimination.

Stores a single ECAPA-TDNN embedding of the user's voice to disk. At runtime,
the mic transcriber cosine-compares each VAD buffer against this embedding and
drops mismatches before whisper runs. Off by default; user opts in via the
settings UI.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime

import numpy as np

logger = logging.getLogger(__name__)

# Cosine similarity floor for "this is the enrolled user". Below = drop.
VOICE_FINGERPRINT_THRESHOLD = float(os.environ.get("VOICE_FINGERPRINT_THRESHOLD", "0.55"))
# Minimum enrollment sample duration — below this the embedding is unreliable.
VOICE_PROFILE_MIN_DURATION_S = float(os.environ.get("VOICE_PROFILE_MIN_DURATION_S", "5.0"))
# Minimum RMS during enrollment — reject saves that are just silence/noise.
VOICE_PROFILE_MIN_RMS = 0.01
# Minimum duration of a runtime buffer to score it — skip very short utterances.
VOICE_MATCH_MIN_DURATION_S = 2.0


def _default_dir() -> str:
    """Location for profile files — alongside user_profile.json at repo root."""
    return os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))


class VoiceProfile:
    """Single persisted voice embedding for the local user.

    One instance per process, handed to any WhisperTranscriber that needs it.
    Thread-safe: embedding is read under a lock so save/delete can't race with
    a concurrent match_score.
    """

    def __init__(self, speaker_embedder, profile_dir: str | None = None):
        self._speaker_embedder = speaker_embedder
        self._dir = profile_dir or _default_dir()
        self._npy_path = os.path.join(self._dir, "voice_profile.npy")
        self._meta_path = os.path.join(self._dir, "voice_profile.json")
        self._lock = threading.Lock()
        self._embedding: np.ndarray | None = None
        self._meta: dict = {}
        self._load()

    # ── Public API ──────────────────────────────────────

    def is_active(self) -> bool:
        return self._embedding is not None

    def metadata(self) -> dict:
        """Return {exists, created_at?, duration_s?} for the status WS reply."""
        if self._embedding is None:
            return {"exists": False}
        return {
            "exists": True,
            "created_at": self._meta.get("created_at", ""),
            "duration_s": self._meta.get("duration_s", 0.0),
        }

    def embed_and_save(self, audio: np.ndarray, sample_rate: int = 16000) -> dict:
        """Embed the given audio and persist. Raises ValueError on bad input."""
        if not isinstance(audio, np.ndarray) or audio.size == 0:
            raise ValueError("No audio provided")
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)
        duration_s = len(audio) / sample_rate
        if duration_s < VOICE_PROFILE_MIN_DURATION_S:
            raise ValueError(
                f"Recording too short ({duration_s:.1f}s). "
                f"Please record at least {VOICE_PROFILE_MIN_DURATION_S:.0f}s."
            )
        rms = float(np.sqrt(np.mean(audio ** 2)))
        if rms < VOICE_PROFILE_MIN_RMS:
            raise ValueError(
                "Your voice wasn't detected clearly. Try again in a quieter spot."
            )

        embedding = self._compute_embedding(audio)
        if embedding is None:
            raise ValueError("Couldn't extract a voice fingerprint from this recording.")

        meta = {
            "created_at": datetime.now().isoformat(),
            "duration_s": round(duration_s, 2),
            "sample_rate": sample_rate,
            "rms": round(rms, 4),
        }

        os.makedirs(self._dir, exist_ok=True)
        # Write atomically so a crash mid-save doesn't leave a corrupt profile.
        # np.save auto-appends .npy if the path doesn't end in .npy, so pass a
        # file handle to keep the tmp filename deterministic.
        tmp_npy = self._npy_path + ".tmp"
        tmp_meta = self._meta_path + ".tmp"
        with open(tmp_npy, "wb") as f:
            np.save(f, embedding, allow_pickle=False)
        with open(tmp_meta, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
        os.replace(tmp_npy, self._npy_path)
        os.replace(tmp_meta, self._meta_path)

        with self._lock:
            self._embedding = embedding
            self._meta = meta
        logger.info(f"Voice profile saved (duration={duration_s:.1f}s, rms={rms:.3f})")
        return self.metadata()

    def delete(self) -> None:
        with self._lock:
            self._embedding = None
            self._meta = {}
        for path in (self._npy_path, self._meta_path):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except Exception as e:
                logger.warning(f"Failed to delete {path}: {e}")
        logger.info("Voice profile deleted")

    def match_score(self, audio: np.ndarray, sample_rate: int = 16000) -> float | None:
        """Return cosine similarity of `audio` to the stored reference.

        Returns None if no profile exists, audio is too short, or embedding
        extraction fails. Caller (transcriber) compares against the threshold.
        """
        with self._lock:
            ref = self._embedding
        if ref is None:
            return None
        if audio is None or audio.size == 0:
            return None
        if len(audio) / sample_rate < VOICE_MATCH_MIN_DURATION_S:
            return None
        emb = self._compute_embedding(audio)
        if emb is None:
            return None
        return float(np.dot(emb, ref))

    # ── Internals ───────────────────────────────────────

    def _compute_embedding(self, audio: np.ndarray) -> np.ndarray | None:
        """Run ECAPA on `audio` and return a unit-normalized 192-dim vector."""
        if self._speaker_embedder is None:
            logger.warning("VoiceProfile has no SpeakerEmbedder — cannot embed")
            return None
        self._speaker_embedder._ensure_model()
        model = self._speaker_embedder._model
        if model is None:
            return None
        import torch
        waveform = torch.from_numpy(audio.astype(np.float32)).unsqueeze(0)
        embedding = model.encode_batch(waveform).squeeze().cpu().numpy()
        norm = np.linalg.norm(embedding)
        if norm < 1e-8:
            return None
        return embedding / norm

    def _load(self) -> None:
        """Populate embedding + metadata from disk if present."""
        if not os.path.isfile(self._npy_path):
            return
        try:
            emb = np.load(self._npy_path)
            if emb.ndim != 1:
                logger.warning(f"Unexpected voice profile shape: {emb.shape}")
                return
            norm = np.linalg.norm(emb)
            if norm < 1e-8:
                logger.warning("Voice profile has zero norm — ignoring")
                return
            # Ensure unit norm in memory (tolerate small drift on disk).
            emb = emb / norm
            meta = {}
            if os.path.isfile(self._meta_path):
                try:
                    with open(self._meta_path, encoding="utf-8") as f:
                        meta = json.load(f)
                except Exception as e:
                    logger.warning(f"Voice profile metadata unreadable: {e}")
            self._embedding = emb
            self._meta = meta
            logger.info(f"Voice profile loaded (created={meta.get('created_at', 'unknown')})")
        except Exception as e:
            logger.warning(f"Failed to load voice profile: {e}")
