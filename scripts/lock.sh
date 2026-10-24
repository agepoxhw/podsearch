#!/usr/bin/env bash
# Regenerate the per-architecture lock files for the ML images.
# Run when images/ml-base/requirements.in changes; commit the results.
set -euo pipefail
cd "$(dirname "$0")/.."
D=images/ml-base
COMMON=(--python-version 3.11 --no-header)

# arm64: PyPI torch is already CPU-only; fairseq2n comes from our committed wheel (wheels/).
uv pip compile $D/requirements.in "${COMMON[@]}" \
  --python-platform aarch64-manylinux_2_28 --find-links wheels \
  -o $D/requirements.arm64.txt

# amd64: CPU-only torch and fairseq2n come from the PyTorch and Meta CPU indexes (PyPI's are CUDA).
uv pip compile $D/requirements.in "${COMMON[@]}" \
  --python-platform x86_64-manylinux_2_28 --index-strategy unsafe-best-match \
  --extra-index-url https://download.pytorch.org/whl/cpu \
  --extra-index-url https://fair.pkg.atmeta.com/fairseq2/whl/pt2.8.0/cpu \
  -o $D/requirements.amd64.txt
