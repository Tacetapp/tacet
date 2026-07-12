# Privacy — what leaves this machine

Tacet is local-first by design. This page is the honest, specific account of what
stays on your Mac, what is sent to your chosen LLM provider, and what is never
collected at all. No marketing — just the data flow.

## The short version

- **Your audio never leaves your Mac and is never written to disk.** It exists
  only in memory, long enough to detect speech and transcribe it.
- **Transcription and speaker diarization run 100% locally** on models that live
  on your machine. There is no cloud speech-to-text.
- **Only text goes to the cloud** — transcript windows, your chat messages, and
  (only if you enable screen analysis) downscaled screen frames — and only to
  **the LLM provider you configured**. Tacet has no server of its own.

## Data flow

| Data | Where it goes | Controlled by |
|------|---------------|---------------|
| **Raw microphone + system audio** | RAM only, for VAD + transcription. **Never written to disk. Never sent over the network.** | — (always local) |
| **Transcription** | Local Whisper (mlx-whisper on Apple Silicon, faster-whisper on Intel). Stays on-device. | `WHISPER_BACKEND` and friends |
| **Speaker diarization** | Local ECAPA-TDNN voice embeddings + clustering (CPU). Stays on-device. | — (always local) |
| **Transcript excerpts** | Sent to your configured LLM provider for insights and summaries. With `LLM_PROVIDER=ollama`, inference is fully on-device — nothing leaves your Mac. | `LLM_PROVIDER` (+ provider keys) |
| **Your chat messages** | Sent to your configured LLM provider to answer them. With Ollama, these stay entirely on-device. | `LLM_PROVIDER` (+ provider keys) |
| **Screen frames** | Downscaled and sent to the provider's vision model — **only when screen analysis is on**. Off means nothing is captured or sent. | `SCREEN_ANALYSIS_ENABLED` |
| **Voice embeddings for named speakers** | Stored locally, **opt-in**, deletable. Numeric fingerprints only — no audio. Used to re-label known voices in future meetings. | `SPEAKER_REGISTRY_ENABLED` |

"Your configured LLM provider" is whichever of OpenRouter, Venice, Ollama, or
AWS Bedrock you selected. With Ollama, inference runs entirely on-device and no
text leaves your Mac. For all other providers, your data is subject to that
provider's terms — Tacet never routes it anywhere else.

## What's stored on disk

All of it lives on your Mac as ordinary local files:

- **Meetings** — one markdown file per session (transcript, insights, chat,
  summary) in `MEETINGS_DIR` (default `<repo>/meetings`).
- **Knowledge vault** — a projects/people/sources wiki synthesized from your
  meetings, in `KNOWLEDGE_DIR` (default `<meetings>/knowledge`).
- **Config** — non-secret settings in `~/Library/Application Support/Tacet/config.json`
  (desktop app) or your environment / `.env` (web server).
- **Encrypted secrets** — provider API keys are stored encrypted via the macOS
  keychain (`safeStorage`) in `~/Library/Application Support/Tacet/secrets.json`,
  never in plaintext config.
- **Voice profile** — `voice_profile.npy` / `voice_profile.json` at the repo
  root, used to label your own speech as "You".
- **Speaker registry** (opt-in) — `speaker-registry.json` beside your meetings
  folder; only present if you enabled it.
- **Logs** — `~/Library/Application Support/Tacet/server.log` (desktop app) or
  wherever `LOG_FILE` points. Logs rotate (5 MB × 3). With `LOG_CONTENT=false`,
  transcript and chat text are replaced by character counts in the logs.

## What is never collected

- **No telemetry.** Tacet phones no home; there are no usage metrics or crash
  reporters baked in.
- **No analytics.**
- **No accounts.** There is no Tacet login or Tacet server.
- **No cloud storage of your data.** Your meetings, vault, and settings live only
  on your Mac (and in whatever your own LLM provider retains per its terms).

## A note on meeting-participant consent

Tacet minimizes its footprint — no bot joins the call, and audio never leaves
your machine — but **transcribing a conversation may still count as recording**
under some laws. Consent and recording rules vary by jurisdiction (some places
require every participant's consent). **You are responsible for complying with
the recording and privacy laws that apply to you and the people in your
meetings.** Check before you use Tacet in a given setting.

## How to delete everything

Remove any of these you want gone:

```bash
# Meetings + knowledge vault (adjust if you set MEETINGS_DIR / KNOWLEDGE_DIR)
rm -rf ./meetings

# Voice profile
rm -f ./voice_profile.npy ./voice_profile.json

# Opt-in speaker registry (path may differ if you set SPEAKER_REGISTRY_PATH)
rm -f ./speaker-registry.json

# Desktop app: config, encrypted secrets, and logs
rm -rf ~/Library/Application\ Support/Tacet
```

Deleting `secrets.json` (or the whole `Tacet` support folder) forgets your saved
provider API keys. Deleting the speaker registry forgets every named voice.
