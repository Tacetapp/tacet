"""Vision fallback: an invalid vision model ID disables screen analysis for the
session — one warning, one user-visible info message, no further frame sends."""

import asyncio
import io

import pytest
from PIL import Image

from web.backend.engine import PROMPTS
from web.backend.llm_service import InvalidModelError
from web.backend.screen_analyzer import ScreenAnalyzer


def _jpeg(color=(10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buf, format="JPEG")
    return buf.getvalue()


class _InvalidVisionLLM:
    """Vision provider whose model ID is invalid — analyze() always raises."""

    supports_vision = True

    def __init__(self):
        self.calls = 0

    async def analyze(self, messages, system_prompt=None, use_cache=False, max_tokens=4096):
        self.calls += 1
        raise InvalidModelError("us.anthropic.claude-sonnet-4-6")


@pytest.mark.asyncio
async def test_invalid_vision_model_disables_and_emits_once():
    events = []
    async def emit(m): events.append(m)

    llm = _InvalidVisionLLM()
    analyzer = ScreenAnalyzer(llm=llm, prompts=PROMPTS, emit=emit, min_analysis_interval=0.0)

    # First analysis hits the invalid model → latch disabled + one info message.
    await analyzer._analyze_frame(_jpeg())
    assert analyzer._disabled is True
    assert llm.calls == 1
    infos = [e for e in events if e.get("type") == "info"]
    assert len(infos) == 1
    assert "screen analysis disabled" in infos[0]["text"].lower()
    assert "Settings" in infos[0]["text"]

    # Further frames short-circuit at the top of on_frame — no more model calls,
    # no repeated info message.
    await analyzer.on_frame(_jpeg((200, 100, 50)))
    await asyncio.sleep(0.02)
    assert llm.calls == 1
    assert len([e for e in events if e.get("type") == "info"]) == 1

    # reset() re-enables screen analysis for a fresh session.
    analyzer.reset()
    assert analyzer._disabled is False
