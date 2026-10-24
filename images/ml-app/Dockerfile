# ml-app: ml-base + our code, no weights. Runs the Ray head (job driver) and the controller.
# The model images repeat the same COPY on top of their weights layer, so every pod has the same code.
ARG BASE=ml-base:dev
FROM ${BASE}
COPY --chown=1000 src/ /app/src/
