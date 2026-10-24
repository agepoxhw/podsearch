# search-api: python-slim + the API's pure-Python dependencies + our code. No torch, no models, no ffmpeg:
# the query embedding happens in the separate `embedder` service (ml-text image).
# Build from the repo root:  docker build -f images/search-api/Dockerfile .
FROM python:3.11-slim-bookworm AS builder
ARG TARGETARCH
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv
RUN uv venv /opt/venv
COPY images/search-api/requirements.${TARGETARCH}.txt /tmp/requirements.txt
RUN VIRTUAL_ENV=/opt/venv uv pip install --no-cache -r /tmp/requirements.txt

# Test image (scripts/test.sh builds it with --target test): the runtime venv plus pytest and moto, pinned to the
# same versions as ml-base's lock (-c) so tests run against the libraries we ship.
FROM builder AS test-venv
COPY images/ml-base/requirements.${TARGETARCH}.txt /tmp/ml-base-lock.txt
RUN VIRTUAL_ENV=/opt/venv uv pip install --no-cache -c /tmp/ml-base-lock.txt pytest "moto[s3]"
FROM python:3.11-slim-bookworm AS test
COPY --from=test-venv /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
WORKDIR /app

# Runtime image (the default target: the last stage).
FROM python:3.11-slim-bookworm
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin app
COPY --from=builder /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
WORKDIR /app
COPY --chown=1000 src/ /app/src/
USER 1000
