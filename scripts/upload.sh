#!/usr/bin/env bash
# Upload local audio files to s3://podcasts/incoming/ through the in-cluster toolbox pod.
# Usage: scripts/upload.sh FILE [FILE...]
# Each file is streamed over `kubectl exec -i` straight into `aws s3 cp -`, so the host
# needs no aws-cli and the pod needs no scratch disk.
set -euo pipefail
[ $# -ge 1 ] || { echo "usage: $0 FILE [FILE...]" >&2; exit 2; }
# An empty folder leaves the shell's *.mp3 glob unexpanded; say what to do instead of "not a file".
case "$1" in *'*'*) echo "no mp3 files found ($1): extract the assignment's audio tar into ./source_data/ (or: make load DATA_DIR=path)" >&2; exit 1 ;; esac
NS=podsearch
BUCKET=podcasts
for f in "$@"; do
  [ -f "$f" ] || { echo "not a file: $f" >&2; exit 1; }
  name=$(basename "$f")
  echo "uploading $name ($(du -h "$f" | cut -f1)) -> s3://$BUCKET/incoming/$name"
  kubectl -n "$NS" exec -i deploy/toolbox -- aws s3 cp - "s3://$BUCKET/incoming/$name" < "$f"
done
