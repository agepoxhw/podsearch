#!/usr/bin/env bash
# Upload local audio files to s3://podcasts/incoming/ through the in-cluster toolbox pod.
# Usage: scripts/upload.sh FILE [FILE...]
# Each file is streamed over `kubectl exec -i` straight into `aws s3 cp -`, so the host
# needs no aws-cli and the pod needs no scratch disk.
set -euo pipefail
[ $# -ge 1 ] || { echo "usage: $0 FILE [FILE...]" >&2; exit 2; }
NS=podsearch
BUCKET=podcasts
for f in "$@"; do
  [ -f "$f" ] || { echo "not a file: $f" >&2; exit 1; }
  name=$(basename "$f")
  echo "uploading $name ($(du -h "$f" | cut -f1)) -> s3://$BUCKET/incoming/$name"
  kubectl -n "$NS" exec -i deploy/toolbox -- aws s3 cp - "s3://$BUCKET/incoming/$name" < "$f"
done
