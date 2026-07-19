#!/usr/bin/env bash
# build.sh — build the night-shift agent image and verify enforcement integrity
# inside it. Build context is the repo root (so enforcement/ is available to COPY).
#
# Usage: bash container/build.sh [image-tag]     (default: night-shift-agent:latest)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
IMAGE="${1:-night-shift-agent:latest}"

command -v docker >/dev/null || { echo "docker not found"; exit 1; }

echo ">> building $IMAGE from $ROOT"
docker build -f "$HERE/Dockerfile" -t "$IMAGE" "$ROOT"

echo ">> verifying root-owned /opt integrity inside the image"
docker run --rm --entrypoint bash "$IMAGE" \
  -c 'bash /opt/night-shift/enforcement/verify-integrity.sh'

echo ">> verifying the toolchain is present"
docker run --rm --entrypoint bash "$IMAGE" -c '
  set -e
  echo "  claude:   $(claude --version 2>&1 | head -1)"
  echo "  codex:    $(codex --version 2>&1 | head -1)"
  echo "  gitleaks: $(gitleaks version 2>&1 | head -1)"
  echo "  chromium: $(ls -d /opt/ms-playwright/chromium-* 2>/dev/null | head -1 || echo MISSING)"
  echo "  agent uid: $(id -u node) (node)"
'
echo ">> OK: $IMAGE built + verified"
