"""Per-meeting registry of cluster-to-name assignments and candidate names."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass
class ClusterState:
    """Tracks per-cluster prompt/resolution state.

    Lifecycle: a cluster starts fresh; each segment it produces appends its
    (index, text) to `samples` and increments `count`. When count crosses a
    threshold and the cluster is not resolved/skipped/deferred, the engine
    emits a prompt. User action transitions state to resolved/skipped/deferred.
    """

    count: int = 0
    samples: deque = field(default_factory=lambda: deque(maxlen=3))  # [(seg_index, text)]
    prompted: bool = False  # have we emitted a speaker_prompt for this cluster yet?


class NameRegistry:
    """Maps speaker-diarization cluster keys (e.g. "Speaker 3") to real names.

    One instance per meeting. Tracks:
    - Confirmed name assignments (cluster → real name).
    - Skipped clusters (user said "leave as Speaker N").
    - Deferred clusters (user said "ask me later", batch at meeting end).
    - Candidate names sourced from known attendees and participant-panel OCR.
    """

    def __init__(self):
        self._assignments: dict[str, str] = {}  # cluster → real name
        self._skipped: set[str] = set()
        self._deferred: set[str] = set()
        self._known_names: list[str] = []
        self._ocr_names: list[str] = []
        self._clusters: dict[str, ClusterState] = {}
        self._skip_all: bool = False  # user toggled "stop prompting"

    # ── Seeding candidate names ────────────────────────

    def add_known_names(self, names: list[str]) -> None:
        for n in names:
            n = n.strip()
            if n and n not in self._known_names:
                self._known_names.append(n)

    def add_ocr_names(self, names: list[str]) -> None:
        for n in names:
            n = n.strip()
            if n and n not in self._ocr_names:
                self._ocr_names.append(n)

    def candidates(self) -> list[str]:
        """Names not yet assigned to any cluster. Calendar first, then OCR."""
        used = set(self._assignments.values())
        out = [n for n in self._known_names if n not in used]
        out.extend(n for n in self._ocr_names if n not in used and n not in out)
        return out

    def assigned_names(self) -> list[str]:
        """Distinct real names the user has confirmed for clusters, in first-seen
        order. Used to bias transcription toward names already in play."""
        out: list[str] = []
        for name in self._assignments.values():
            if name not in out:
                out.append(name)
        return out

    # ── Per-cluster state ──────────────────────────────

    def track_utterance(self, cluster: str, seg_index: int, text: str) -> int:
        """Record that `cluster` spoke segment `seg_index` with `text`.

        Returns the new utterance count for the cluster.
        """
        state = self._clusters.setdefault(cluster, ClusterState())
        state.count += 1
        state.samples.append((seg_index, text))
        return state.count

    def samples_for(self, cluster: str) -> list[dict]:
        """Most-recent samples for a cluster, newest-first."""
        state = self._clusters.get(cluster)
        if not state:
            return []
        return [{"index": idx, "text": text} for idx, text in reversed(state.samples)]

    def count_for(self, cluster: str) -> int:
        state = self._clusters.get(cluster)
        return state.count if state else 0

    def mark_prompted(self, cluster: str) -> None:
        state = self._clusters.setdefault(cluster, ClusterState())
        state.prompted = True

    def was_prompted(self, cluster: str) -> bool:
        state = self._clusters.get(cluster)
        return bool(state and state.prompted)

    # ── Resolution ─────────────────────────────────────

    def assign(self, cluster: str, name: str) -> None:
        """Record that `cluster` is really `name`. Name may collide with an
        existing assignment (merge case) — caller handles cluster merging.
        """
        self._assignments[cluster] = name
        # Once assigned, clear skip/defer flags so state is coherent.
        self._skipped.discard(cluster)
        self._deferred.discard(cluster)

    def skip(self, cluster: str) -> None:
        """User chose to leave this cluster labeled as-is for the session."""
        self._skipped.add(cluster)
        self._deferred.discard(cluster)

    def defer(self, cluster: str) -> None:
        """User chose to resolve this cluster later (batch at meeting end)."""
        self._deferred.add(cluster)

    def set_skip_all(self, on: bool) -> None:
        self._skip_all = on

    @property
    def skip_all(self) -> bool:
        return self._skip_all

    def resolve(self, cluster: str) -> str:
        """Return the real name if assigned, else the cluster key itself."""
        return self._assignments.get(cluster, cluster)

    def is_assigned(self, cluster: str) -> bool:
        return cluster in self._assignments

    def is_resolved(self, cluster: str) -> bool:
        """Resolved = assigned OR skipped (i.e. we should NOT prompt again)."""
        return cluster in self._assignments or cluster in self._skipped

    def is_deferred(self, cluster: str) -> bool:
        return cluster in self._deferred

    def should_prompt(self, cluster: str, threshold: int) -> bool:
        """True if we should emit a live speaker_prompt for this cluster right now."""
        if self._skip_all or self.is_resolved(cluster) or self.is_deferred(cluster):
            return False
        if self.was_prompted(cluster):
            return False
        return self.count_for(cluster) >= threshold

    def clusters_for_batch(self) -> list[str]:
        """Clusters to show in the end-of-meeting / review batch view.

        Includes deferred clusters, and any unresolved cluster that has ever
        been prompted or has ≥1 utterance but was auto-deferred via skip_all.
        Excludes resolved (assigned + skipped) clusters.
        """
        out: set[str] = set(self._deferred)
        for cluster, state in self._clusters.items():
            if self.is_resolved(cluster):
                continue
            if state.count >= 1:
                out.add(cluster)
        return sorted(out)

    # ── Reverse lookup for segment rewrite ─────────────

    def original_cluster(self, final_name: str) -> str | None:
        """Find the cluster key that resolves to this name (reverse lookup).
        Used when rewriting segments after an assignment — a segment's stored
        `speaker` may already be the real name after an earlier assignment, or
        still the cluster key. Returns the cluster key if the final_name was
        produced by an assignment, else None.
        """
        for cluster, name in self._assignments.items():
            if name == final_name:
                return cluster
        return None

    def merge_cluster_state(self, src: str, dst: str) -> None:
        """Fold cluster `src`'s utterance tally + samples into `dst`.

        Used by end-of-meeting re-clustering when two online clusters are
        merged. Does not touch assignments — caller owns that policy.
        """
        if src == dst:
            return
        src_state = self._clusters.pop(src, None)
        if src_state is None:
            return
        dst_state = self._clusters.setdefault(dst, ClusterState())
        dst_state.count += src_state.count
        # Preserve newest samples across both clusters, bounded by maxlen.
        combined = list(dst_state.samples) + list(src_state.samples)
        dst_state.samples.clear()
        for item in combined[-dst_state.samples.maxlen:]:
            dst_state.samples.append(item)
        dst_state.prompted = dst_state.prompted or src_state.prompted
        if src in self._skipped:
            self._skipped.discard(src)
            self._skipped.add(dst)
        if src in self._deferred:
            self._deferred.discard(src)
            self._deferred.add(dst)

    def reset(self) -> None:
        self._assignments.clear()
        self._skipped.clear()
        self._deferred.clear()
        self._known_names.clear()
        self._ocr_names.clear()
        self._clusters.clear()
        self._skip_all = False
