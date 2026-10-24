#!/usr/bin/env bash
# Rebuild the committed arm64 wheel. Takes about an hour. Run from the repo root.
set -euo pipefail
docker buildx build --platform linux/arm64 --provenance=false \
  -f wheels/Dockerfile.fairseq2n-arm64 --target out --output type=local,dest=wheels/ .
( cd wheels && shasum -a 256 *.whl > SHA256SUMS && cat SHA256SUMS )
