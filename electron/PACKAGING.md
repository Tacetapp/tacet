# Packaging the desktop app (macOS)

> **Status: scaffold, not yet validated end-to-end.** The pieces below are wired
> but have not produced a verified DMG on a clean machine. Expect to iterate —
> especially on Python-runtime relocatability and signing. There is **no Apple
> Developer ID** in this project, so the app is **unsigned / not notarized**.

## Why this exists

`electron-builder` previously bundled the dev `.venv`, which is **not
relocatable** — a `python -m venv` hardcodes the build machine's interpreter
path, so the server can't start on any other Mac. We replace it with a
self-contained runtime built by `uv`.

## Prerequisites

- [`uv`](https://astral.sh/uv): `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Build on the **same architecture** you ship (native wheels are arch-specific).

## Build

```bash
cd electron
./build-python-runtime.sh        # → ../python-runtime (relocatable, this arch)
npm run dist                     # electron-builder → dist/*.dmg
```

For the other architecture, build on (or cross-build for) that arch and set
`build.mac.target[0].arch` in `package.json` to `["x64"]`.

## What lands where

- The runtime is bundled as `Resources/backend/.venv` (the path `main.js`
  already spawns `uvicorn` from).
- **Models are not bundled.** On first meeting they download into the writable
  userData dir (`HF_HOME`, `MODELS_DIR` are injected there in packaged mode).
- Profile (`profile.md`) and encrypted provider keys (`secrets.json`) also live
  in userData.

## Signing & Gatekeeper (no Developer ID)

`build.mac.identity` is `null` → **unsigned**. Two consequences:

1. **TCC permissions** (mic, screen recording) are keyed to the code signature,
   so an unsigned/ad-hoc app may re-prompt after each rebuild. For stability,
   create a **self-signed certificate** in Keychain Access and set `identity` to
   its name — that gives a stable signature without an Apple account.
2. **Gatekeeper** will warn on first open. Users bypass with right-click → Open,
   or: `xattr -dr com.apple.quarantine "/Applications/Tacet.app"`.

Full **notarization** (clean double-click install, no warning) requires an Apple
Developer ID — deferred until one is available. `entitlements.mac.plist` is
already wired for when signing is enabled.

## Known gaps

- Runtime relocatability for `torch` / `ctranslate2` / `mlx` may need extra work
  (copying the standalone interpreter into the bundle) — see the warning in
  `build-python-runtime.sh`.
- Real first-run model-download **progress UI** isn't built yet (onboarding only
  explains it; the download happens silently on first meeting).
