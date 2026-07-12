"""Tests for the ECAPA model cache dir (writable-dir packaging support)."""

import os

from web.backend import speaker_embedder as se


def test_ecapa_savedir_default(monkeypatch):
    monkeypatch.delenv("MODELS_DIR", raising=False)
    assert se.ecapa_savedir() == os.path.join("models", "speechbrain-ecapa")


def test_ecapa_savedir_from_env(monkeypatch):
    monkeypatch.setenv("MODELS_DIR", "/tmp/ma-models")
    assert se.ecapa_savedir() == os.path.join("/tmp/ma-models", "speechbrain-ecapa")
