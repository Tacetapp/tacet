# Mic bleed mitigation — design notes & history

Reference doc for everything we've done to prevent the mic channel from
duplicating the remote speaker's audio (bleed through the user's speakers
back into the mic). Captures both the problem, the layered guards we added,
their failure modes, and what's parked as future work.

## Problem statement

The capture pipeline feeds two independent channels into whisper:

- **System** audio — what the meeting app (Teams/Zoom/etc.) plays out, labelled
  `Others` by default and relabelled `Speaker N` per voice diarization.
- **Mic** audio — what the laptop mic picks up, labelled `You` in the transcript.

When the user is in a meeting with speakers turned on (not headphones), the mic
inevitably picks up the remote participant's voice. That audio flows through
the mic VAD → mlx-whisper → transcript, producing duplicate lines labelled
`You` for things the user never actually said.

Symptoms:

- Same sentence appears twice in the transcript — once attributed to a real
  speaker, once attributed to `You`.
- Short fragments labelled `You` that the user did not utter (whisper
  hallucinations on faint bleed audio).
- A full remote monologue echoed on the mic channel.

## Guard layers (stacked, in firing order)

### 1. Text-similarity dedup (pre-existing, `engine.py`)

After both channels produce transcript segments, `MeetingEngine._segments_match`
compares mic segments against system segments using `SequenceMatcher.ratio`.
Mic segments with similar text to a system segment are discarded as bleed.

Key constants in `engine.py`:
- `DEDUP_WINDOW_S = 3.0` — how long a mic segment is buffered before being
  emitted, giving a system duplicate a chance to arrive first.
- `DEDUP_TS_TOLERANCE = 15.0` — max timestamp gap between channels for them to
  count as "same event". Started at 1.5, widened to 5.0, then to 15.0 because
  the 20s VAD cap can make the same wall-clock moment land far apart in
  per-channel SRT time.
- `DEDUP_TEXT_THRESHOLD = 0.6` — min ratio to declare "same utterance".

**Fails when:** the mic fragment is a *substring* of a longer system segment
(ratio penalizes length mismatch). Extended by guard #2.

### 2. Substring & fuzzy-containment dedup (Option A from discussion)

`engine.py:_segments_match` also normalizes both texts (lowercase + strip
punctuation) and falls back to:
- Literal substring check (shorter text inside longer).
- Longest common block ≥ `DEDUP_CONTAINMENT_THRESHOLD = 0.8` of the shorter
  text's length.
- Minimum fragment floor `DEDUP_MIN_FRAGMENT_CHARS = 15` to avoid false
  positives on tiny fillers.

**Catches:** fragment-in-whole bleed (e.g. mic hears "with that big of an
audience," which is contained in a 300-char system utterance).
**Fails when:** mic and system whisper runs split at different sub-segment
boundaries AND the mix of channels diverges enough that neither ratio nor
containment passes.

### 3. Joined-batch dedup (Option B from discussion)

`engine.py:_join_batch` concatenates a batch of system sub-segments into one
synthetic segment. `_is_mic_bleed` tries the mic segment against the individual
subs AND against the joined whole.

**Catches:** cross-boundary fragments where a mic sub-segment straddles two
system sub-segments and doesn't match either one alone.
**Fails when:** the mic is genuinely mixed — part real user speech, part bleed
in a single buffer. Text dedup can't do much there.

### 4. RMS-ratio bleed guard (Option 2 from discussion, `transcriber.py`)

Runs *before* whisper. System transcriber records its peak-RMS per turn into
a shared `ChannelActivityTracker`. Mic transcriber reads the tracker and, if:
- system channel had peak RMS ≥ `BLEED_GUARD_MIN_SYSTEM_RMS = 0.02` in the last
  `BLEED_GUARD_WINDOW_S = 3.0` seconds, AND
- `mic_rms < system_rms * BLEED_GUARD_RMS_RATIO (0.5)`,

then the mic buffer is dropped before whisper runs. Kills whisper
hallucinations on near-silent mic that's really just faint speaker bleed.

**Catches:** the common case where the mic picks up faint leakage at ~30-60%
of the system RMS.
**Fails when:** speakers are loud or echo cancellation is off, so the mic
captures the remote voice at near-full volume (`ratio` ≈ 0.7-1.0). RMS alone
can't distinguish real user speech from loud bleed.

### 6. Voice fingerprint (optional, `voice_profile.py`)

Strictly opt-in. User records a ~10s voice sample via the gear-icon settings
menu. Backend extracts an ECAPA embedding and saves it to
`voice_profile.npy` alongside `user_profile.json`. At runtime, every mic VAD
buffer ≥ 2s gets embedded and cosine-compared to the stored reference.
Below `VOICE_FINGERPRINT_THRESHOLD = 0.55` → drop before whisper.

**Runs after guards #4 and #5** so the cheap heuristics filter first; voice
fingerprinting is deterministic but costs an ECAPA inference (~50-200ms CPU)
per mic turn.

**Pros:** deterministic — kills loud bleed and in-room cross-talk that the
RMS / VAD heuristics can't touch. Opt-in means no behavior change for users
who don't set it up.
**Cons:** needs a one-time setup flow. False positives are a dead-end for the
user (dropped audio is gone) — the delete path in the gear menu is their
escape hatch.

WebSocket protocol:
- `voice_profile_status` → server replies `{type: voice_profile, exists, created_at?, duration_s?}`
- `voice_profile_save` (with base64 PCM) → server embeds + saves, replies same
- `voice_profile_delete` → server removes files + in-memory state

### 5. VAD-overlap bleed guard (Option E from discussion, `transcriber.py`)

The headline guard for **loud bleed**. System VAD turns are published into the
same `ChannelActivityTracker` as `(start_ts, end_ts, peak_prob)` tuples. Before
running whisper, the mic transcriber checks:

- Does a recorded system VAD turn overlap ≥ `VAD_BLEED_MIN_OVERLAP_RATIO (0.5)`
  of the mic's VAD window?
- Was the system's peak VAD probability ≥ `VAD_BLEED_SYSTEM_PROB_FLOOR (0.90)`?
- Was the mic's peak VAD probability ≤ `VAD_BLEED_MIC_PROB_CEILING (0.80)`?

If all three: drop.

**Why it works:** bleed is a reverberated, attenuated copy of a primary signal.
The primary channel's VAD fires with high confidence (0.95-1.00); the bleed
channel's VAD fires less confidently because the signal is noisier. This is
robust to volume — unlike the RMS guard.

**Fails when:** the user interjects briefly (1-2 words) during someone else's
speech AND the mic VAD fires with low confidence. Tunable by tightening the
probability thresholds if false positives show up.

## Chronological timeline of what we tried

| Attempt | File(s) | What changed | Result |
|---|---|---|---|
| Widen dedup timestamp tolerance | `engine.py` | `DEDUP_TS_TOLERANCE` 1.5 → 5.0 | Caught more same-event pairs across channels. |
| Substring / containment dedup | `engine.py` | Added normalized substring + longest-common-block fallback with `DEDUP_CONTAINMENT_THRESHOLD=0.8`, `DEDUP_MIN_FRAGMENT_CHARS=15` | Caught fragment-in-whole bleed (e.g. 29-char mic fragment inside 300-char system text). |
| Widen timestamp tolerance further | `engine.py` | `DEDUP_TS_TOLERANCE` 5.0 → 15.0 | Compensates for 20s VAD cap per-channel drift. |
| Joined-batch dedup | `engine.py` | Added `_join_batch`, `_is_mic_bleed` — compares mic segs against both individual system subs and their concatenation | Caught cross-boundary bleed. |
| RMS bleed guard | `transcriber.py`, `main.py` | New `ChannelActivityTracker`; mic path skips whisper when `mic_rms < system_rms * 0.5` | Killed faint-bleed hallucinations (≥90% of the "I never spoke" cases). |
| VAD-overlap bleed guard | `transcriber.py` | Track system VAD turn peak probs; mic path drops when overlap + prob mismatch | Targets LOUD bleed where RMS guard can't help. |

## Known residual failure modes

After all five guards, the following bleed patterns can still slip through.
Captured here so the next change is a fix, not a rediscovery.

1. **User briefly interjects over a loud remote speaker.** Both VAD
   probabilities can fire with similar high confidence, and mic RMS can match
   system RMS. No guard handles this robustly with heuristics alone.
   → Long-term answer: voice fingerprinting (task #1).

2. **Whisper transcribes mic bleed with slightly different wording than
   whisper on system.** Same audio, different text (mic version is usually
   noisier). Text dedup then can't match. The RMS + VAD-overlap guards
   mitigate this by dropping the mic run before whisper.

3. **Monologue spanning the 20s `VAD_MAX_SPEECH_S` cap.** The cap forces a
   mid-sentence split, and the two channels split at different points.
   Individual-sub dedup fails; joined-batch dedup handles most of these, but
   mixed user+remote audio in one forced-split buffer stays hard.

## Tunable constants — quick reference

All in `web/backend/transcriber.py` and `web/backend/engine.py`:

```
# engine.py — post-transcription text dedup
DEDUP_WINDOW_S             = 3.0
DEDUP_TS_TOLERANCE         = 15.0
DEDUP_TEXT_THRESHOLD       = 0.6
DEDUP_CONTAINMENT_THRESHOLD = 0.8
DEDUP_MIN_FRAGMENT_CHARS   = 15

# transcriber.py — pre-whisper RMS guard
BLEED_GUARD_WINDOW_S       = 3.0
BLEED_GUARD_RMS_RATIO      = 0.5
BLEED_GUARD_MIN_SYSTEM_RMS = 0.02

# transcriber.py — pre-whisper VAD overlap guard (Option E)
VAD_BLEED_MIN_OVERLAP_RATIO  = 0.5
VAD_BLEED_MIC_PROB_CEILING   = 0.80
VAD_BLEED_SYSTEM_PROB_FLOOR  = 0.90

# voice_profile.py — pre-whisper voice fingerprint (opt-in)
VOICE_FINGERPRINT_THRESHOLD   = 0.55
VOICE_PROFILE_MIN_DURATION_S  = 5.0
VOICE_MATCH_MIN_DURATION_S    = 2.0
```

**Tuning direction:**
- Bleed still getting through → lower `BLEED_GUARD_RMS_RATIO` (e.g. 0.7) or
  `VAD_BLEED_SYSTEM_PROB_FLOOR` (e.g. 0.85).
- Real user speech being dropped → lower `VAD_BLEED_MIC_PROB_CEILING` (e.g.
  0.6) or raise `VAD_BLEED_MIN_OVERLAP_RATIO` (e.g. 0.8).

## Options discussed but not (yet) implemented

- **"I'm muted" toggle (Option 5).** Explicit UI checkbox that drops all mic
  audio unconditionally while on. Zero false positives, but needs user
  discipline.
- **Meeting-app mute state detection (Option 6/F).** Read Teams/Zoom mute
  state via macOS Accessibility API or screenshot OCR. Fully automatic
  version of the mute toggle. Moderate complexity per-app.
- **Joined-text full-batch comparison against whole batch.** Rejected due to
  false-positive risk on long monologues sharing filler with unrelated mic
  utterances. The conservative variant (`_join_batch` that requires ≥15 char
  substring) ships, but the aggressive variant does not.

## Related code

- `web/backend/transcriber.py` — VAD, `ChannelActivityTracker`, pre-whisper
  guards, per-turn peak prob + wall-clock tracking.
- `web/backend/engine.py` — post-whisper text dedup, mic buffer pending
  window, `_segments_match`, `_is_mic_bleed`, `_join_batch`.
- `web/backend/main.py` — wires the shared `ChannelActivityTracker` between
  the system and mic transcribers.
- `docs/audio-pipeline-flow.md` — end-to-end pipeline doc with the exact
  point where each guard sits in the flow.
