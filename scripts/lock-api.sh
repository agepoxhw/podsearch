#!/usr/bin/env bash
# Regenerate images/search-api/requirements.{amd64,arm64}.txt from requirements.in (pins every transitive package).
# Takes the uv binary from the same pinned image the Dockerfile uses, so the host needs only Docker.
set -euo pipefail
cd "$(dirname "$0")/.."
UV_IMAGE=ghcr.io/astral-sh/uv:0.13.5
docker rm -f uv-lock-tmp >/dev/null 2>&1 || true
docker create --name uv-lock-tmp "$UV_IMAGE" x >/dev/null
mkdir -p .build && docker cp uv-lock-tmp:/uv .build/uv && docker rm uv-lock-tmp >/dev/null
docker run --rm -v "$PWD:/w" -v "$PWD/.build/uv:/uv:ro" -w /w python:3.11-slim-bookworm sh -c '
  for pair in amd64:x86_64-unknown-linux-gnu arm64:aarch64-unknown-linux-gnu; do
    /uv pip compile --python-version 3.11 --python-platform "${pair#*:}" --no-header \
      images/search-api/requirements.in -o "images/search-api/requirements.${pair%%:*}.txt"
  done'
