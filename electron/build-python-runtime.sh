#!/usr/bin/env bash
# Build a RELOCATABLE, SELF-CONTAINED Python runtime for bundling in the
# packaged desktop app.
#
# The packaged app can't rely on a `python -m venv` (its pyvenv.cfg hardcodes the
# build machine's interpreter path → dangling on any other Mac). A plain
# `uv venv --relocatable` is also insufficient: the venv's `bin/python` is just a
# symlink to uv's shared managed interpreter (~/.local/share/uv/python/…), and the
# stdlib lives there too — so the bundle breaks the moment it lands on a machine
# without that cache (i.e. every user's Mac).
#
# Instead we copy uv's standalone (python-build-standalone) CPython distribution
# *wholesale* into the bundle — a real interpreter + full stdlib with no external
# symlinks — and install the backend deps into its own site-packages. The result
# is fully self-contained and movable. electron-builder bundles it as
# Resources/backend/.venv (the path electron/main.js spawns `uvicorn` from).
#
# Requires: uv  (https://astral.sh/uv).  Run this BEFORE `npm run dist`, on the
# SAME architecture you're shipping (the native wheels are arch-specific).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
DEST="$REPO/python-runtime"
PYVER="${PYTHON_VERSION:-3.12}"

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv not found. Install with:"
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
fi

echo "==> Ensuring a managed standalone CPython $PYVER is available"
uv python install "$PYVER"

# Locate the managed standalone interpreter (python-build-standalone). Resolve
# from a neutral dir with VIRTUAL_ENV cleared so uv doesn't pick up a project
# venv in the CWD.
BASE_PY="$(cd /tmp && env -u VIRTUAL_ENV uv python find --python-preference only-managed "$PYVER")"
# uv exposes a version-ALIAS install (…/cpython-3.12-… ) whose bin/lib are
# symlinks into the real patch-versioned install (…/cpython-3.12.13-… ). Copying
# the alias would bundle dangling absolute symlinks, so resolve the interpreter's
# realpath and take the REAL install root (two levels up from bin/python3.X).
BASE_PY="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$BASE_PY")"
BASE_ROOT="$(cd "$(dirname "$BASE_PY")/.." && pwd)"
echo "==> Managed standalone root: $BASE_ROOT"

if [ ! -x "$BASE_ROOT/bin/python3" ] && [ ! -x "$BASE_ROOT"/bin/python3.* ]; then
  echo "error: could not locate a standalone interpreter under $BASE_ROOT" >&2
  exit 1
fi

echo "==> Copying standalone distribution into the bundle: $DEST"
rm -rf "$DEST"
# -R follows the tree; the internal python/python3 → python3.X links are relative
# (they stay inside the bundle). Make everything writable so uv can install into it.
cp -R "$BASE_ROOT" "$DEST"
chmod -R u+w "$DEST"

# python-build-standalone ships an EXTERNALLY-MANAGED marker (PEP 668). Remove it
# so we can install the backend deps directly into the bundle's site-packages.
find "$DEST/lib" -maxdepth 2 -name EXTERNALLY-MANAGED -delete 2>/dev/null || true

echo "==> Installing backend dependencies into the bundle"
# $DEST/bin/python3 is a relative symlink → the real python3.X copied above, so
# uv computes sys.prefix = $DEST and installs into the bundle's own site-packages.
uv pip install --python "$DEST/bin/python3" -r "$REPO/web/backend/requirements.txt"

echo "==> Smoke-checking the runtime"
"$DEST/bin/python3" - <<'PY'
import sys
print("runtime python:", sys.version.split()[0])
for mod in ("fastapi", "uvicorn", "numpy", "httpx"):
    __import__(mod)
print("core imports OK")
PY

# Relocatability guard: every python entrypoint must be a real file or a symlink
# that resolves *inside* the bundle. A link (or interpreter) pointing outside
# means the bundle isn't self-contained and will break on other Macs.
echo "==> Verifying self-containment (no interpreter references outside the bundle)"
bad=0
for name in python python3 "python${PYVER}"; do
  p="$DEST/bin/$name"
  [ -e "$p" ] || continue
  real="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$p")"
  case "$real" in
    "$DEST"/*) : ;;                       # inside the bundle — good
    *) echo "  ✗ $name resolves outside the bundle: $real"; bad=1 ;;
  esac
done
if [ "$bad" -ne 0 ]; then
  echo "error: runtime is NOT self-contained — see the paths above." >&2
  exit 1
fi
echo "  ✓ interpreter and symlinks resolve inside the bundle"

echo "==> Done. Bundle at: $DEST"
echo "    Portability check (run on a different Mac or after moving the bundle):"
echo "      cp -R \"$DEST\" /tmp/rt-test && /tmp/rt-test/bin/python -c 'import fastapi; print(\"relocatable OK\")'"

# Arch guard: the bundled runtime MUST match the host arch (cross-arch site-
# packages silently break MLX/torch). Per-arch DMGs are built on native runners.
RT_ARCH=$(file "$DEST/bin/python" | grep -oE 'x86_64|arm64')
HOST_ARCH=$(uname -m); [[ "$HOST_ARCH" == "arm64" ]] || HOST_ARCH="x86_64"
if [[ "$RT_ARCH" != "$HOST_ARCH" ]]; then
  echo "error: runtime arch ($RT_ARCH) != host arch ($HOST_ARCH)"; exit 1
fi
echo "runtime arch OK: $RT_ARCH"
