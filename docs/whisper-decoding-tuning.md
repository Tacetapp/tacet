# Whisper Decoding Strategy Tuning

## Problem

Some words are dropped from transcription — whisper's language model overrides the acoustic signal for low-probability words. Example: speaker said "demo god" but transcription shows "the demo" because "demo god" is statistically unlikely.

## Root Cause

We use greedy decoding (`temperature=0.0`) which always picks the single highest-probability token. LocalVocal uses beam search (`beam_size=5`) which explores multiple paths in parallel and catches acoustically correct but statistically unlikely words.

## mlx-whisper Decoding Options

### Beam Search
- **NOT available** — `NotImplementedError("Beam search decoder is not yet implemented")`
- If/when implemented, use: `beam_size=5, patience=1.0`

### best_of (Available Alternative)
- Samples N independent transcriptions, picks the best by log probability
- Not true beam search (samples complete sequences independently, doesn't explore per-step)
- mlx-whisper CLI defaults to `best_of=5`
- Requires `temperature > 0` (sampling mode)

### Temperature Fallback Chain (Available)
- Pass `temperature=(0.0, 0.2, 0.4)` — tries greedy first, retries with sampling if quality checks fail
- Quality checks: `compression_ratio_threshold=2.4`, `logprob_threshold=-1.0`
- Good middle ground: fast when greedy works, retries when it doesn't

## Options to Evaluate

### Option A: Greedy (current)
```python
temperature=0.0
```
- Fastest (~0.3-0.5s for 5s audio on M4)
- Most likely to drop low-probability words
- No retry on quality failure

### Option B: best_of=5 with low temperature
```python
temperature=0.2,
best_of=5,
```
- ~5x slower per segment (~1.5-2.5s for 5s audio)
- Explores 5 alternative transcriptions
- Better chance of catching unusual words
- Low temperature keeps output close to greedy (not too random)

### Option C: Temperature fallback chain
```python
temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
```
- Starts with greedy (fast path)
- Only retries if compression_ratio > 2.4 or avg_logprob < -1.0
- Up to 6 attempts in worst case, but usually 1
- Default in OpenAI whisper and mlx-whisper CLI

### Option D: Moderate fallback + best_of
```python
temperature=(0.0, 0.2, 0.4),
best_of=3,
```
- Balanced: greedy first, retry with 3 candidates if needed
- Max ~9x slower worst case, usually 1x

## Evaluation Plan

Compare transcription accuracy across strategies using the same audio:

1. Record a meeting segment with OBS LocalVocal (beam_search, beam_size=5) as ground truth
2. Feed the same audio through mlx-whisper with each option (A, B, C, D)
3. Compare:
   - Word error rate (WER) vs LocalVocal output
   - Dropped words count
   - Hallucinated words count
   - Latency per segment
   - Repetition loop frequency

## How to Change

In `web/backend/transcriber.py`, the `_run_whisper` method:

```python
# Current (Option A):
result = mlx_whisper.transcribe(
    audio,
    ...
    temperature=0.0,
    verbose=False,
)

# Option B — add best_of:
result = mlx_whisper.transcribe(
    audio,
    ...
    temperature=0.2,
    best_of=5,
    verbose=False,
)

# Option C — temperature fallback:
result = mlx_whisper.transcribe(
    audio,
    ...
    temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
    verbose=False,
)
```

Note: `best_of` is passed via `**decode_options` — it's not an explicit parameter of `transcribe()` but gets forwarded to `DecodingOptions`.

## Other Factors Affecting Accuracy

- **condition_on_previous_text** — currently `False` (prevents repetition loops but reduces context). Re-enabling may help accuracy now that segments are longer (2-7s).
- **initial_prompt** — currently removed. Could re-add with a domain-specific vocabulary hint (e.g., product names or jargon common to your meetings) rather than feeding previous transcript.
- **Audio resampling** — fixed (AVAudioConverter), should no longer be a factor.
- **VAD boundaries** — words at the very start/end of speech segments may be partially cut. Could increase `VAD_SPEECH_PAD_MS` to add more padding.
