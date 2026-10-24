# Shared base for the ML images: Python 3.11 + ffmpeg + torch (CPU) + fairseq2 + SONAR + Ray.
# Build from the repo root:  docker build -f images/ml-base/Dockerfile .
# Weights are NOT here; ml-speech / ml-text add one model each.

# ---- builder: has a compiler, produces /opt/venv, is thrown away ----
FROM python:3.11-slim-bookworm AS builder
ARG TARGETARCH
# build-essential only because psutil (a fairseq2 dependency) has no aarch64 wheel.
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv
RUN uv venv /opt/venv
COPY images/ml-base/requirements.${TARGETARCH}.txt /tmp/requirements.txt
COPY wheels/ /wheels/
# arm64: fairseq2n is found in /wheels (our build). amd64: it and torch come from the CPU indexes.
RUN if [ "$TARGETARCH" = "amd64" ]; then \
      EXTRA="--index-strategy unsafe-best-match \
             --extra-index-url https://download.pytorch.org/whl/cpu \
             --extra-index-url https://fair.pkg.atmeta.com/fairseq2/whl/pt2.8.0/cpu"; \
    else EXTRA=""; fi; \
    VIRTUAL_ENV=/opt/venv uv pip install --no-cache --find-links /wheels $EXTRA -r /tmp/requirements.txt

# ---- runtime ----
FROM python:3.11-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libsndfile1 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 1000 --create-home --shell /usr/sbin/nologin app
COPY --from=builder /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src
WORKDIR /app
USER 1000
