#!/usr/bin/env bash
# Submit the ingest pipeline for the given incoming/ names through the Ray Jobs REST API and wait for it.
# Debug helper until the controller (Phase 4c) does this on its own.  Usage: scripts/run-job.sh a.mp3 b.mp3
set -euo pipefail
API=${RAY_API:-http://localhost:8265}
keys=""; for n in "$@"; do keys="$keys incoming/$n"; done
ID=$(curl -fsS -X POST "$API/api/jobs/" -H 'Content-Type: application/json' \
  -d "{\"entrypoint\":\"python -m podsearch.ingest.pipeline$keys\"}" | python3 -c "import sys,json;print(json.load(sys.stdin)['submission_id'])")
echo "submitted $ID"
while :; do
  st=$(curl -fsS "$API/api/jobs/$ID" | python3 -c "import sys,json;print(json.load(sys.stdin)['status'])")
  case $st in SUCCEEDED|FAILED|STOPPED) break;; esac; sleep 3
done
echo "status $st"
curl -fsS "$API/api/jobs/$ID/logs" | python3 -c "
import sys,json
for l in json.load(sys.stdin)['logs'].splitlines():
    if l.startswith('{') and '\"file ' in l: d=json.loads(l); print(d['msg'], d.get('file'), d.get('outcome',''), d.get('chunks',''))"
