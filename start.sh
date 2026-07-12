#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

# ── Colours / helpers ────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info() { echo -e "${GREEN}[✓]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }

# Bedrock auth prefers a Bedrock API key (AWS_BEARER_TOKEN_BEDROCK); the AWS
# SigV4 chain (SSO/profile) is only the advanced fallback. Returns 0 (true) when
# the AWS SSO check is actually needed — LLM_PROVIDER is explicitly 'bedrock' AND
# no API key is set. The default provider is openrouter, so an unset/blank
# LLM_PROVIDER (or any non-bedrock provider) skips it.
needs_aws_sso() {
    [[ "${LLM_PROVIDER:-}" == "bedrock" ]] || return 1
    [[ -n "${AWS_BEARER_TOKEN_BEDROCK:-}" ]] && return 1
    return 0
}

info "Tacet dev server — development surface; end users should run the desktop app (cd electron && npm start)"

# ── 0. Load .env ─────────────────────────────────────
# Exposes MEETINGS_DIR and any other user-configured vars to the server.
if [[ -f ".env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

# ── 0b. Transcription backend (chip-dependent) ───────
if [[ "$(uname -m)" == "arm64" ]]; then
    WHISPER_BACKEND_KIND="mlx"
else
    WHISPER_BACKEND_KIND="faster"
fi

# ── 1. Python venv + dependencies ────────────────────
if [[ ! -d ".venv" ]]; then
    warn "Virtual environment not found — creating..."
    PYTHON_CMD=""
    for cmd in python3.13 python3.12 python3.11 python3; do
        if command -v "$cmd" &>/dev/null; then
            PY_VER=$("$cmd" --version 2>&1 | awk '{print $2}')
            PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1-2)
            if printf '%s\n%s' "3.11" "$PY_MAJOR" | sort -V -C; then
                PYTHON_CMD="$cmd"
                break
            fi
        fi
    done
    if [[ -z "$PYTHON_CMD" ]]; then
        err "Python 3.11+ not found. Install it first (brew install python@3.13) or run setup.sh."
        exit 1
    fi
    "$PYTHON_CMD" -m venv .venv
    info "Created venv with $PYTHON_CMD ($PY_VER)"
fi

source .venv/bin/activate

# Install/update deps if requirements.txt is newer than the venv marker
MARKER=".venv/.deps_installed"
if [[ ! -f "$MARKER" || "web/backend/requirements.txt" -nt "$MARKER" ]]; then
    warn "Installing Python dependencies..."
    pip install --quiet --upgrade pip
    pip install --quiet -r web/backend/requirements.txt
    touch "$MARKER"
    info "Python dependencies installed"
fi

# ── 2. Frontend build ────────────────────────────────
if [[ ! -d "web/backend/static" || ! -f "web/backend/static/index.html" ]]; then
    warn "Frontend not built — building..."
    (cd web/frontend && npm install --silent 2>&1 && npm run build 2>&1)
    info "Frontend built → web/backend/static/"
fi

# ── 3. Swift capture binary ──────────────────────────
if [[ ! -f "bin/capture-helper" ]]; then
    warn "CaptureHelper not found — building Swift binary..."
    mkdir -p bin
    (cd swift/CaptureHelper && swift build -c release 2>&1 | tail -3)
    if [[ -f "swift/CaptureHelper/.build/release/CaptureHelper" ]]; then
        cp swift/CaptureHelper/.build/release/CaptureHelper bin/capture-helper
        info "CaptureHelper built → bin/capture-helper"
    else
        err "Swift build failed. Run setup.sh for full diagnostics."
        exit 1
    fi
fi

# ── 4. Whisper model (auto-downloads from HuggingFace on first use) ──
if [[ "$WHISPER_BACKEND_KIND" == "mlx" ]]; then
    MLX_MODEL="${WHISPER_MLX_MODEL:-${WHISPER_MODEL:-mlx-community/whisper-large-v3-turbo}}"
    if ! python -c "
from huggingface_hub import try_to_load_from_cache
result = try_to_load_from_cache('${MLX_MODEL}', 'config.json')
import sys; sys.exit(0 if result is not None else 1)
" 2>/dev/null; then
        warn "Whisper model not cached. Pre-downloading (~1.6GB, one-time)..."
        python -c "from huggingface_hub import snapshot_download; snapshot_download('${MLX_MODEL}')" && \
            info "Whisper model downloaded" || \
            warn "Download failed — will retry on first use"
    else
        info "Whisper model cached: ${MLX_MODEL}"
    fi
else
    FW_MODEL="${WHISPER_FASTER_MODEL:-${WHISPER_MODEL:-distil-small.en}}"
    if ! python -c "
from faster_whisper import download_model
download_model('${FW_MODEL}', local_files_only=True)
" 2>/dev/null; then
        warn "Whisper model not cached. Pre-downloading (~1.6GB, one-time)..."
        python -c "from faster_whisper import download_model; download_model('${FW_MODEL}')" && \
            info "Whisper model downloaded" || \
            warn "Download failed — will retry on first use"
    else
        info "Whisper model cached: ${FW_MODEL}"
    fi
fi

# ── 5. Speaker embedding model (speechbrain ECAPA-TDNN) ──
SPEAKER_MODEL="speechbrain/spkrec-ecapa-voxceleb"
if ! python -c "
from huggingface_hub import try_to_load_from_cache
result = try_to_load_from_cache('${SPEAKER_MODEL}', 'hyperparams.yaml')
import sys; sys.exit(0 if result is not None else 1)
" 2>/dev/null; then
    warn "Speaker embedding model not cached. Pre-downloading (~80MB, one-time)..."
    python -c "from huggingface_hub import snapshot_download; snapshot_download('${SPEAKER_MODEL}')" && \
        info "Speaker model downloaded" || \
        warn "Download failed — will retry on first use"
else
    info "Speaker model cached: ${SPEAKER_MODEL}"
fi

# ── 6. Bedrock credentials (AWS SSO/profile fallback) ──
# Only needed when Bedrock is the active provider AND no Bedrock API key is set.
# AWS_BEARER_TOKEN_BEDROCK or a non-bedrock LLM_PROVIDER skips this entirely.
if needs_aws_sso; then
    SSO_VALID=false
    if [[ -d "$HOME/.aws/sso/cache" ]]; then
        for f in "$HOME"/.aws/sso/cache/*.json; do
            [[ -f "$f" ]] || continue
            if python3 -c "
import json, sys
from datetime import datetime, timezone
data = json.load(open('$f'))
if 'accessToken' not in data: sys.exit(1)
exp = datetime.fromisoformat(data['expiresAt'].replace('Z', '+00:00'))
sys.exit(0 if exp > datetime.now(timezone.utc) else 1)
" 2>/dev/null; then
                SSO_VALID=true
                break
            fi
        done
    fi

    if ! $SSO_VALID; then
        warn "Bedrock credentials missing. The server will start, but LLM calls will fail until you either:"
        warn "  • add AWS_BEARER_TOKEN_BEDROCK=<key> to .env  (recommended — AWS console → Bedrock → API keys)"
        warn "  • or run: aws sso login   (advanced, if you use an AWS SSO profile)"
        warn "  • or choose another provider in .env: LLM_PROVIDER=ollama|openrouter|venice"
    fi
fi

# ── 7. Generate a per-session WebSocket auth token ───
# Any local process could connect to the WS without this; the token ensures
# only a browser that loaded the page (which has the token in the URL) can
# connect (§4.1). Falls back to Python secrets if openssl is absent.
if command -v openssl &>/dev/null; then
    WS_AUTH_TOKEN=$(openssl rand -hex 24)
else
    WS_AUTH_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(24))")
fi
export WS_AUTH_TOKEN

# ── 8. Start ─────────────────────────────────────────
info "Starting server — open this URL in your browser:"
info "  http://localhost:8000/?token=${WS_AUTH_TOKEN}"
# Loopback only — this is a local, single-user app; never expose it on the LAN.
exec uvicorn web.backend.main:app --host 127.0.0.1 --port 8000
