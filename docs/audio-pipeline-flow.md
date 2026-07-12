# End-to-End Audio Pipeline: Capture → Transcript

Reference doc for the entire path an utterance travels from the microphone /
system audio buffer through ScreenCaptureKit, VAD, whisper, diarization, and
into a transcript line with a speaker label on it. Every tuning knob is
documented with its file, value, and effect.

## Pipeline overview

```
┌─────────────────────────────────────────────────────────────────────┐
│ 1. ScreenCaptureKit (Swift)                                          │
│    SCStream @ 48kHz  →  AVAudioConverter  →  16kHz float32 mono      │
│    stdin JSON command, stdout framed binary (type + length + body)   │
└─────────────────────────────────────────────────────────────────────┘
                              │ MSG_AUDIO (0x01) / MSG_MIC_AUDIO (0x04)
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 2. AudioCaptureSource (Python subprocess bridge)                     │
│    Reads framed messages, fans out to two callbacks (no buffering)   │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 3. Silero VAD (per-channel)                                          │
│    Slices incoming audio into 512-sample (32ms) frames.              │
│    State machine: speech / silence; emits a buffer once a speech     │
│    burst is terminated by ≥800ms silence.                            │
└─────────────────────────────────────────────────────────────────────┘
                              │ audio: np.ndarray (one turn)
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 4a. SpeakerEmbedder.identify() ──┐   (CPU, no lock, parallel)        │
│     ECAPA-TDNN → 192-dim emb →   │                                   │
│     cosine match vs known        │                                   │
│     centroids; returns "Speaker  │                                   │
│     3" or None                   │                                   │
│                                  ▼                                   │
│ 4b. mlx-whisper.transcribe()  (Metal GPU, under shared lock)         │
│     whisper-large-v3-turbo; returns N sentence sub-segments          │
│                                  │                                   │
│     await both → zip speaker onto every sub-segment                  │
└─────────────────────────────────────────────────────────────────────┘
                              │ list[SrtSegment]
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 5. MeetingEngine.on_new_segments()                                   │
│    Mic↔system dedup (3s buffer), name-registry resolution,           │
│    emit to WebSocket, trigger analysis / rolling summary             │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                       WebSocket → Frontend
```

## 1. ScreenCaptureKit (Swift)

**File:** `swift/CaptureHelper/Sources/CaptureManager.swift`

**What it does:** Runs macOS `SCStream` to capture system audio + screen, plus
`AVAudioEngine` to capture the mic. Resamples all audio to 16kHz float32 mono
and writes framed binary messages to stdout.

### System audio config (`CaptureManager.swift:82-95`)

| Setting | Value | Notes |
|---|---|---|
| `config.sampleRate` | 48000 | SCK native rate |
| `config.channelCount` | 1 | mono |
| Target format | `pcmFormatFloat32, 16000Hz, mono` | what whisper wants |
| Resampler | `AVAudioConverter` | proper anti-aliased downsample, cached per source format |
| Buffer size | ~10–20ms per SCK callback | no batching, forwarded immediately |

### Mic config (`CaptureManager.swift:263-296`)

| Setting | Value | Notes |
|---|---|---|
| Engine | `AVAudioEngine` + `inputNode.installTap` | |
| Tap `bufferSize` | 8192 | ~170ms at 48kHz |
| Resampler | `AVAudioConverter` per tap call | |
| Output | `MSG_MIC_AUDIO` (0x04) | routed to `mic_transcriber` |

### Screen frame config (`CaptureManager.swift:91-94`, `315-319`)

| Setting | Value | Notes |
|---|---|---|
| `config.width × height` | 1920 × 1080 | fixed |
| `config.minimumFrameInterval` | `screenInterval` seconds | hint only |
| App-side gate | `now - lastFrameTime >= screenCaptureInterval` | hard rate-limit |
| `screen_interval` default (Python) | 20.0s | `main.py:199` |
| JPEG quality | 0.7 | |

### Protocol

Header: 1 byte type + 4 bytes BE length, followed by payload.

| Type | Value | Payload |
|---|---|---|
| `MSG_AUDIO` | 0x01 | system audio PCM |
| `MSG_SCREEN_FRAME` | 0x02 | JPEG bytes |
| `MSG_STATUS` | 0x03 | JSON status |
| `MSG_MIC_AUDIO` | 0x04 | mic PCM |

## 2. Python subprocess bridge

**File:** `web/backend/audio_capture.py`

`AudioCaptureSource` runs the Swift binary as a subprocess, reads framed
messages from stdout in a background task, and fans them out via three
callbacks. **Zero buffering here** — every chunk forwards immediately:

- `MSG_AUDIO` → `system_transcriber.feed_audio()`
- `MSG_MIC_AUDIO` → `mic_transcriber.feed_audio()`
- `MSG_SCREEN_FRAME` → `screen_analyzer.on_frame()`

No timing knobs in this layer.

## 3. Silero VAD

**File:** `web/backend/transcriber.py:17-23`

**What it does:** Before paying the whisper cost, decides what's speech vs
silence. Only speech buffers get transcribed.

### Frame rate

| Constant | Value | Effect |
|---|---|---|
| `VAD_FRAME_SAMPLES` | 512 | **32ms** at 16kHz — Silero's native frame size |
| `SAMPLE_RATE` | 16000 | |

Audio arrives in whatever chunk size SCK emits; `feed_audio` concatenates it
into `_vad_frame_buf` and slices out 512-sample frames
(`transcriber.py:156-164`). Any leftover stays in the buffer for the next call.

### Decision thresholds

| Constant | Value | Effect |
|---|---|---|
| `VAD_THRESHOLD` | 0.5 | speech probability floor per frame |
| `VAD_MIN_SILENCE_MS` | 800 | silence after speech that ends a turn → triggers transcription |
| `VAD_MIN_SPEECH_MS` | 500 | turns shorter than this are discarded (noise, coughs) |
| `VAD_MAX_SPEECH_S` | 20.0 | force transcription if someone talks this long straight |
| `SILENCE_RMS_THRESHOLD` | 0.003 | after VAD greenlights a buffer, if RMS < this, skip transcription anyway |

### State machine (`_process_vad_frame`, line 166)

- `not speaking` → speech_prob ≥ 0.5 → mark speaking, emit `speech_start`.
- `speaking` → speech_prob < 0.5 → accumulate silence counter; at 800ms, if
  total speech ≥ 500ms, fire transcription; else discard.
- `speaking` → speech_prob ≥ 0.5 → reset silence counter, accumulate buffer;
  at 20s speech, force transcription.

**Practical latency floor** = 800ms silence wait + whisper time.

## 4. mlx-whisper

**File:** `web/backend/transcriber.py:303-355`

**What it does:** Once VAD hands off a speech buffer, whisper transcribes
it on the Metal GPU.

### Model

| Setting | Value | Notes |
|---|---|---|
| `DEFAULT_MODEL` | `mlx-community/whisper-large-v3-turbo` | override via `WHISPER_MODEL` env var |
| Local path resolution | `huggingface_hub.snapshot_download(local_files_only=True)` | one-time, avoids per-call HF API lookup |

### Decode config (`transcriber.py:309`)

| Param | Value | Why |
|---|---|---|
| `language` | "en" | forced, skips language-detection pass |
| `condition_on_previous_text` | False | prevents prompt-drift hallucination cascade |
| `no_speech_threshold` | 0.6 | whisper's own no-speech gate |
| `compression_ratio_threshold` | 2.4 | rejects too-repetitive output |
| `logprob_threshold` | -1.0 | rejects low-confidence output |
| `word_timestamps` | False | saves significant compute |
| `temperature` | 0.0 | greedy, deterministic |
| `verbose` | False | |

### Concurrency

Shared `asyncio.Lock` between the two transcribers (`main.py:158`). Metal GPU
can't handle concurrent mlx-whisper calls — they crash. The lock serializes
them, so system and mic audio take turns on the GPU.

### Post-filters (`transcriber.py:322-337`)

- `_HALLUCINATION_PATTERNS` regex drops "thank you", "[Music]", "please
  subscribe", etc.
- `_REPETITION_RE` drops decoder loops (10+ char phrase repeated 3+ times).

## 5. Speaker diarization (runs in parallel with whisper)

**File:** `web/backend/speaker_embedder.py`

**What it does:** Runs on CPU in parallel with whisper on Metal GPU. Takes the
same speech buffer VAD selected, extracts an ECAPA-TDNN voice embedding, and
assigns it to a cluster.

### Parallel launch (`transcriber.py:255-259`)

```python
speaker_task = asyncio.create_task(
    asyncio.to_thread(self._speaker_embedder.identify, audio)
)
# ... whisper runs under the GPU lock ...
speaker = await asyncio.wait_for(speaker_task, timeout=5.0)
```

Embedding extraction overlaps with whisper decode — adds zero latency unless
it's slower than whisper (it isn't).

### Model

- `speechbrain/spkrec-ecapa-voxceleb`, CPU only.
- Saved under `models/speechbrain-ecapa`.
- Pre-loaded at meeting start in a background task (`main.py:143`) so the
  first utterance isn't delayed by cold-start.

### Clustering config

| Constant | Value | Effect |
|---|---|---|
| `MIN_AUDIO_DURATION` | 2.0s | below this, `identify()` returns None → channel default used (was 0.8s, raised to reduce cluster-spawn noise) |
| `SIMILARITY_THRESHOLD` | 0.55 | cosine similarity floor to match existing cluster; below = new cluster (lowered from 0.75 — ECAPA scores the same short-clip voice at 0.55–0.65, so the higher bar over-split single speakers) |
| `EMBEDDING_UPDATE_RATE` | 0.1 | running-average weight for matched-cluster centroid |
| `RECLUSTER_DISTANCE` | 0.55 | mid-meeting / end-of-meeting agglomerative merge threshold (cosine distance ≈ 0.45 similarity; must stay *strictly looser* than 1 − SIMILARITY_THRESHOLD = 0.45 or the global merge goes redundant — at exactly 0.45 it was a no-op, 22 clusters → 22 groups). Each reconcile run logs a distance sweep (0.40–0.65) to pick this cut empirically against the real headcount. |

### Embedding timeout

`asyncio.wait_for(..., timeout=5.0)` (`transcriber.py:282`). If the model is
still loading on first call, give up on speaker ID rather than block segment
delivery. Rare in practice because of the pre-load above.

### End-of-meeting reconciliation

In `_handle_stop`, before the batch prompt:
`SpeakerEmbedder.reconcile_clusters()` runs scipy agglomerative hierarchical
clustering across all collected embeddings with threshold 0.55, produces a
`{fragmented_cluster: canonical_cluster}` mapping, merges fragments back
together. Engine rewrites segment labels + name-registry state before the
batch speaker-naming prompt and the summary generation.

## How diarization and whisper outputs get matched

**The match is by shared audio buffer, not by timestamp alignment.**

One VAD-bounded speech buffer goes into two parallel functions:

- `SpeakerEmbedder.identify(audio)` → one cluster label (`"Speaker 3"` or `None`).
- `mlx_whisper.transcribe(audio)` → list of sentence sub-segments.

After both return, every sub-segment is stapled with the single speaker label
the embedding produced. No cross-checking, no alignment math.

### Code path (`transcriber.py:235-293`)

```python
# 1. Fire embedding on the SAME buffer (CPU, no lock)
speaker_task = asyncio.create_task(
    asyncio.to_thread(self._speaker_embedder.identify, audio)
)

# 2. Whisper runs on the SAME buffer (Metal GPU, shared lock)
async with self._lock:
    segments = await asyncio.to_thread(
        self._run_whisper, audio, start_time, duration
    )

# 3. Await the speaker result (5s timeout)
speaker = await asyncio.wait_for(speaker_task, timeout=5.0)

# 4. Stamp every sub-segment with the one speaker label
if speaker and segments:
    segments = [
        SrtSegment(s.index, s.start, s.end, s.text, speaker=speaker)
        for s in segments
    ]

await self.on_segments(segments)
```

### Why one-buffer-one-speaker is safe by construction

VAD gives us a buffer only after ≥800ms of silence on both sides. That silence
gap defines a turn. Under the assumption that one turn = one speaker, the
pipeline is trivially correct. ECAPA produces a single embedding from the
whole buffer; whisper may split into multiple sentences but they all inherit
the same speaker label — which is correct when one person speaks multiple
sentences in a row.

### Fallback chain

`transcriber.py:349` — `_run_whisper` builds `SrtSegment` objects with a
**channel default** speaker: `"You"` for the mic transcriber, `"Others"` for
the system-audio transcriber. The overwrite to `speaker` only happens
`if speaker and segments:`. So if `identify()` returned `None` (buffer below
`MIN_AUDIO_DURATION`, too quiet, or timed out), the segment keeps the channel
default — no spurious `Speaker N` gets invented.

### Failure modes (when one-buffer-one-speaker breaks)

1. **Two people talk over each other within one VAD turn.** VAD sees
   continuous speech, produces one buffer with mixed voices. ECAPA extracts
   one chimeric embedding that matches neither person well. Score drops below
   `SIMILARITY_THRESHOLD = 0.75` → spurious new cluster spawned. Primary root
   cause of the "Speaker 25 / Speaker 31" fragmentation in crosstalk-heavy
   meetings.

2. **Speaker changes without a full 800ms silence gap.** VAD keeps
   accumulating into one buffer. One buffer, two speakers — chimera again.

3. **Forced cutoff at `VAD_MAX_SPEECH_S`.** Mostly harmless if the speaker
   hasn't changed, but risky if the cutoff lands mid-overlap.

All three are mitigated (not fixed) by the end-of-meeting re-clustering pass.

## 6. Engine dedup + delivery

**File:** `web/backend/engine.py`

**What it does:** Segments arrive from both transcribers. Engine dedups mic vs
system audio bleed (your voice captured on both channels) and emits over
WebSocket.

### Dedup config (`engine.py:49-52`)

| Constant | Value | Effect |
|---|---|---|
| `DEDUP_WINDOW_S` | 3.0s | mic segments buffer this long; if a matching system segment arrives, mic copy is dropped as bleed |
| `DEDUP_TS_TOLERANCE` | 1.5s | max timestamp diff for two segments to be considered "same event" |
| `DEDUP_TEXT_THRESHOLD` | 0.6 | `SequenceMatcher` ratio floor for text similarity |

System segments emit immediately; mic segments sit for up to 3s before
confirmation. `_flush_mic_buffer` wakes after 3s and emits whatever survived.

### Analysis triggers

| Constant | Value | Effect |
|---|---|---|
| `ANALYSIS_WINDOW` | 50 | segments included as context in insight analysis |
| `CHAT_WINDOW` | 100 | segments included as chat context |
| `ROLLING_SUMMARY_INTERVAL` | 200 | rolling summary regenerated every N segments |
| `SPEAKER_PROMPT_THRESHOLD` | 10 | utterances from an unresolved cluster before prompting the user for a name |
| Insight curation | every 50 segments, if >5 insights | prune stale/redundant insights |

## End-to-end latency

For a 4-second spoken phrase:

1. Person speaks → SCK emits ~10ms audio buffers continuously.
2. Swift resamples 48→16kHz per buffer, forwards immediately. **(~1ms)**
3. Python subprocess read loop dispatches. **(sub-ms)**
4. VAD processes 32ms frames; first frame above 0.5 flips `is_speaking=True`.
   User keeps talking → frames accumulate in `_speech_buffer`. **(real-time)**
5. User stops. 800ms of silence frames must accumulate before VAD fires
   transcription. **← +800ms floor**
6. `_transcribe_audio` starts the speaker embedding task (CPU, immediately)
   and awaits the GPU lock.
7. Whisper runs on the buffer. For a 4s buffer on whisper-large-v3-turbo +
   MLX on Apple Silicon: ~300–800ms.
8. Speaker embedding finishes by now (CPU, ~100–300ms, always faster than
   whisper).
9. Segment emitted over WebSocket.
10. If mic segment: sits another 3s for dedup confirmation. If system: emits
    immediately.

**Typical end-to-end latency after someone stops talking:**

- System audio (others): ~800ms (VAD) + ~500ms (whisper) = **~1.3s**
- Mic audio (you): ~800ms + ~500ms + up to 3000ms (dedup) = **up to ~4.3s**

The 3s dedup window is the single biggest cost on mic latency. It's there
because system audio often replays your voice with slight delay (Zoom/Teams
echo from speakers back into the mic path) — we want to drop the mic copy to
avoid double-printed transcript lines.

## Key files

| File | Role |
|---|---|
| `swift/CaptureHelper/Sources/CaptureManager.swift` | ScreenCaptureKit stream, AVAudioEngine mic, 48→16kHz resample |
| `web/backend/audio_capture.py` | Subprocess bridge, framed-binary reader, callback dispatch |
| `web/backend/transcriber.py` | Silero VAD + mlx-whisper; parallel launches speaker embedder |
| `web/backend/speaker_embedder.py` | ECAPA-TDNN, online clustering, end-of-meeting re-clustering |
| `web/backend/engine.py` | Dedup, name registry, WebSocket emission, analysis triggers |
| `web/backend/main.py` | FastAPI WebSocket server, per-client engine/transcriber lifecycle |
