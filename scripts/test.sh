#!/usr/bin/env bash
# Run the unit tests with the repo mounted read-only, in two images:
#   ml-base     everything except the API tests (same libs as the ML pods)
#   search-api  tests/test_api.py (only this image has fastapi); built from its own `test` stage
# Builds the images first if the host has none yet (Docker layer cache makes this cheap).
set -euo pipefail
cd "$(dirname "$0")/.."
IMAGE=${ML_BASE_IMAGE:-ml-base:dev}
case "$(uname -m)" in arm64|aarch64) PLATFORM=linux/arm64 ;; *) PLATFORM=linux/amd64 ;; esac
run() {  # run <image> <pytest args...>
  local image=$1; shift
  docker run --rm --platform "$PLATFORM" -v "$PWD":/app:ro -w /app -e PYTHONDONTWRITEBYTECODE=1 \
    "$image" python -m pytest -q -p no:cacheprovider "$@"
}
docker buildx build --platform "$PLATFORM" -f images/ml-base/Dockerfile -t "$IMAGE" --load --progress=quiet . >/dev/null
docker buildx build --platform "$PLATFORM" -f images/search-api/Dockerfile --target test -t search-api-test:dev --load --progress=quiet . >/dev/null
if [ $# -gt 0 ]; then run "$IMAGE" "$@"; exit; fi   # explicit args: run them in ml-base
run "$IMAGE" --ignore=tests/test_api.py
run search-api-test:dev tests/test_api.py
