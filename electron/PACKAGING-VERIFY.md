# Packaging build — verification checklist

Run top to bottom on the machine/arch you're shipping. Each box is a concrete
pass/fail. Pair with `PACKAGING.md` (the how-to). The unvalidated risk is
concentrated in **Stage 1** (runtime relocatability) and **Stage 4** (server
actually starts from the bundled runtime).

`UD="$HOME/Library/Application Support/Tacet"` — used below.

## Stage 0 — Prereqs
- [ ] `uv --version` succeeds.
- [ ] `uname -m` matches `build.mac.target[0].arch` in `package.json`
      (`arm64` ↔ Apple Silicon, `x86_64` ↔ set arch to `x64`).

## Stage 1 — Relocatable runtime  ⚠️ highest risk
```bash
cd electron && ./build-python-runtime.sh
```
- [ ] Ends with `core imports OK` and `Done. Bundle at: …/python-runtime`.
- [ ] **No** `resolves outside the bundle` warning.
- [ ] Backend imports load (arch-specific whisper):
  ```bash
  ../python-runtime/bin/python -c "import fastapi,uvicorn,httpx,numpy,speechbrain,silero_vad; print('ok')"
  # Apple Silicon: also  -c "import mlx_whisper"   |   Intel: also  -c "import faster_whisper"
  ```
- [ ] **Portability test (the real one):** copy the bundle elsewhere and run it —
      if this fails, the bundle isn't relocatable.
  ```bash
  cp -R ../python-runtime /tmp/rt-test && /tmp/rt-test/bin/python -c "import fastapi; print('relocatable OK')"
  ```

## Stage 2 — Build the DMG
```bash
npm run dist
```
- [ ] Produces `dist/Tacet-<ver>-<arch>.dmg` with no missing-`python-runtime` error.
- [ ] Size is plausible (runtime + Electron; ~0.5–1.5 GB with torch).

## Stage 3 — Install + first launch (onboarding)
- [ ] Mount DMG → drag to /Applications. First open: right-click → **Open**
      (or `xattr -dr com.apple.quarantine "/Applications/Tacet.app"`).
- [ ] **Onboarding window appears** (not the main app).
- [ ] Mic "Allow" → real macOS prompt → status flips to `✓ granted`.
- [ ] Screen "Open Settings" → opens the Screen Recording privacy pane.
- [ ] Provider = OpenRouter → key + model fields appear; enter a key.
- [ ] Fill the "About you" fields → **Finish** → window closes, app loads.
- [ ] On-disk side effects:
  ```bash
  ls "$UD/.onboarded"                 # exists
  cat "$UD/config.json"               # has LLM_PROVIDER (+ model)
  grep -c 'sk-or-' "$UD/secrets.json" # → 0  (key is ENCRYPTED, not plaintext)
  cat "$UD/profile.md"                # your discovery answers
  ```

## Stage 4 — First meeting (download + transcription from the bundled runtime)
- [ ] Start a meeting and speak.
- [ ] `tail -f "$UD/server.log"` shows the server starting from the bundled
      runtime — **no** `Python environment not found` and **no** dylib/symbol errors.
- [ ] Model downloads into **userData**, not the read-only bundle:
  ```bash
  du -sh "$UD/hf-cache" "$UD/models" 2>/dev/null   # these grow on first meeting
  ```
- [ ] Transcript appears.
- [ ] Provider check: vision model → visual insights appear; text-only provider →
      `server.log` shows `Screen analysis off (...)` and no per-frame errors.

## Stage 5 — Relaunch + persistence
- [ ] Quit (⌘Q) + reopen → straight to the main app (**no** onboarding; flag persisted).
- [ ] Provider still works — `server.log` uses your provider, no `no API key configured`
      (confirms `safeStorage` decrypt at spawn).
- [ ] ⌘, → Settings shows the AI-provider section with your values.

## If something fails

| Symptom | Likely cause → fix |
|---|---|
| Server won't start, `Python environment not found` | runtime not at `backend/.venv` — rerun `build-python-runtime.sh`, confirm `extraResources` bundled it |
| Server starts then dies; dylib/symbol/`@rpath` errors | native-wheel relocatability — copy the standalone interpreter into the bundle; try a different `PYTHON_VERSION`; rebuild |
| "App is damaged / can't be opened" | quarantine — `xattr -dr com.apple.quarantine …` |
| Mic/screen re-prompts every relaunch | unsigned ad-hoc signature — create a self-signed cert (Keychain Access) and set `build.mac.identity` to its name |
| Key visible in `secrets.json` | `safeStorage` unavailable — should be `{enc:…}` not `{plain:…}`; check the OS keychain is unlocked |
| Model download stalls/fails | `HF_HOME`/`MODELS_DIR` not writable — verify userData perms, or pre-warm with `HF_HOME=$UD/hf-cache python -c "import huggingface_hub; …"` |
