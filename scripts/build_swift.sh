#!/bin/bash
# Build the Swift CaptureHelper binary.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
SWIFT_DIR="$PROJECT_ROOT/swift/CaptureHelper"
BIN_DIR="$PROJECT_ROOT/bin"

echo "Building CaptureHelper..."
cd "$SWIFT_DIR"
swift build -c release 2>&1

# Copy binary to bin/
mkdir -p "$BIN_DIR"
cp "$(swift build -c release --show-bin-path)/CaptureHelper" "$BIN_DIR/capture-helper"

echo "Built: $BIN_DIR/capture-helper"
