# Tacet

**A local-first meeting assistant for macOS. No bots in your calls, no audio leaves your Mac.**

*Tacet* captures meeting audio with ScreenCaptureKit, transcribes and identifies
speakers **entirely on your machine**, and uses **the inference provider of your
choice** — OpenRouter, Venice, or fully local via Ollama — for live
insights, chat, and summaries. It ships as a native desktop app.

---

## How it works

1. **Local capture** — ScreenCaptureKit captures system audio (remote
   participants), your microphone, and periodic screen frames as separate
   streams. Nothing joins your meeting.
2. **Local transcription + diarization** — Silero VAD finds speech, Whisper
   transcribes it (mlx-whisper on Apple Silicon, faster-whisper on Intel), and
   ECAPA-TDNN voice embeddings cluster speakers into stable labels
   (Speaker 1, Speaker 2, …; your mic is "You"). All of this runs on-device.
3. **Your LLM, text only** — only transcript windows, your chat messages, and
   (when you enable screen analysis) downscaled screen frames are sent to your
   configured provider. Raw audio never is.
4. **Notes you keep** — live notes during the meeting, a structured summary with
   action items at the end, and a searchable **knowledge vault** built from your
   meetings — all saved locally as markdown.

## Privacy at a glance

| Data | What happens to it |
|------|--------------------|
| **Raw audio** (mic + system) | Held in RAM for VAD/transcription only. **Never written to disk. Never sent anywhere.** |
| **Transcription + speaker diarization** | 100% local — Whisper + ECAPA-TDNN run on your Mac. No cloud STT. |
| **Transcript excerpts + your chat messages** | Sent to **your configured LLM provider** (Bedrock / OpenRouter / Venice) for insights, chat, and summaries (or no cloud at all with Ollama — everything stays on your Mac). |
| **Screen frames** | Only when screen analysis is enabled; downscaled and sent to the provider's vision model. Off → nothing is sent. |
| **Voice embeddings** (optional speaker registry) | Local, opt-in, and deletable. No audio is stored — only numeric fingerprints of names you confirm. |
| **Storage** | Meetings, the knowledge vault, and config live on your Mac as local files. |

Full details and the exact env toggles are in **[PRIVACY.md](PRIVACY.md)**.

## Install

```bash
git clone https://github.com/tacetapp/tacet.git
cd tacet
make setup      # one-time setup: venv, deps, frontend, Swift binary, models
make app        # launch the desktop app  (runs: cd electron && npm install && npm start)
```

First launch runs a short onboarding: pick your LLM provider, paste a key if it
needs one, done.

> Packaged releases are coming — once they ship, install becomes
> `brew install --cask tacetapp/tap/tacet` with no build tools required.
>
> Release DMGs are currently **unsigned**: if macOS reports the app "damaged"
> after a browser download, run `xattr -cr /Applications/Tacet.app` once and
> open normally (browser downloads carry a quarantine flag; `curl -LO` and
> Homebrew installs do not).

On first meeting start, macOS prompts for **Screen Recording** and **Microphone**
permissions — click Allow.

**Requirements:** macOS 13+ (Ventura, for ScreenCaptureKit) · Apple Silicon or
Intel Mac · Node 20+ · Python 3.11+ · Xcode Command Line Tools (for the Swift
build). Whisper and speaker models download from Hugging Face on first run.

## Choose your LLM provider

Tacet is bring-your-own-LLM. Pick a provider in onboarding; API keys are stored
encrypted in the macOS keychain. (Contributors running the bare dev server
configure the same variables via `.env` — see [Development](#development).)

### OpenRouter (recommended)

Set `LLM_PROVIDER=openrouter` and provide `OPENROUTER_API_KEY` and
`OPENROUTER_MODEL`. OpenAI-compatible; a wide model catalog.

### Venice

Set `LLM_PROVIDER=venice` and provide `VENICE_API_KEY` and `VENICE_MODEL`.
OpenAI-compatible; privacy-focused.

### Ollama (fully local)

1. Install [Ollama](https://ollama.com) and pull a model: `ollama pull llama3.1`
2. Pick **Ollama** in onboarding.

With Ollama, meeting text and chat messages never leave your Mac — inference runs
entirely on-device. No API key is needed. Screen analysis (vision model) is not
available with Ollama.

Set `OLLAMA_MODEL` to any model you have pulled (default `llama3.1`). Override
the endpoint with `OLLAMA_BASE_URL` if Ollama is running on a non-default port.

### AWS Bedrock

1. In the AWS console, open **Bedrock → API keys** and generate a key.
2. Pick **Bedrock** in onboarding, paste the key, and set an AWS region.

Bedrock runs through the official Anthropic SDK and uses `anthropic.`-prefixed
model IDs (`BEDROCK_MODEL_ID`, default `anthropic.claude-opus-4-8`). No AWS CLI
or SSO setup is required. Advanced fallback: leave the key blank to use an AWS
credential chain instead (`AWS_PROFILE` / SSO).

### Key environment variables

| Variable | Description |
|----------|-------------|
| `LLM_PROVIDER` | `openrouter` (default), `venice`, `ollama`, or `bedrock` — set by onboarding; dev server reads it from `.env` |
| `WHISPER_BACKEND` | Force `mlx` or `faster-whisper` (default: auto-detect by chip) |
| `MEETINGS_DIR` | Where meetings are saved as markdown (default: `<repo>/meetings`) |
| `KNOWLEDGE_DIR` | Where the knowledge vault is built (default: `<meetings>/knowledge`) |
| `SCREEN_ANALYSIS_ENABLED` | `false` to disable screen frames entirely |
| `WS_AUTH_TOKEN` | If set, every WebSocket connection must present this token |
| `LOG_CONTENT` | `false` to log lengths instead of transcript/chat text |
| `HF_TOKEN` | Optional Hugging Face token for faster model downloads |

Provider-specific keys — **Bedrock:** `AWS_BEARER_TOKEN_BEDROCK`, `AWS_REGION`,
`AWS_PROFILE`, `BEDROCK_MODEL_ID`, `BEDROCK_FAST_MODEL_ID`,
`BEDROCK_VISION_MODEL_ID`. **OpenRouter/Venice:** `*_API_KEY`, `*_MODEL`,
`*_FAST_MODEL`. **Ollama:** `OLLAMA_MODEL` (default `llama3.1`),
`OLLAMA_BASE_URL` (default `http://localhost:11434/v1`), `OLLAMA_FAST_MODEL`
(optional; falls back to `OLLAMA_MODEL`). The desktop app exposes the full set
in Settings; see [`electron/config.js`](electron/config.js) for every option.

## Features

- **Live transcript** with voice-based speaker labels; rename speakers as you go.
- **Live notes feed** — insights (key terms, context, decisions, questions)
  stream in as the meeting runs.
- **Summary + action items** on "End Meeting", saved as markdown and exportable.
- **Knowledge vault** — a projects/people/sources wiki synthesized from your
  meetings, with **vault chat** to ask questions across everything.
- **Voice profile + optional speaker registry** — label your own voice as "You";
  opt in to remember named voices across meetings (local, deletable).
- **Auto-stop** — ends the meeting after inactivity or when the window closes.
- **Screen analysis (optional)** — periodic screen frames analyzed for slides,
  code, and docs.
- **Pause/resume** and **image attachments** in chat.
- **Menu-bar tray + global shortcut** (Option+Cmd+M) to show/hide the app.

## Architecture

```
Swift CaptureHelper (ScreenCaptureKit)
  → system audio (0x01), mic (0x04), screen frames (0x02)
        │
  Silero VAD → Whisper (mlx / faster-whisper)      [local]
        │   ECAPA-TDNN speaker embeddings (CPU)     [local]
        │
  MeetingEngine — bleed dedup, analysis, chat, summary, vault, persistence
        │
  LLMService (Bedrock | OpenRouter | Venice | Ollama) [your provider]
        │
  FastAPI WebSocket ─── vanilla-JS frontend (Vite build)
```

All client↔server traffic is a single WebSocket; there are no REST endpoints.
The Electron shell spawns and manages the Python server, health-checks it, and
reaps it on quit.

## Development

The Electron app and the dev server are the same backend + frontend; for
day-to-day development you can skip Electron entirely:

```bash
make dev              # dev server at http://localhost:8000 (config via .env)  [./start.sh]
make test             # run backend tests
make build            # rebuild the frontend bundle

# Raw equivalents (same thing, useful when make is unavailable):
# ./start.sh
# .venv/bin/python -m pytest web/backend/tests/ -q
# cd web/frontend && npm run dev   # Vite dev server (proxies /ws to :8000)
# cd web/frontend && npm run build
```

The browser flow is a development surface — end users should run the desktop
app, which adds onboarding, keychain-backed secrets, the tray, and settings.

The server serves the built bundle in `web/backend/static/`, so rebuild the
frontend after changes. See [`CLAUDE.md`](CLAUDE.md) for a contributor overview
and [`electron/README.md`](electron/README.md) for the desktop app.

![CI](https://img.shields.io/badge/CI-pending-lightgrey) <!-- replace with the repo's CI badge -->

## Contributing

Issues and pull requests are welcome. Start with [`CLAUDE.md`](CLAUDE.md) for the
architecture and conventions.

Tacet is built and maintained by [Muthu Kumar](https://github.com/klmuthu).

## License

[MIT](LICENSE). Third-party fonts, libraries, and runtime-downloaded models are
credited in [NOTICES](NOTICES).
