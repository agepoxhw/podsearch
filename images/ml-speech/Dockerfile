# ml-speech: ml-base + the speech encoder weights baked in + our code.
# Build from the repo root, e.g.:  docker build -f images/ml-speech/Dockerfile --build-arg BASE=ml-base:dev .
ARG BASE=ml-base:dev
FROM ${BASE}
ENV FAIRSEQ2_CACHE_DIR=/models
USER root
RUN install -d -o 1000 -g 1000 /models
USER 1000

# Weights layer FIRST, using only the 3 tiny files it needs, so editing other code never
# invalidates this layer (and never re-downloads gigabytes).
COPY --chown=1000 src/podsearch/__init__.py src/podsearch/models.py src/podsearch/prefetch.py /app/src/podsearch/
RUN python -m podsearch.prefetch speech

COPY --chown=1000 src/ /app/src/
