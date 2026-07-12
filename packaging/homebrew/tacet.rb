# Homebrew cask for Tacet — a local, real-time macOS meeting assistant.
#
# This is a TEMPLATE. It lives in a Homebrew "tap" repo (tacetapp/homebrew-tap)
# under Casks/tacet.rb. See packaging/homebrew/README.md for the publish steps.
#
# The app is unsigned / not notarized (no Apple Developer ID), so Gatekeeper
# quarantines it on first open. The `postflight`/uninstall notes and the
# `no_check` alternative below explain how users get past that.

cask "tacet" do
  # Bump this on every release and fill in the two sha256 values below.
  version "0.1.0"

  # Per-arch DMGs are published to the GitHub Release. `on_arm`/`on_intel`
  # select the right one and its checksum.
  on_arm do
    sha256 "REPLACE_WITH_ARM64_DMG_SHA256"
    url "https://github.com/tacetapp/tacet/releases/download/v#{version}/Tacet-#{version}-arm64.dmg"
  end
  on_intel do
    sha256 "REPLACE_WITH_X64_DMG_SHA256"
    url "https://github.com/tacetapp/tacet/releases/download/v#{version}/Tacet-#{version}-x64.dmg"
  end

  # Alternative to the two sha256 lines above while iterating on pre-releases:
  # replace both `sha256 "..."` lines with a single `sha256 :no_check`. Do NOT
  # ship `:no_check` in a public tap — it disables download verification.

  name "Tacet"
  desc "Local, real-time meeting assistant (transcription, diarization, insights)"
  homepage "https://github.com/tacetapp/tacet"

  # Minimum macOS for ScreenCaptureKit; adjust if you raise the floor.
  depends_on macos: ">= :ventura"

  app "Tacet.app"

  # Unsigned build: strip the quarantine flag so it opens without the
  # "damaged / can't be opened" Gatekeeper block on first launch.
  postflight do
    system_command "/usr/bin/xattr",
                   args: ["-dr", "com.apple.quarantine", "#{appdir}/Tacet.app"],
                   sudo: false
  end

  # Model caches, config, encrypted keys, and the profile live in userData.
  zap trash: [
    "~/Library/Application Support/Tacet",
    "~/Library/Preferences/app.tacet.desktop.plist",
    "~/Library/Saved Application State/app.tacet.desktop.savedState",
  ]
end
