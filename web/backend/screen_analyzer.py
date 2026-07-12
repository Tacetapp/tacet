"""Screen change detection and visual analysis via Claude multimodal."""

from __future__ import annotations

import asyncio
import io
import logging
import time
from typing import Callable, Awaitable

import numpy as np
from PIL import Image

from web.backend.llm_service import LLMService, InvalidModelError
from web.backend.prompt_loader import PromptLoader

logger = logging.getLogger(__name__)

# Downscale resolution for SSIM comparison (saves CPU)
COMPARE_WIDTH = 960
COMPARE_HEIGHT = 540

# Vision API frames: cap the long edge and re-encode before sending. Raw
# ScreenCaptureKit frames can be multi-MB 5K Retina images; image tokens scale
# with resolution, so this is a large cut on the vision line (§3.5).
MAX_VISION_EDGE = 1568
VISION_JPEG_QUALITY = 80

NO_VISUAL = "[NO_VISUAL]"


class ScreenAnalyzer:
    """Detects significant visual changes and sends frames to Claude for analysis.

    Two-stage change detection:
    1. Fast pre-filter: mean absolute pixel difference (numpy, ~1ms)
    2. SSIM comparison for structural similarity

    Rate-limited to avoid excessive Bedrock calls.
    """

    def __init__(
        self,
        llm: LLMService,
        prompts: PromptLoader,
        emit: Callable[[dict], Awaitable[None]],
        ssim_threshold: float = 0.85,
        pixel_diff_threshold: float = 5.0,
        min_analysis_interval: float = 30.0,
        is_paused: Callable[[], bool] | None = None,
    ):
        self._llm = llm
        self._prompts = prompts
        self._emit = emit
        self._ssim_threshold = ssim_threshold
        self._pixel_diff_threshold = pixel_diff_threshold
        self._min_interval = min_analysis_interval
        # Optional callback (wired to engine.paused) — skip analysis while paused.
        self._is_paused = is_paused
        self._prev_frame: np.ndarray | None = None
        self._last_analysis_time: float = 0
        self._visual_context: str = ""
        self._next_insight_id_offset = 10000  # offset to avoid collisions with text insight IDs
        self._insight_count = 0
        self._lock = asyncio.Lock()
        # Set once the vision model ID is rejected (InvalidModelError). Latched for
        # the session so we stop sending frames to a model that can't read them;
        # checked at the top of on_frame. Cleared by reset() for a new session.
        self._disabled = False

    @property
    def visual_context(self) -> str:
        """Current visual context string for inclusion in system prompts."""
        return self._visual_context

    async def on_frame(self, jpeg_bytes: bytes):
        """Called by AudioCaptureSource when a new screen frame arrives."""
        try:
            # The vision model ID was rejected earlier this session — stay off.
            if self._disabled:
                return

            # Skip entirely while the meeting is paused (§3.5).
            if self._is_paused is not None and self._is_paused():
                return

            frame = self._decode_and_resize(jpeg_bytes)
            if frame is None:
                return

            # Check for significant change
            if self._prev_frame is not None:
                if not self._has_significant_change(frame):
                    return

            self._prev_frame = frame

            # Rate limit
            now = time.monotonic()
            if now - self._last_analysis_time < self._min_interval:
                return

            self._last_analysis_time = now
            # Analyze in background to not block frame processing
            asyncio.create_task(self._analyze_frame(jpeg_bytes))

        except Exception as e:
            logger.warning(f"Frame processing error: {e}")

    def _decode_and_resize(self, jpeg_bytes: bytes) -> np.ndarray | None:
        """Decode JPEG and resize for comparison."""
        try:
            img = Image.open(io.BytesIO(jpeg_bytes))
            img = img.resize((COMPARE_WIDTH, COMPARE_HEIGHT), Image.LANCZOS)
            return np.array(img)
        except Exception as e:
            logger.warning(f"Failed to decode frame: {e}")
            return None

    def _has_significant_change(self, frame: np.ndarray) -> bool:
        """Two-stage change detection: fast pixel diff, then SSIM."""
        prev = self._prev_frame
        if prev is None or prev.shape != frame.shape:
            return True

        # Stage 1: Fast mean pixel difference (~1ms)
        diff = np.mean(np.abs(frame.astype(np.float32) - prev.astype(np.float32)))
        if diff < self._pixel_diff_threshold:
            return False

        # Stage 2: SSIM for structural comparison
        try:
            from skimage.metrics import structural_similarity
            # Convert to grayscale for SSIM (faster)
            gray_prev = np.mean(prev, axis=2).astype(np.uint8)
            gray_curr = np.mean(frame, axis=2).astype(np.uint8)
            score = structural_similarity(gray_prev, gray_curr)
            return score < self._ssim_threshold
        except ImportError:
            # If scikit-image not available, fall back to pixel diff only
            logger.warning("scikit-image not installed, using pixel diff only for change detection")
            return diff > self._pixel_diff_threshold * 3

    def _downscale_for_vision(self, jpeg_bytes: bytes) -> bytes:
        """Shrink a frame to <= MAX_VISION_EDGE on the long edge and re-encode as
        JPEG before sending to the vision model (§3.5). Returns the original bytes
        if the frame is already small enough or if decoding fails."""
        try:
            img = Image.open(io.BytesIO(jpeg_bytes))
            img.load()
            long_edge = max(img.size)
            if long_edge <= MAX_VISION_EDGE:
                return jpeg_bytes
            scale = MAX_VISION_EDGE / long_edge
            new_size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
            img = img.convert("RGB").resize(new_size, Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=VISION_JPEG_QUALITY)
            return buf.getvalue()
        except Exception as e:
            logger.warning(f"Vision downscale failed, sending original: {e}")
            return jpeg_bytes

    async def _analyze_frame(self, jpeg_bytes: bytes):
        """Send frame to Bedrock multimodal for visual analysis."""
        async with self._lock:
            try:
                frame_bytes = self._downscale_for_vision(jpeg_bytes)
                prompt = self._prompts.render("vision")
                messages = [{
                    "role": "user",
                    "content": [
                        {"image": {"format": "jpeg", "source": {"bytes": frame_bytes}}},
                        {"text": prompt},
                    ]
                }]

                result = await self._llm.analyze(messages)

                if NO_VISUAL in result:
                    return

                # Update visual context for system prompt
                self._visual_context = result.strip()

                # Emit as visual insight
                self._insight_count += 1
                insight_id = self._next_insight_id_offset + self._insight_count
                insight = {
                    "id": insight_id,
                    "category": "visual",
                    "text": result.strip(),
                }
                await self._emit({"type": "insight", **insight})

                logger.info(f"Visual insight generated: {result[:80]}...")

            except InvalidModelError:
                # Bad/unavailable vision model ID → disable screen analysis for the
                # rest of the session (one warning, one user-visible message).
                if not self._disabled:
                    self._disabled = True
                    logger.warning(
                        "Vision model ID invalid — disabling screen analysis for this "
                        "session (fix in Settings → AI Provider)."
                    )
                    await self._emit({
                        "type": "info",
                        "text": "Vision model ID invalid — screen analysis disabled "
                                "(fix in Settings → AI Provider)",
                    })
            except Exception as e:
                logger.warning(f"Screen analysis failed: {e}")

    def reset(self):
        """Reset state for a new session."""
        self._prev_frame = None
        self._last_analysis_time = 0
        self._visual_context = ""
        self._insight_count = 0
        self._disabled = False
