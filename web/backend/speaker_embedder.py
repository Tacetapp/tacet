"""Speaker embedding extraction and online clustering using speechbrain ECAPA-TDNN."""

from __future__ import annotations

import logging
import os
import re
import threading

import numpy as np

logger = logging.getLogger(__name__)

# A diarization cluster key ("Speaker 3"), as opposed to a real name assigned by
# the user or seeded from the persistent registry. Mirrors engine._CLUSTER_RE so
# the reconcile pass can keep named clusters as canonical labels (see below).
_SPEAKER_N_RE = re.compile(r"^Speaker \d+$")


def _is_speaker_n(label: str) -> bool:
    """True iff `label` is an anonymous "Speaker N" cluster key (not a real name)."""
    return bool(_SPEAKER_N_RE.match(label))


def _f(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, "").strip() or default)
    except (TypeError, ValueError):
        return default


# All env-overridable; matching fields live in electron/config.js. Defaults
# preserve prior hardcoded behaviour.
#
# cosine similarity threshold for same-speaker match. ECAPA on short ~2s clips
# routinely scores the *same* voice at 0.55–0.65, so a 0.65 bar over-splits a
# single speaker into many "Speaker N" clusters. 0.55 keeps genuinely distinct
# voices apart (cross-speaker pairs cluster well below 0.5) while collapsing the
# same-voice fragments that were spawning above 0.55.
SIMILARITY_THRESHOLD = _f("SPEAKER_SIMILARITY_THRESHOLD", 0.55)
EMBEDDING_UPDATE_RATE = _f("SPEAKER_EMBEDDING_UPDATE_RATE", 0.1)  # running-avg update weight
MIN_AUDIO_DURATION = _f("SPEAKER_MIN_AUDIO_DURATION", 2.0)  # min seconds to spawn a NEW cluster
# Shorter clips (down to this) can still be MATCHED to an existing speaker, but
# never spawn a NEW cluster. This recovers a known speaker's quick "yeah"/"right"
# interjections (currently dropped under the 2s floor) without over-fragmenting
# the speaker set on brief, ambiguous audio.
MIN_EMBED_DURATION = _f("SPEAKER_MIN_EMBED_DURATION", 1.0)
# Agglomerative cosine-distance threshold for the reconcile pass. Must stay
# strictly LOOSER (larger) than 1 - SIMILARITY_THRESHOLD, or the global merge
# can't consolidate anything the greedy live pass didn't already merge — it
# goes redundant (observed: 22 online clusters → 22 merged groups, a no-op).
# At 0.45 (== 1 - 0.55) it sat exactly on the live bar and merged nothing.
# 0.55 ≈ 0.45 similarity: the global pass now collapses the 0.45–0.55-sim
# fragments the live pass leaves split, pulling toward the true headcount.
# This is the safer lever than dropping SIMILARITY_THRESHOLD: the reconcile is
# global (sees all utterances at once) and only relabels in a consolidation
# pass, vs. greedy order-dependent live merges that can't be undone.
RECLUSTER_DISTANCE = _f("SPEAKER_RECLUSTER_DISTANCE", 0.55)
EMBEDDING_MODEL = os.environ.get(
    "SPEAKER_EMBEDDING_MODEL", "speechbrain/spkrec-ecapa-voxceleb"
)


def ecapa_savedir() -> str:
    """Writable cache dir for the downloaded ECAPA weights.

    Defaults to repo-local ``models/`` in dev; the desktop app points MODELS_DIR
    at a writable userData dir when packaged (the app's Resources are read-only,
    so the old hardcoded relative path failed there).
    """
    return os.path.join(os.environ.get("MODELS_DIR", "models"), "speechbrain-ecapa")


def _shim_torch_amp_custom_fwd() -> None:
    """Compat shim: speechbrain >=1.1 calls ``torch.amp.custom_fwd(fwd,
    device_type=..., cast_inputs=...)``, which exists only in torch >=2.4 — but
    torch is capped at 2.2 here (torch 2.3+ ships no Intel-mac wheels, and this
    project supports Intel Macs). We run ECAPA on CPU in fp32, where the AMP
    decorator is a no-op, so a pass-through shim is safe: delegate to the old
    ``torch.cuda.amp.custom_fwd`` for cuda, identity for everything else.
    Remove once torch >=2.4 can be required across both architectures.
    """
    import torch

    if hasattr(torch.amp, "custom_fwd"):
        return

    def _custom(old):
        def shim(fwd=None, *, device_type=None, cast_inputs=None):
            if device_type == "cuda":
                if fwd is None:
                    return old(cast_inputs=cast_inputs)
                return old(fwd, cast_inputs=cast_inputs)
            # CPU / other device: AMP casting doesn't apply — identity.
            if fwd is None:
                return lambda f: f
            return fwd
        return shim

    torch.amp.custom_fwd = _custom(torch.cuda.amp.custom_fwd)
    torch.amp.custom_bwd = _custom(torch.cuda.amp.custom_bwd)


class SpeakerEmbedder:
    """Extract speaker embeddings and assign consistent speaker IDs within a meeting.

    Uses speechbrain's ECAPA-TDNN model on CPU. Safe to run concurrently with
    mlx-whisper on Metal GPU since they use different compute backends.
    """

    def __init__(self):
        self._model = None
        self._model_lock = threading.Lock()
        self._known_speakers: dict[str, np.ndarray] = {}  # "Speaker 1" → avg embedding
        # Guards _known_speakers. identify() runs on a transcriber thread while
        # seeding (meeting start) and centroid reads (confirm_speaker, on the
        # event loop) touch the same dict — the lock keeps a read from observing
        # a half-updated centroid.
        self._speakers_lock = threading.Lock()
        # Per-utterance history for end-of-meeting re-clustering.
        # Each entry: (assigned_cluster_label, normalized_embedding).
        self._history: list[tuple[str, np.ndarray]] = []
        self._history_lock = threading.Lock()

    def _ensure_model(self):
        """Lazy-load speechbrain ECAPA-TDNN model (first call only).

        Thread-safe: multiple threads may call identify() concurrently;
        only one will load the model while others wait.
        """
        if self._model is not None:
            return
        with self._model_lock:
            if self._model is not None:  # double-check after acquiring lock
                return
            _shim_torch_amp_custom_fwd()
            from speechbrain.inference.speaker import EncoderClassifier
            logger.info("Loading speechbrain ECAPA-TDNN speaker embedding model...")
            self._model = EncoderClassifier.from_hparams(
                source=EMBEDDING_MODEL,
                savedir=ecapa_savedir(),
                run_opts={"device": "cpu"},
            )
            logger.info("Loaded speechbrain ECAPA-TDNN speaker embedding model")

    def identify(self, audio: np.ndarray, sample_rate: int = 16000) -> str | None:
        """Extract embedding from audio and assign a speaker ID.

        Returns "Speaker 1", "Speaker 2", etc. or None if audio too short.
        Called from a thread (CPU-only, safe to run alongside Metal GPU).

        Short clips (MIN_EMBED_DURATION ≤ duration < MIN_AUDIO_DURATION) can only
        be *matched* to an existing speaker, never spawn a new cluster.
        """
        duration = len(audio) / sample_rate
        if duration < MIN_EMBED_DURATION:
            return None

        self._ensure_model()

        import torch
        waveform = torch.from_numpy(audio).unsqueeze(0)  # [1, samples]
        embedding = self._model.encode_batch(waveform).squeeze().cpu().numpy()

        # Normalize
        norm = np.linalg.norm(embedding)
        if norm < 1e-8:
            return None
        embedding = embedding / norm

        # Compare to known speakers (locked: seeding/reads may run concurrently).
        with self._speakers_lock:
            best_match, best_score = None, 0.0
            for name, ref_emb in self._known_speakers.items():
                score = float(np.dot(embedding, ref_emb))
                if score > best_score:
                    best_match, best_score = name, score

            if best_score > SIMILARITY_THRESHOLD:
                # Matched an existing speaker — safe even for short clips. Update avg.
                self._known_speakers[best_match] = (
                    (1 - EMBEDDING_UPDATE_RATE) * self._known_speakers[best_match]
                    + EMBEDDING_UPDATE_RATE * embedding
                )
                # Re-normalize after update
                self._known_speakers[best_match] /= np.linalg.norm(self._known_speakers[best_match])
                assigned = best_match
            elif duration >= MIN_AUDIO_DURATION:
                # Only spawn a NEW cluster from a sufficiently long, confident clip.
                assigned = f"Speaker {len(self._known_speakers) + 1}"
                self._known_speakers[assigned] = embedding
                logger.info(f"New speaker detected: {assigned} (best_match_score={best_score:.3f})")
            else:
                # Short clip that matched no existing speaker: don't invent one —
                # leave it to the channel default so we don't over-fragment.
                return None

        with self._history_lock:
            self._history.append((assigned, embedding))
        return assigned

    def reset(self):
        """Reset speaker registry for a new meeting."""
        with self._speakers_lock:
            self._known_speakers = {}
        with self._history_lock:
            self._history = []

    def seed_known_speakers(self, centroids: dict[str, np.ndarray]) -> None:
        """Preload named speaker centroids (from the persistent registry, §7) so
        identify() can label them from their first utterance.

        Keys are real names, not "Speaker N" — such keys flow through the engine
        unchanged (they never trigger the unknown-speaker prompt). Centroids are
        stored unit-normalized; existing keys are left untouched. Call once at
        meeting start, before capture begins.
        """
        if not centroids:
            return
        with self._speakers_lock:
            for name, emb in centroids.items():
                if not name or not str(name).strip():
                    continue
                arr = np.asarray(emb, dtype=np.float32).reshape(-1)
                norm = np.linalg.norm(arr)
                if norm < 1e-8:
                    continue
                self._known_speakers.setdefault(name, arr / norm)

    def get_centroid(self, key: str) -> np.ndarray | None:
        """Return a copy of the unit-norm centroid for `key`, or None if absent.

        Used by confirm_speaker to snapshot a person's voiceprint for the
        persistent registry. Locked so it never reads a half-updated centroid.
        """
        with self._speakers_lock:
            emb = self._known_speakers.get(key)
            return None if emb is None else emb.copy()

    def reconcile_clusters(self) -> dict[str, str]:
        """Re-cluster all per-utterance embeddings collected during the meeting
        and return a {old_cluster_label: merged_cluster_label} mapping.

        Online clustering spawns a new cluster whenever similarity dips below
        SIMILARITY_THRESHOLD, which fragments a single speaker into many
        clusters over the course of a meeting. This pass runs agglomerative
        hierarchical clustering over *all* collected embeddings with a looser
        cosine-distance threshold, then assigns each online cluster to the
        merged group that best represents the majority of its utterances.

        Uses average-linkage cosine clustering via scipy (already a speechbrain
        transitive dep — no new requirement).

        Safe to call multiple times; the mapping is deterministic for a given
        history. Returns an empty dict if fewer than 2 online clusters exist
        (nothing to merge).
        """
        with self._history_lock:
            history = list(self._history)

        if not history:
            return {}
        online_clusters = sorted({label for label, _ in history})
        if len(online_clusters) < 2:
            return {}

        from scipy.cluster.hierarchy import linkage, fcluster
        from scipy.spatial.distance import pdist
        from collections import Counter

        embeddings = np.stack([emb for _, emb in history])
        # All embeddings are unit-norm, so cosine distance = 1 - dot product.
        distances = pdist(embeddings, metric="cosine")
        Z = linkage(distances, method="average")
        merged_ids = fcluster(Z, t=RECLUSTER_DISTANCE, criterion="distance")

        # Distance sweep — log how many merged groups each candidate distance
        # would yield from the *same* linkage tree. Turns RECLUSTER_DISTANCE
        # tuning into measurement: pick the cut whose group count matches the
        # real participant headcount. The active threshold is marked with *.
        sweep = []
        for d in (0.40, 0.45, 0.50, 0.55, 0.60, 0.65):
            n_groups = len(set(fcluster(Z, t=d, criterion="distance")))
            mark = "*" if abs(d - RECLUSTER_DISTANCE) < 1e-9 else ""
            sweep.append(f"{d:.2f}{mark}→{n_groups}")
        logger.info(
            f"Reconcile distance sweep ({len(online_clusters)} online clusters, "
            f"{len(history)} utterances): " + " ".join(sweep)
            + f"  (active d={RECLUSTER_DISTANCE})"
        )

        # For each online cluster, pick the merged group that owns the majority
        # of its utterances. Ties broken by whichever merged group was seen first.
        online_to_merged: dict[str, int] = {}
        by_online: dict[str, list[int]] = {c: [] for c in online_clusters}
        for (online_label, _), merged_id in zip(history, merged_ids):
            by_online[online_label].append(int(merged_id))
        for online_label, merged_ids_list in by_online.items():
            counts = Counter(merged_ids_list)
            online_to_merged[online_label] = counts.most_common(1)[0][0]

        # Build canonical merged-group labels. A named cluster (a real name from
        # a user confirmation or the persistent registry) always wins canonical
        # status over an anonymous "Speaker N" key, so merging never demotes a
        # named speaker back to "Speaker N". Within each tier the lowest label
        # wins, so "Speaker 1" stays "Speaker 1" whenever possible.
        canonical_order = sorted(online_clusters, key=lambda lbl: (_is_speaker_n(lbl), lbl))
        merged_to_canonical: dict[int, str] = {}
        for online_label in canonical_order:
            merged_id = online_to_merged[online_label]
            merged_to_canonical.setdefault(merged_id, online_label)

        mapping = {
            online: merged_to_canonical[merged_id]
            for online, merged_id in online_to_merged.items()
        }
        # Skip trivial mappings (cluster → itself) to reduce noise downstream.
        mapping = {k: v for k, v in mapping.items() if k != v}

        if mapping:
            # Apply merges to the in-memory centroids so future identify() calls
            # (post-stop review usage) see the consolidated state.
            for old, new in mapping.items():
                self._merge_centroid(old, new)
            # Rewrite history labels so a second reconcile is idempotent.
            with self._history_lock:
                self._history = [
                    (mapping.get(label, label), emb) for label, emb in self._history
                ]

        logger.info(
            f"Re-clustered {len(history)} utterances: "
            f"{len(online_clusters)} online clusters → "
            f"{len(set(merged_to_canonical.values()))} merged groups"
        )
        return mapping

    def _merge_centroid(self, old_key: str, new_key: str) -> None:
        """Internal helper: merge two existing centroids without touching history."""
        if old_key == new_key:
            return
        with self._speakers_lock:
            old_emb = self._known_speakers.pop(old_key, None)
            if old_emb is None:
                return
            existing = self._known_speakers.get(new_key)
            if existing is None:
                self._known_speakers[new_key] = old_emb
                return
            merged = (existing + old_emb) / 2.0
            norm = np.linalg.norm(merged)
            if norm > 1e-8:
                merged = merged / norm
            self._known_speakers[new_key] = merged

    def rename_cluster(self, old_key: str, new_key: str) -> bool:
        """Rename a speaker cluster. Merges centroids if new_key already exists.

        Returns True if the rename happened, False if old_key didn't exist.
        Merge: averages the two unit-norm centroids and re-normalizes.
        """
        if old_key == new_key:
            return True
        with self._speakers_lock:
            old_emb = self._known_speakers.pop(old_key, None)
            if old_emb is None:
                return False
            existing = self._known_speakers.get(new_key)
            if existing is None:
                self._known_speakers[new_key] = old_emb
            else:
                merged = (existing + old_emb) / 2.0
                norm = np.linalg.norm(merged)
                if norm > 1e-8:
                    merged = merged / norm
                self._known_speakers[new_key] = merged
        return True
