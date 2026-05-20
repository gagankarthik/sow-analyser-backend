#!/usr/bin/env bash
# Build the shared Lambda layer zip required by Terraform.
# Requires: Docker (for Linux arm64 pip wheels), or Python 3.12 on Linux/Mac arm64.
#
# Output: build/shared-layer.zip  (structure: python/<packages> + python/shared/*.py)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$ROOT/build"
LAYER_DIR="$BUILD_DIR/shared-layer"
PYTHON_DIR="$LAYER_DIR/python"

echo "→ Cleaning build/shared-layer/"
rm -rf "$LAYER_DIR"
mkdir -p "$PYTHON_DIR"

echo "→ Installing pip deps for linux/arm64 (Python 3.12)..."
pip install \
  -r "$ROOT/lambdas/shared/requirements.txt" \
  -t "$PYTHON_DIR" \
  --platform manylinux2014_aarch64 \
  --implementation cp \
  --python-version 3.12 \
  --only-binary=:all: \
  --upgrade \
  --quiet

echo "→ Copying shared/ module..."
cp -r "$ROOT/lambdas/shared/." "$PYTHON_DIR/shared/"
# Remove __pycache__ to keep the zip clean.
find "$PYTHON_DIR" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

echo "→ Zipping → build/shared-layer.zip"
cd "$LAYER_DIR"
zip -r "$BUILD_DIR/shared-layer.zip" python -q

echo "✓ Done: $BUILD_DIR/shared-layer.zip ($(du -sh "$BUILD_DIR/shared-layer.zip" | cut -f1))"
