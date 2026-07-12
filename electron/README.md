# Tacet — Desktop (Electron)

A self-contained desktop wrapper around Tacet's FastAPI server. The Electron
main process owns the full lifecycle of the Python backend: it starts the server
on launch, waits for it to become healthy, loads the UI, and kills the server
(and the Swift capture helper it spawns) on quit. All configuration lives in a
native **Settings** page, and a first-run onboarding wizard collects your LLM
provider and key.

## How it runs the server

```
Electron main (main.js)
  ├─ picks a free port (prefers 8000)
  ├─ spawns:  .venv/bin/python -m uvicorn web.backend.main:app --host 127.0.0.1 --port <port>
  │            cwd = repo root,  env = process env + settings + PYTHONPATH
  ├─ health-checks http://127.0.0.1:<port>/ until 200
  ├─ loads that URL in the main window
  └─ on quit: SIGTERM+SIGKILL the server's process group (reaps capture-helper too)
```

The Python process is spawned `detached` (its own process group) so the whole
tree can be reaped reliably. The frontend derives its WebSocket URL from
`window.location`, so the dynamic port "just works"; the backend's CORS/WS
origin checks accept any `localhost`/`127.0.0.1` port.

## Prerequisites (one-time)

From the repo root, build the Python venv, frontend, and Swift capture binary:

```bash
make setup      # runs ./setup.sh
```

This is the same setup the CLI (`start.sh`) uses. The Electron app reuses the
resulting `.venv`, `web/backend/static`, and `bin/capture-helper`.

## Develop / run

```bash
make app        # from the repo root — installs Electron deps if needed, then starts
# equivalent: cd electron && npm install && npm start
```

- **Settings:** `Cmd+,` (also under the app menu). Saving applies the values and
  restarts the server automatically.
- **Restart server:** `Cmd+Shift+R`. **Reload app:** `Cmd+R`.
- **Server log:** View menu → *Open server log* (also a button in Settings).
  Stored at `~/Library/Application Support/Tacet/server.log`.

Settings are persisted to
`~/Library/Application Support/Tacet/config.json` and injected as environment
variables into the server process — there is no `.env` to edit. Provider API
keys are stored encrypted in the OS keychain (`safeStorage`), never in
`config.json`.

## Configuration

Every option maps to an environment variable the backend reads. See
[`config.js`](./config.js) for the full schema — Storage, the **LLM provider**
section (Bedrock, OpenRouter, Venice) plus its Bedrock model settings, LLM cost
& latency, Transcription, Voice profile, Auto-stop, Tuning, and Advanced. Empty
fields fall back to the backend's own defaults.

## Packaging

```bash
npm install      # ensure electron-builder is present
npm run dist
```

`electron-builder` (config in `package.json` → `build`) bundles the Electron
shell plus the backend (`web/`, `bin/`, `prompts/`, and `.venv/`) under
`resources/backend/`. Output goes to `electron/dist/`.

> **Note on bundling `.venv`:** a Python virtualenv is not fully relocatable, so
> the produced app is intended for the machine/architecture it was built on.
> For a portable distributable you would swap the bundled venv for a relocatable
> Python (e.g. PyInstaller or `python-build-standalone`) and code-sign the app
> with microphone/screen-recording entitlements. The dev flow (`npm start`) and
> a same-machine `dir` build are wired and working today.
