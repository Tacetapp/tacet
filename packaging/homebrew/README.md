# Publishing Tacet as a Homebrew cask

`tacet.rb` is a ready-to-use cask template. Homebrew installs casks from a
**tap** (a Git repo named `homebrew-<name>`), so Tacet is distributed from
`tacetapp/homebrew-tap`, not from this app repo. Once published, users install
with:

```bash
brew install --cask tacetapp/tap/tacet
```

## One-time: create the tap

1. **Create the tap repo.** On GitHub, create a public repo named
   **`tacetapp/homebrew-tap`** (the `homebrew-` prefix is required; the tap is
   then referenced as `tacetapp/tap`).

2. **Add a `Casks/` directory and drop the cask in it.** Copy this template:

   ```bash
   git clone https://github.com/tacetapp/homebrew-tap
   mkdir -p homebrew-tap/Casks
   cp packaging/homebrew/tacet.rb homebrew-tap/Casks/tacet.rb
   ```

## Each release

3. **Cut the release.** Push a `v*` tag on `tacetapp/tacet`; the
   `.github/workflows/release.yml` workflow builds and uploads
   `Tacet-<version>-arm64.dmg` and `Tacet-<version>-x64.dmg` to the GitHub
   Release.

4. **Fill in the version + sha256s.** In `Casks/tacet.rb`, set `version` to the
   released version and compute both checksums from the release assets:

   ```bash
   V=0.1.0
   for A in arm64 x64; do
     URL="https://github.com/tacetapp/tacet/releases/download/v$V/Tacet-$V-$A.dmg"
     echo "$A: $(curl -sL "$URL" | shasum -a 256 | awk '{print $1}')"
   done
   ```

   Paste the `arm64` digest into the `on_arm` block and the `x64` digest into
   the `on_intel` block. (While iterating on pre-releases you may temporarily use
   `sha256 :no_check` instead — never ship that in the public tap.)

5. **Commit and push the tap.** Then verify end-to-end:

   ```bash
   brew update
   brew install --cask tacetapp/tap/tacet   # or: brew reinstall --cask ...
   brew audit --cask --new tacetapp/tap/tacet   # lint before pushing, optional
   ```

## Notes

- The app is **unsigned / not notarized** (no Apple Developer ID). The cask's
  `postflight` runs `xattr -dr com.apple.quarantine` so it opens without the
  "damaged" Gatekeeper error. Once a Developer ID + notarization are in place,
  that workaround can be dropped.
- Native ML wheels are architecture-specific, so each DMG is built on its own
  runner (arm64 on `macos-14`, x64 on `macos-13`). Keep both `url`/`sha256`
  pairs in sync with the release assets.
