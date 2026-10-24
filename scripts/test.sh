#!/usr/bin/env bash
# Run the unit tests inside the ml-base image, with the repo mounted read-only.
# Builds the image first if the host's architecture has none yet (Docker layer cache makes this cheap).
set -euo pipefail
cd "$(dirname "$0")/.."
IMAGE=${ML_BASE_IMAGE:-ml-base:dev}
case "$(uname -m)" in arm64|aarch64) PLATFORM=linux/arm64 ;; *) PLATFORM=linux/amd64 ;; esac
docker buildx build --platform "$PLATFORM" -f images/ml-base/Dockerfile -t "$IMAGE" --load --progress=quiet . >/dev/null
docker run --rm --platform "$PLATFORM" -v "$PWD":/app:ro -w /app -e PYTHONDONTWRITEBYTECODE=1 \
  "$IMAGE" python -m pytest -q -p no:cacheprovider "$@"
