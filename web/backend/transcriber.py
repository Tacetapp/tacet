"""Real-time speech-to-text using mlx-whisper with Silero VAD."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Callable, Awaitable

import numpy as np

from web.backend.srt_parser import SrtSegment
from web.backend.whisper_backend import create_whisper_backend

logger = logging.getLogger(__name__)

# When LOG_CONTENT=false, log segment counts/lengths instead of transcript text (§4.4).
_LOG_CONTENT = os.environ.get("LOG_CONTENT", "").strip().lower() not in ("0", "false", "no", "off")


def _f(key: str, default: float) -> float:
    """Read a float env var, falling back to the default on missing/blank/bad."""
    try:
        return float(os.environ.get(key, "").strip() or default)
    except (TypeError, ValueError):
        return default


def _b(key: str, default: bool) -> bool:
    raw = os.environ.get(key, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# VAD configuration. All env-overridable; every key has a matching field in
# electron/config.js so the Settings UI can expose it. Defaults preserve prior
# hardcoded behaviour. Read at import (the desktop app restarts the server on a
# settings change, so import-time reads are sufficient).
VAD_THRESHOLD = _f("VAD_THRESHOLD", 0.5)               # Speech probability threshold
VAD_MIN_SILENCE_MS = _f("VAD_MIN_SILENCE_MS", 800)     # Silence to end a speech segment (ms)
VAD_MIN_SPEECH_MS = _f("VAD_MIN_SPEECH_MS", 500)       # Minimum speech duration to keep (ms)
VAD_MAX_SPEECH_S = _f("VAD_MAX_SPEECH_S", 20.0)        # Hard cap before forced transcription (s)
VAD_FRAME_SAMPLES = 512        # 32ms at 16kHz — Silero VAD frame size (fixed by the model)

# Progressive finalization: emit a long monologue in chunks instead of only at
# end-of-turn, so text appears while someone is still talking. When an active
# turn's speech exceeds VAD_SOFT_CUT_S, the next sub-pause of at least
# VAD_SOFT_CUT_SILENCE_MS (shorter than the full end-of-turn silence) cuts a
# finalized chunk and keeps the turn open. Append-only — no interim/replace, so
# no frontend change and no redundant re-decode competing for the shared GPU lock.
VAD_PROGRESSIVE_FINALIZE = _b("VAD_PROGRESSIVE_FINALIZE", True)
VAD_SOFT_CUT_S = _f("VAD_SOFT_CUT_S", 8.0)             # turn length before soft cuts kick in (s)
VAD_SOFT_CUT_SILENCE_MS = _f("VAD_SOFT_CUT_SILENCE_MS", 200)  # min mid-turn pause to cut at (ms)

# Audio
SAMPLE_RATE = 16000
SILENCE_RMS_THRESHOLD = _f("SILENCE_RMS_THRESHOLD", 0.003)  # Skip transcription if ~silent

# Mic-bleed RMS guard — drop mic audio when the system channel was loudly
# active during the same window and the mic is much quieter. This kills
# whisper hallucinations on near-silent mic that's really just speaker bleed.
BLEED_GUARD_WINDOW_S = _f("BLEED_GUARD_WINDOW_S", 3.0)      # look-back for concurrent system audio
BLEED_GUARD_RMS_RATIO = _f("BLEED_GUARD_RMS_RATIO", 0.5)    # bleed if mic_rms < system_rms * this
BLEED_GUARD_MIN_SYSTEM_RMS = _f("BLEED_GUARD_MIN_SYSTEM_RMS", 0.02)  # ignore quieter system audio

# Option E: VAD-overlap guard — handles the LOUD-bleed case where mic picks up
# the speaker playback at ~full volume (so RMS guard can't help). When mic and
# system VAD windows overlap in time AND the system's VAD peak probability is
# substantially higher than the mic's, treat mic as bleed. This is robust to
# volume: it exploits the fact that bleed is a reverberated copy of a primary
# signal, so the primary-source VAD fires with more confidence.
VAD_BLEED_MIN_OVERLAP_RATIO = _f("VAD_BLEED_MIN_OVERLAP_RATIO", 0.5)   # mic must overlap ≥this with system turn
VAD_BLEED_MIC_PROB_CEILING = _f("VAD_BLEED_MIC_PROB_CEILING", 0.80)    # mic peak prob below this (uncertain mic)
VAD_BLEED_SYSTEM_PROB_FLOOR = _f("VAD_BLEED_SYSTEM_PROB_FLOOR", 0.90)  # system peak prob ≥this (confident system)


class ChannelActivityTracker:
    """Tiny shared blackboard so the mic transcriber can see if the system
    channel was recently active, how loud it was, and how confidently VAD
    fired on it.

    Records:
    - An RMS ring (for the RMS bleed guard): (timestamp, peak_rms)
    - A VAD-turn ring (for the Option E overlap guard):
      (start_ts, end_ts, peak_prob) for every system VAD turn.

    One tracker is created by the system transcriber's creator and handed
    to both transcribers.
    """

    def __init__(self, history_s: float = 10.0):
        self._history_s = history_s
        # Each RMS entry: (wall_clock_time, peak_rms) — small list, truncated in-place.
        self._entries: list[tuple[float, float]] = []
        # Each VAD turn: (start_ts, end_ts, peak_prob) in wall-clock monotonic time.
        self._vad_turns: list[tuple[float, float, float]] = []

    def record(self, rms: float) -> None:
        now = time.monotonic()
        self._entries.append((now, rms))
        cutoff = now - self._history_s
        # Trim oldest entries; list stays tiny so in-place filter is fine.
        self._entries = [e for e in self._entries if e[0] >= cutoff]

    def peak_rms_within(self, window_s: float) -> float:
        """Return the loudest RMS seen in the last `window_s` seconds, or 0 if empty."""
        if not self._entries:
            return 0.0
        cutoff = time.monotonic() - window_s
        recent = [rms for ts, rms in self._entries if ts >= cutoff]
        return max(recent) if recent else 0.0

    def record_vad_turn(self, start_ts: float, end_ts: float, peak_prob: float) -> None:
        """Record one system VAD turn — its time span and peak speech probability."""
        self._vad_turns.append((start_ts, end_ts, peak_prob))
        cutoff = time.monotonic() - self._history_s
        self._vad_turns = [t for t in self._vad_turns if t[1] >= cutoff]

    def overlapping_vad_turn(
        self, mic_start: float, mic_end: float, min_overlap_ratio: float
    ) -> tuple[float, float, float] | None:
        """Find a recorded system VAD turn that overlaps ≥ min_overlap_ratio of
        the mic window. Returns (start, end, peak_prob) or None.
        """
        if mic_end <= mic_start:
            return None
        mic_len = mic_end - mic_start
        for start_ts, end_ts, peak_prob in self._vad_turns:
            overlap = max(0.0, min(mic_end, end_ts) - max(mic_start, start_ts))
            if overlap / mic_len >= min_overlap_ratio:
                return (start_ts, end_ts, peak_prob)
        return None

# Hallucination patterns — Whisper generates these on silence/noise
_HALLUCINATION_PATTERNS = re.compile(
    r"^("
    r"(thank you\.?\s*)+"
    r"|thanks for watching\.?"
    r"|please subscribe\.?"
    r"|like and subscribe\.?"
    r"|(bye\.?\s*)+"
    r"|you$"
    r"|\[.*\]"           # [Music], [Applause], etc.
    r"|\(.*\)"           # (Music), (Applause), etc.
    r"|\.+$"             # Just dots
    r")$",
    re.IGNORECASE,
)

# Repetition detection — catches whisper decoder loops
# Matches a phrase of 10+ chars repeated 3+ times in a row
_REPETITION_RE = re.compile(r"(.{10,}?)\1{2,}", re.IGNORECASE | re.DOTALL)


def _format_timestamp(seconds: float) -> str:
    """Convert seconds to SRT timestamp format HH:MM:SS,mmm."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _is_hallucination(text: str) -> bool:
    """Check if text matches known Whisper hallucination patterns."""
    return bool(_HALLUCINATION_PATTERNS.match(text.strip()))


def _has_repetition_loop(text: str) -> bool:
    """Detect whisper decoder repetition loops (same phrase 3+ times)."""
    return bool(_REPETITION_RE.search(text))


class WhisperTranscriber:
    """Real-time transcription using a Whisper backend with Silero VAD.

    Uses Voice Activity Detection to find speech boundaries, then transcribes
    only speech segments with the chip-appropriate backend (mlx-whisper on
    Apple Silicon, faster-whisper on Intel — see whisper_backend.py).
    """

    def __init__(
        self,
        on_segments: Callable[[list[SrtSegment]], Awaitable[None]],
        model: str | None = None,
        channel: str = "",
        on_speech_start: Callable[[], None] | None = None,
        transcription_lock: asyncio.Lock | None = None,
        speaker_embedder=None,
        activity_tracker: ChannelActivityTracker | None = None,
        is_mic: bool = False,
        voice_profile=None,
        prompt_provider: Callable[[], str | None] | None = None,
    ):
        self.on_segments = on_segments
        self._backend = create_whisper_backend(model)
        self._channel = channel
        self._on_speech_start = on_speech_start
        # Returns the current Whisper initial_prompt (vocabulary biasing), or
        # None. Consulted per transcription so dynamic context (confirmed speaker
        # names, attendees) is picked up without recreating the transcriber.
        self._prompt_provider = prompt_provider
        self._segment_index = 0
        self._active = True
        self._speaker_embedder = speaker_embedder
        # Bleed guard: system transcriber writes its RMS here; mic transcriber reads.
        self._activity_tracker = activity_tracker
        self._is_mic = is_mic
        # Optional deterministic bleed guard — compares mic buffers against the
        # user's enrolled voice fingerprint. Only consulted on the mic channel.
        self._voice_profile = voice_profile

        # VAD state
        self._vad_model = None
        self._is_speaking = False
        self._speech_buffer: list[np.ndarray] = []
        self._speech_start_time = 0.0
        self._silence_duration = 0.0
        self._speech_duration = 0.0
        # Per-turn VAD peak probability + wall-clock span, for Option E overlap guard.
        self._turn_peak_prob = 0.0
        self._turn_wall_start = 0.0
        self._turn_wall_end = 0.0

        # VAD frame buffer — accumulate until we have 512 samples
        self._vad_frame_buf = np.zeros(0, dtype=np.float32)

        # Time tracking — count total VAD frames processed
        self._samples_processed = 0

        # Serialise transcription calls (shared lock prevents concurrent Metal GPU access)
        self._lock = transcription_lock or asyncio.Lock()

        # In-flight transcription tasks. Tracked so flush() can drain the queue
        # on stop — otherwise buffers fired between speech turns get abandoned
        # when _active flips to False.
        self._pending_tasks: set[asyncio.Task] = set()

    @property
    def speech_start_time(self) -> float:
        """Current speech segment start time in seconds."""
        return self._speech_start_time

    def _ensure_vad(self):
        """Lazy-load the Silero VAD model."""
        if self._vad_model is not None:
            return

        from silero_vad import load_silero_vad
        self._vad_model = load_silero_vad(onnx=True)
        logger.info("Loaded Silero VAD model (ONNX)")

    async def feed_audio(self, pcm_data: bytes):
        """Feed raw PCM float32 audio data from the capture source."""
        if not self._active:
            return

        samples = np.frombuffer(pcm_data, dtype=np.float32)
        if len(samples) == 0:
            return

        self._ensure_vad()

        # Append to VAD frame buffer
        self._vad_frame_buf = np.concatenate([self._vad_frame_buf, samples])

        # Process complete VAD frames (512 samples = 32ms each)
        while len(self._vad_frame_buf) >= VAD_FRAME_SAMPLES:
            frame = self._vad_frame_buf[:VAD_FRAME_SAMPLES]
            self._vad_frame_buf = self._vad_frame_buf[VAD_FRAME_SAMPLES:]

            await self._process_vad_frame(frame)
            self._samples_processed += VAD_FRAME_SAMPLES

    async def _process_vad_frame(self, frame: np.ndarray):
        """Process a single VAD frame (512 samples / 32ms)."""
        if not self._active:
            return

        import torch

        frame_duration = VAD_FRAME_SAMPLES / SAMPLE_RATE  # 0.032s

        # Get speech probability from Silero VAD
        audio_tensor = torch.from_numpy(frame)
        speech_prob = float(self._vad_model(audio_tensor, SAMPLE_RATE))

        if speech_prob >= VAD_THRESHOLD:
            # Speech detected
            if not self._is_speaking:
                self._is_speaking = True
                self._speech_start_time = self._samples_processed / SAMPLE_RATE
                self._speech_buffer = []
                self._speech_duration = 0.0
                self._silence_duration = 0.0
                self._turn_peak_prob = speech_prob
                self._turn_wall_start = time.monotonic()
                logger.info(f"[{self._channel or 'default'}] Speech started at "
                            f"{self._speech_start_time:.1f}s (prob={speech_prob:.2f})")
                if self._on_speech_start:
                    self._on_speech_start()

            self._silence_duration = 0.0
            self._speech_duration += frame_duration
            self._speech_buffer.append(frame.copy())
            if speech_prob > self._turn_peak_prob:
                self._turn_peak_prob = speech_prob

            # Force transcription if speech exceeds max duration
            if self._speech_duration >= VAD_MAX_SPEECH_S:
                logger.info(f"Speech hit {VAD_MAX_SPEECH_S}s cap, forcing transcription")
                self._fire_transcription()
        else:
            # Silence / non-speech
            if self._is_speaking:
                self._silence_duration += frame_duration
                self._speech_buffer.append(frame.copy())  # keep tail padding

                # Progressive finalization: once a turn is long enough, cut a
                # finalized chunk at the first short mid-turn pause and keep the
                # turn open. speech_duration resets to 0 on the cut, so this
                # won't re-fire until another VAD_SOFT_CUT_S of speech builds up.
                if (VAD_PROGRESSIVE_FINALIZE
                        and self._speech_duration >= VAD_SOFT_CUT_S
                        and VAD_SOFT_CUT_SILENCE_MS / 1000.0 <= self._silence_duration
                        < VAD_MIN_SILENCE_MS / 1000.0):
                    logger.info(
                        f"Progressive cut at {self._speech_duration:.1f}s "
                        f"(pause={self._silence_duration * 1000:.0f}ms)")
                    self._fire_transcription(continue_turn=True)
                    return

                if self._silence_duration >= VAD_MIN_SILENCE_MS / 1000.0:
                    if self._speech_duration >= VAD_MIN_SPEECH_MS / 1000.0:
                        logger.info(
                            f"Speech ended after {self._speech_duration:.1f}s "
                            f"(silence={self._silence_duration:.1f}s)")
                        self._fire_transcription()
                    else:
                        logger.debug(f"Discarding short speech "
                                     f"({self._speech_duration:.3f}s)")
                        self._is_speaking = False
                        self._speech_buffer = []

    def _fire_transcription(self, continue_turn: bool = False):
        """Capture speech buffer and launch transcription as a background task.

        With continue_turn=True (progressive finalization mid-monologue) the turn
        is kept open: buffers/counters reset but a fresh chunk starts at the
        current position so the next slice of speech continues seamlessly.
        """
        if not self._speech_buffer:
            if not continue_turn:
                self._is_speaking = False
            return

        audio = np.concatenate(self._speech_buffer)
        start_time = self._speech_start_time
        peak_prob = self._turn_peak_prob
        wall_start = self._turn_wall_start
        wall_end = time.monotonic()

        # If this is the system channel, publish the VAD turn so the mic side
        # can check for overlap before running whisper on likely bleed.
        if not self._is_mic and self._activity_tracker is not None and wall_start > 0:
            self._activity_tracker.record_vad_turn(wall_start, wall_end, peak_prob)

        # Reset buffer/counters so the next (sub)segment accumulates cleanly.
        self._speech_buffer = []
        self._speech_duration = 0.0
        self._silence_duration = 0.0
        self._turn_peak_prob = 0.0
        if continue_turn:
            # Keep the turn open; the next chunk starts at the current position.
            self._is_speaking = True
            self._speech_start_time = self._samples_processed / SAMPLE_RATE
            self._turn_wall_start = time.monotonic()
        else:
            self._is_speaking = False
            self._turn_wall_start = 0.0

        task = asyncio.create_task(
            self._transcribe_audio(audio, start_time, peak_prob, wall_start, wall_end))
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    async def _transcribe_audio(self, audio: np.ndarray, start_time: float,
                                 peak_prob: float = 0.0,
                                 wall_start: float = 0.0,
                                 wall_end: float = 0.0):
        """Transcribe a speech segment with mlx-whisper.

        If a speaker_embedder is configured, runs embedding extraction on CPU
        in parallel with whisper on Metal GPU for zero added latency.
        """
        if not self._active:
            return

        duration = len(audio) / SAMPLE_RATE

        # Quick RMS check
        rms = float(np.sqrt(np.mean(audio ** 2)))
        if rms < SILENCE_RMS_THRESHOLD:
            logger.debug(f"Speech buffer too quiet (RMS={rms:.6f}), skipping")
            return

        # Bleed guard #1 (Option 2 — RMS ratio): if this is the mic channel and
        # the system channel was loudly active in the same window AND this mic
        # buffer is substantially quieter, it's almost certainly speaker bleed
        # picking up the other side of the call. Drop before whisper to avoid
        # hallucinations on near-silent mic audio.
        if self._is_mic and self._activity_tracker is not None:
            system_peak = self._activity_tracker.peak_rms_within(BLEED_GUARD_WINDOW_S)
            if (system_peak >= BLEED_GUARD_MIN_SYSTEM_RMS
                    and rms < system_peak * BLEED_GUARD_RMS_RATIO):
                logger.info(
                    f"Bleed guard (RMS): dropping mic buffer "
                    f"(mic_rms={rms:.4f}, system_rms={system_peak:.4f}, "
                    f"ratio={rms/system_peak:.2f} < {BLEED_GUARD_RMS_RATIO})"
                )
                return

        # Bleed guard #2 (Option E — VAD overlap): catches the LOUD-bleed case
        # where the mic picks up speaker playback at roughly full volume (so the
        # RMS ratio guard can't help). If a system VAD turn overlaps the mic
        # window and fired with much higher confidence, this mic buffer is
        # almost certainly a reverberated copy of the remote speaker.
        if (self._is_mic and self._activity_tracker is not None
                and wall_start > 0 and wall_end > wall_start):
            overlap = self._activity_tracker.overlapping_vad_turn(
                wall_start, wall_end, VAD_BLEED_MIN_OVERLAP_RATIO
            )
            if overlap is not None:
                sys_start, sys_end, sys_peak_prob = overlap
                if (peak_prob <= VAD_BLEED_MIC_PROB_CEILING
                        and sys_peak_prob >= VAD_BLEED_SYSTEM_PROB_FLOOR):
                    logger.info(
                        f"Bleed guard (VAD overlap): dropping mic buffer "
                        f"(mic_prob={peak_prob:.2f} <= {VAD_BLEED_MIC_PROB_CEILING}, "
                        f"sys_prob={sys_peak_prob:.2f} >= {VAD_BLEED_SYSTEM_PROB_FLOOR})"
                    )
                    return

        # Bleed guard #3 (voice fingerprint): if the user has enrolled a voice
        # profile, require the mic buffer to match it. Any mic turn that isn't
        # the enrolled user is assumed to be bleed or in-room cross-talk.
        # Only runs when the user has explicitly opted in.
        if self._is_mic and self._voice_profile is not None and self._voice_profile.is_active():
            try:
                from web.backend.voice_profile import VOICE_FINGERPRINT_THRESHOLD
                score = await asyncio.to_thread(self._voice_profile.match_score, audio)
            except Exception as e:
                logger.warning(f"Voice fingerprint match failed (continuing): {e}")
                score = None
            if score is not None and score < VOICE_FINGERPRINT_THRESHOLD:
                logger.info(
                    f"Bleed guard (voice fingerprint): dropping mic buffer "
                    f"(score={score:.3f} < {VOICE_FINGERPRINT_THRESHOLD})"
                )
                return

        # Record system-channel activity so the mic transcriber can see it.
        if not self._is_mic and self._activity_tracker is not None:
            self._activity_tracker.record(rms)

        logger.info(f"Transcribing {duration:.1f}s of speech (RMS={rms:.4f})")

        # Start speaker embedding extraction immediately (CPU, no lock needed)
        speaker_task = None
        if self._speaker_embedder:
            speaker_task = asyncio.create_task(
                asyncio.to_thread(self._speaker_embedder.identify, audio)
            )

        # Run whisper transcription (Metal GPU, needs lock)
        async with self._lock:
            if not self._active:
                if speaker_task:
                    speaker_task.cancel()
                return

            segments = await asyncio.to_thread(
                self._run_whisper, audio, start_time, duration
            )

        if not self._active:
            if speaker_task:
                speaker_task.cancel()
            return

        # Get speaker result — with timeout so model loading never blocks delivery
        speaker = None
        if speaker_task:
            try:
                speaker = await asyncio.wait_for(speaker_task, timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("Speaker embedding timed out (model still loading?), using channel default")
            except Exception as e:
                logger.warning(f"Speaker embedding failed: {e}")

        # Apply speaker ID to segments (overrides channel default)
        if speaker and segments:
            segments = [
                SrtSegment(s.index, s.start, s.end, s.text, speaker=speaker)
                for s in segments
            ]

        if segments:
            if _LOG_CONTENT:
                logger.info(f"Whisper produced {len(segments)} segment(s) "
                            f"(speaker={speaker or self._channel}): "
                            f"{segments[0].text[:60]}...")
            else:
                total_chars = sum(len(s.text) for s in segments)
                logger.info(f"Whisper produced {len(segments)} segment(s) "
                            f"(speaker={speaker or self._channel}): "
                            f"{total_chars} chars total")
            await self.on_segments(segments)
        else:
            logger.info("Whisper produced no segments")

    def _run_whisper(self, audio: np.ndarray, start_time: float,
                     duration: float) -> list[SrtSegment]:
        """Run whisper transcription synchronously (called in thread)."""
        try:
            # Vocabulary biasing: a static, curated prompt (names/jargon) — never
            # Whisper's own output — so there's no repetition-loop risk.
            initial_prompt = None
            if self._prompt_provider is not None:
                try:
                    initial_prompt = self._prompt_provider()
                except Exception as e:
                    logger.debug(f"prompt_provider failed (continuing without): {e}")
            raw_segments = self._backend.transcribe(audio, initial_prompt=initial_prompt)

            segments = []

            for seg in raw_segments:
                text = seg["text"].strip()
                if not text:
                    continue

                # Filter hallucinations
                if _is_hallucination(text):
                    logger.debug(f"Filtered hallucination: {text!r}")
                    continue

                # Detect repetition loops
                if _has_repetition_loop(text):
                    logger.warning(f"Filtered repetition loop: {text[:80]}...")
                    continue

                self._segment_index += 1

                seg_start = start_time + seg["start"]
                seg_end = start_time + seg["end"]

                segments.append(SrtSegment(
                    index=self._segment_index,
                    start=_format_timestamp(seg_start),
                    end=_format_timestamp(seg_end),
                    text=text,
                    speaker=self._channel or None,
                ))

            return segments
        except Exception as e:
            logger.error(f"Whisper transcription failed: {e}", exc_info=True)
            return []

    def stop(self):
        """Stop accepting and processing audio."""
        self._active = False

    async def flush(self):
        """Transcribe any remaining audio and drain all queued transcriptions.

        Called on stop, before stop() flips _active off. Must run while _active
        is still True so the queued _transcribe_audio tasks don't short-circuit.
        """
        # Fire whatever is still buffering as a final segment.
        if self._is_speaking and self._speech_duration > 0.5:
            logger.info(f"Flushing {self._speech_duration:.1f}s of remaining speech")
            self._fire_transcription()
        # Drain every in-flight/queued transcription so a slow backend's backlog
        # isn't discarded when capture stops.
        pending = list(self._pending_tasks)
        if pending:
            logger.info(f"Draining {len(pending)} pending transcription task(s)")
            await asyncio.gather(*pending, return_exceptions=True)

    def reset(self):
        """Reset the transcriber state for a new session."""
        self._segment_index = 0
        self._samples_processed = 0
        self._is_speaking = False
        self._speech_buffer = []
        self._speech_duration = 0.0
        self._silence_duration = 0.0
        self._vad_frame_buf = np.zeros(0, dtype=np.float32)
        self._active = True
        if self._vad_model is not None:
            try:
                self._vad_model.reset_states()
            except Exception:
                pass
        if self._speaker_embedder:
            self._speaker_embedder.reset()
