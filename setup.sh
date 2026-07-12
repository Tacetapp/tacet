#!/usr/bin/env bash
set -euo pipefail

# ── Config ───────────────────────────────────────────
PYTHON_MIN="3.11"
NODE_MIN="18"
SWIFT_MIN="5.9"
REPO_URL="https://github.com/tacetapp/tacet.git"  # update if the repo moves

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
err()   { echo -e "${RED}[✗]${NC} $*"; }
step()  { echo -e "\n${CYAN}${BOLD}──── $* ────${NC}"; }

MANUAL_STEPS=()

# ── Helpers ──────────────────────────────────────────
version_gte() {
    # Returns 0 if $1 >= $2
    printf '%s\n%s' "$2" "$1" | sort -V -C
}

command_exists() { command -v "$1" &>/dev/null; }

# Bedrock auth prefers a Bedrock API key (AWS_BEARER_TOKEN_BEDROCK); the AWS
# SigV4 chain (SSO/profile) is only the advanced fallback. Returns 0 (true) when
# the AWS CLI / SSO checks are actually needed — i.e. LLM_PROVIDER is explicitly
# 'bedrock' AND no API key is set. The default provider is openrouter, so an
# unset/blank LLM_PROVIDER (or any non-bedrock provider) skips all AWS setup.
needs_aws_sso() {
    [[ "${LLM_PROVIDER:-}" == "bedrock" ]] || return 1
    [[ -n "${AWS_BEARER_TOKEN_BEDROCK:-}" ]] && return 1
    return 0
}

check_macos_version() {
    local macos_ver
    macos_ver=$(sw_vers -productVersion | cut -d. -f1)
    if [[ "$macos_ver" -lt 13 ]]; then
        err "macOS 13 (Ventura) or later is required for ScreenCaptureKit."
        err "Current version: $(sw_vers -productVersion)"
        exit 1
    fi
    info "macOS $(sw_vers -productVersion) — meets minimum (13+)"
}

# ── 1. Platform check ───────────────────────────────
step "Checking platform"
if [[ "$(uname)" != "Darwin" ]]; then
    err "This setup script is for macOS only."
    err "ScreenCaptureKit requires macOS 13+."
    exit 1
fi
check_macos_version

# Detect CPU architecture → transcription backend
ARCH="$(uname -m)"
if [[ "$ARCH" == "arm64" ]]; then
    WHISPER_BACKEND="mlx-whisper"
    info "Apple Silicon detected — transcription will use mlx-whisper (Metal GPU)"
else
    WHISPER_BACKEND="faster-whisper"
    info "Intel CPU detected — transcription will use faster-whisper (CPU)"
fi

# ── 2. Xcode Command Line Tools (includes Swift) ────
step "Checking Xcode Command Line Tools"
if xcode-select -p &>/dev/null; then
    info "Xcode CLI tools found at $(xcode-select -p)"
else
    warn "Installing Xcode Command Line Tools..."
    echo "  A system dialog may appear — click 'Install' and wait for it to finish."
    xcode-select --install 2>/dev/null || true
    # Wait for installation to complete
    echo "  Waiting for installation to complete..."
    until xcode-select -p &>/dev/null; do
        sleep 5
    done
    info "Xcode CLI tools installed"
fi

# Verify Swift version
if command_exists swift; then
    SWIFT_VER=$(swift --version 2>&1 | head -1 | sed -E 's/.*version ([0-9]+\.[0-9]+).*/\1/')
    if version_gte "$SWIFT_VER" "$SWIFT_MIN"; then
        info "Swift ${SWIFT_VER} — meets minimum (${SWIFT_MIN}+)"
    else
        err "Swift ${SWIFT_VER} is too old. Need ${SWIFT_MIN}+. Update Xcode."
        exit 1
    fi
else
    err "Swift not found after installing Xcode CLI tools."
    err "Install Xcode from the App Store and try again."
    exit 1
fi

# ── 3. Homebrew ──────────────────────────────────────
step "Checking Homebrew"
if command_exists brew; then
    info "Homebrew found"
else
    warn "Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Add to path for Apple Silicon
    if [[ -f /opt/homebrew/bin/brew ]]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    fi
    info "Homebrew installed"
fi

# ── 4. Python ────────────────────────────────────────
step "Checking Python"
PYTHON_CMD=""
for cmd in python3.13 python3.12 python3.11 python3; do
    if command_exists "$cmd"; then
        PY_VER=$("$cmd" --version 2>&1 | awk '{print $2}')
        if version_gte "$PY_VER" "$PYTHON_MIN"; then
            PYTHON_CMD="$cmd"
            break
        fi
    fi
done

if [[ -n "$PYTHON_CMD" ]]; then
    info "Python found: $PYTHON_CMD ($PY_VER)"
else
    warn "Installing Python via Homebrew..."
    brew install python@3.13
    PYTHON_CMD="python3.13"
    info "Python installed"
fi

# ── 5. Node.js ───────────────────────────────────────
step "Checking Node.js"
if command_exists node; then
    NODE_VER=$(node --version | sed 's/^v//')
    if version_gte "$NODE_VER" "$NODE_MIN"; then
        info "Node.js found: v${NODE_VER}"
    else
        warn "Node.js ${NODE_VER} is too old (need ${NODE_MIN}+), upgrading..."
        brew install node
        info "Node.js installed"
    fi
else
    warn "Installing Node.js..."
    brew install node
    info "Node.js installed"
fi

# ── 6. AWS CLI (only for the Bedrock SSO/profile fallback) ──
if needs_aws_sso; then
    step "Checking AWS CLI"
    if command_exists aws; then
        AWS_VER=$(aws --version 2>&1 | awk '{print $1}' | cut -d/ -f2)
        info "AWS CLI found: v${AWS_VER}"
    else
        warn "Installing AWS CLI..."
        brew install awscli
        info "AWS CLI installed"
    fi
fi

# ── 7. Repository ────────────────────────────────────
step "Setting up repository"
if [[ -f "pyproject.toml" && -d "web/backend" ]]; then
    REPO_DIR="$(pwd)"
    info "Already in repo directory: ${REPO_DIR}"
else
    REPO_DIR="$(pwd)/meetingassistant"
    if [[ -d "$REPO_DIR/.git" ]]; then
        info "Repo already cloned at ${REPO_DIR}"
    else
        warn "Cloning repository..."
        git clone "$REPO_URL" "$REPO_DIR"
        info "Cloned to ${REPO_DIR}"
    fi
    cd "$REPO_DIR"
fi

# ── 7b. Meetings directory ───────────────────────────
step "Configuring meetings directory"

ENV_FILE="$REPO_DIR/.env"
EXISTING_MEETINGS_DIR=""
if [[ -f "$ENV_FILE" ]]; then
    EXISTING_MEETINGS_DIR=$(grep -E '^MEETINGS_DIR=' "$ENV_FILE" | tail -1 | cut -d= -f2- | sed 's/^"\(.*\)"$/\1/')
fi

DEFAULT_MEETINGS_DIR="${EXISTING_MEETINGS_DIR:-$HOME/tacet-meetings}"
echo ""
echo "  Saved meeting markdown files will be written to this directory."
echo "  Press enter to accept the default, or type a different path."
echo ""
read -rp "  Meetings directory [${DEFAULT_MEETINGS_DIR}]: " MEETINGS_DIR_INPUT
MEETINGS_DIR="${MEETINGS_DIR_INPUT:-$DEFAULT_MEETINGS_DIR}"
# Expand ~ manually since shell expansion doesn't apply to read variables
MEETINGS_DIR="${MEETINGS_DIR/#\~/$HOME}"

mkdir -p "$MEETINGS_DIR"
info "Meetings directory: ${MEETINGS_DIR}"

# Persist to .env so start.sh picks it up on every launch.
touch "$ENV_FILE"
if grep -qE '^MEETINGS_DIR=' "$ENV_FILE"; then
    # Replace existing line (portable sed -i for macOS)
    sed -i.bak "s|^MEETINGS_DIR=.*|MEETINGS_DIR=\"${MEETINGS_DIR}\"|" "$ENV_FILE"
    rm -f "${ENV_FILE}.bak"
else
    echo "MEETINGS_DIR=\"${MEETINGS_DIR}\"" >> "$ENV_FILE"
fi
info "Persisted MEETINGS_DIR to .env"

# One-time migration: move any sessions from the legacy repo-local path.
LEGACY_MEETINGS_DIR="${REPO_DIR}/meetings"
if [[ -d "$LEGACY_MEETINGS_DIR" && \
      "$(cd "$LEGACY_MEETINGS_DIR" && pwd -P)" != "$(cd "$MEETINGS_DIR" && pwd -P)" ]]; then
    legacy_count=$(find "$LEGACY_MEETINGS_DIR" -maxdepth 1 -name "*.md" | wc -l | tr -d ' ')
    if [[ "$legacy_count" -gt 0 ]]; then
        warn "Found ${legacy_count} existing meeting file(s) in the legacy location."
        read -rp "  Move them to ${MEETINGS_DIR}? [Y/n] " migrate_answer
        if [[ -z "$migrate_answer" || "$migrate_answer" = "y" || "$migrate_answer" = "Y" ]]; then
            moved=0
            skipped=0
            for f in "$LEGACY_MEETINGS_DIR"/*.md; do
                [[ -f "$f" ]] || continue
                target="${MEETINGS_DIR}/$(basename "$f")"
                if [[ -e "$target" ]]; then
                    skipped=$((skipped + 1))
                    continue
                fi
                mv "$f" "$target"
                moved=$((moved + 1))
            done
            info "Moved ${moved} meeting file(s); skipped ${skipped} already-present file(s)"
            # Remove the legacy dir if it's empty now.
            rmdir "$LEGACY_MEETINGS_DIR" 2>/dev/null && info "Removed empty legacy meetings/ directory" || true
        else
            info "Skipping migration — legacy files stay in ${LEGACY_MEETINGS_DIR}"
        fi
    fi
fi

# ── 8. Python venv & dependencies ────────────────────
step "Setting up Python environment"
if [[ ! -d ".venv" ]]; then
    "$PYTHON_CMD" -m venv .venv
    info "Created virtual environment"
else
    info "Virtual environment exists"
fi

source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r web/backend/requirements.txt
info "Python dependencies installed"

# Verify critical imports — the transcription backend depends on the chip
if [[ "$WHISPER_BACKEND" == "mlx-whisper" ]]; then
    if python -c "import mlx_whisper" 2>/dev/null; then
        info "mlx-whisper available"
    else
        err "mlx-whisper failed to import. Requires Apple Silicon Mac with macOS 13+."
        MANUAL_STEPS+=("Install mlx-whisper: pip install mlx-whisper")
    fi
else
    if python -c "import faster_whisper" 2>/dev/null; then
        info "faster-whisper available"
    else
        err "faster-whisper failed to import."
        MANUAL_STEPS+=("Install faster-whisper: pip install faster-whisper")
    fi
fi

if python -c "import silero_vad" 2>/dev/null; then
    info "silero-vad available"
else
    err "silero-vad failed to import."
    MANUAL_STEPS+=("Install silero-vad: pip install silero-vad")
fi

if python -c "import speechbrain" 2>/dev/null; then
    info "speechbrain available (speaker diarization)"
else
    err "speechbrain failed to import."
    MANUAL_STEPS+=("Install speechbrain: pip install speechbrain")
fi

# ── 9. Build Swift CaptureHelper ─────────────────────
step "Building Swift CaptureHelper"
CAPTURE_BINARY="bin/capture-helper"
if [[ -f "$CAPTURE_BINARY" ]]; then
    info "CaptureHelper binary already exists at ${CAPTURE_BINARY}"
    warn "Rebuilding to ensure it's up to date..."
fi

mkdir -p bin
echo "  Compiling Swift package (this may take a minute on first build)..."
cd swift/CaptureHelper
swift build -c release 2>&1 | tail -5
BUILD_OUTPUT=".build/release/CaptureHelper"
if [[ -f "$BUILD_OUTPUT" ]]; then
    cp "$BUILD_OUTPUT" "../../bin/capture-helper"
    cd ../..
    info "CaptureHelper built → ${CAPTURE_BINARY}"
else
    cd ../..
    err "Swift build failed. Check the output above."
    err "Make sure Xcode Command Line Tools are installed: xcode-select --install"
    exit 1
fi

# ── 10. Pre-download Whisper model ───────────────────
step "Checking Whisper model"

if [[ "$WHISPER_BACKEND" == "mlx-whisper" ]]; then
    MLX_MODEL="${WHISPER_MLX_MODEL:-${WHISPER_MODEL:-mlx-community/whisper-large-v3-turbo}}"
    # Check if model is already cached by HuggingFace
    if python -c "
from huggingface_hub import try_to_load_from_cache
result = try_to_load_from_cache('${MLX_MODEL}', 'config.json')
import sys; sys.exit(0 if result is not None else 1)
" 2>/dev/null; then
        info "Whisper model cached: ${MLX_MODEL}"
    else
        warn "Pre-downloading Whisper model from HuggingFace (~1.6GB, one-time)..."
        if python -c "
from huggingface_hub import snapshot_download
snapshot_download('${MLX_MODEL}')
print('Download complete')
"; then
            info "Whisper model downloaded: ${MLX_MODEL}"
        else
            warn "Model download failed — it will download automatically on first use."
        fi
    fi
else
    FW_MODEL="${WHISPER_FASTER_MODEL:-${WHISPER_MODEL:-distil-small.en}}"
    # faster-whisper stores CTranslate2 weights in the HuggingFace cache.
    if python -c "
from faster_whisper import download_model
download_model('${FW_MODEL}', local_files_only=True)
" 2>/dev/null; then
        info "faster-whisper model cached: ${FW_MODEL}"
    else
        warn "Pre-downloading faster-whisper model '${FW_MODEL}' from HuggingFace (one-time)..."
        if python -c "
from faster_whisper import download_model
download_model('${FW_MODEL}')
print('Download complete')
"; then
            info "faster-whisper model downloaded: ${FW_MODEL}"
        else
            warn "Model download failed — it will download automatically on first use."
        fi
    fi
fi

# ── 11. Pre-download speaker embedding model ─────────
step "Checking speaker embedding model"
SPEAKER_MODEL="speechbrain/spkrec-ecapa-voxceleb"

if python -c "
from huggingface_hub import try_to_load_from_cache
result = try_to_load_from_cache('${SPEAKER_MODEL}', 'hyperparams.yaml')
import sys; sys.exit(0 if result is not None else 1)
" 2>/dev/null; then
    info "Speaker embedding model cached: ${SPEAKER_MODEL}"
else
    warn "Pre-downloading speaker embedding model (~80MB, one-time)..."
    if python -c "
from huggingface_hub import snapshot_download
snapshot_download('${SPEAKER_MODEL}')
print('Download complete')
"; then
        info "Speaker embedding model downloaded: ${SPEAKER_MODEL}"
    else
        warn "Download failed — it will download automatically on first use."
    fi
fi

# ── 12. Frontend build ───────────────────────────────
step "Building frontend"
cd web/frontend
npm install --silent 2>&1
npm run build 2>&1
cd ../..
info "Frontend built → web/backend/static/"

# ── 13. Bedrock credentials ──────────────────────────
# Bedrock prefers a Bedrock API key (set AWS_BEARER_TOKEN_BEDROCK, or configure
# it in the desktop app's Settings). We only fall back to the AWS SigV4 chain
# (SSO/profile) when no API key is set and Bedrock is the active provider.
SSO_VALID=true   # "true" means no interactive login is required
if needs_aws_sso; then
    step "Checking AWS configuration (Bedrock SSO/profile fallback)"

    # Check for any AWS config
    if [[ -f "$HOME/.aws/config" ]]; then
        info "AWS config found at ~/.aws/config"
    else
        warn "No AWS config found at ~/.aws/config"
        MANUAL_STEPS+=("Configure Bedrock auth: add AWS_BEARER_TOKEN_BEDROCK=<key> to .env (recommended; AWS console → Bedrock → API keys), or 'aws configure sso' for the advanced fallback")
    fi

    # Check for active SSO session
    SSO_VALID=false
    if [[ -d "$HOME/.aws/sso/cache" ]]; then
        for f in "$HOME"/.aws/sso/cache/*.json; do
            [[ -f "$f" ]] || continue
            if python -c "
import json, sys
from datetime import datetime, timezone
data = json.load(open('$f'))
if 'accessToken' not in data:
    sys.exit(1)
exp = datetime.fromisoformat(data['expiresAt'].replace('Z', '+00:00'))
if exp > datetime.now(timezone.utc):
    sys.exit(0)
sys.exit(1)
" 2>/dev/null; then
                SSO_VALID=true
                break
            fi
        done
    fi

    if $SSO_VALID; then
        info "Active AWS SSO session found"
    else
        warn "No active AWS SSO session (SSO/profile is the advanced fallback — a Bedrock API key skips it)."
        MANUAL_STEPS+=("Set Bedrock auth: AWS_BEARER_TOKEN_BEDROCK=<key> in .env (web server) or Settings → Bedrock API key (desktop app); or 'aws sso login' if you use the SSO fallback")
    fi
fi

# ── Done ─────────────────────────────────────────────
step "Setup complete!"

echo ""
echo "┌──────────────────────────────────────────────────────────────────┐"
echo "│                      Quick Start                                │"
echo "├──────────────────────────────────────────────────────────────────┤"
echo "│                                                                  │"
echo "│  source .venv/bin/activate                                       │"
echo "│  uvicorn web.backend.main:app --reload --port 8000               │"
echo "│                                                                  │"
echo "│  Then open http://localhost:8000                                  │"
echo "│                                                                  │"
echo "├──────────────────────────────────────────────────────────────────┤"
echo "│                   Environment Variables                          │"
echo "├──────────────────────────────────────────────────────────────────┤"
echo "│                                                                  │"
echo "│  MEETINGS_DIR         Where to save meeting .md files (in .env)   │"
echo "│  WHISPER_BACKEND      Force 'mlx' or 'faster-whisper' (auto)     │"
echo "│  WHISPER_MODEL        Model for whichever backend runs            │"
echo "│  WHISPER_MLX_MODEL    Model for mlx only (default large-v3-turbo) │"
echo "│  WHISPER_FASTER_MODEL Model for CPU only (default distil-small.en)│"
echo "│  WHISPER_CPU_THREADS  faster-whisper CPU threads (0 = auto)       │"
echo "│  AWS_BEARER_TOKEN_BEDROCK  Bedrock API key (recommended, in .env) │"
echo "│  AWS_REGION           AWS region for Bedrock                      │"
echo "│  BEDROCK_MODEL_ID     Analysis model id (default opus-4-8)        │"
echo "│                                                                  │"
echo "└──────────────────────────────────────────────────────────────────┘"

if [[ ${#MANUAL_STEPS[@]} -gt 0 ]]; then
    echo ""
    echo -e "${YELLOW}${BOLD}Action items before first run:${NC}"
    echo ""
    for i in "${!MANUAL_STEPS[@]}"; do
        echo -e "  ${YELLOW}$((i+1)).${NC} ${MANUAL_STEPS[$i]}"
    done
    echo ""
fi

echo -e "  ${BOLD}Note:${NC} macOS will prompt for Screen Recording and Microphone"
echo -e "  permissions on first meeting start. Click Allow when asked."
echo ""

read -rp "Start the server now? [y/N] " answer
if [[ "$answer" = "y" || "$answer" = "Y" ]]; then
    if ! $SSO_VALID; then
        echo ""
        warn "AWS SSO session not active (Bedrock profile/SSO fallback). Logging in first..."
        aws sso login
    fi
    source .venv/bin/activate
    uvicorn web.backend.main:app --reload --host 0.0.0.0 --port 8000
fi
