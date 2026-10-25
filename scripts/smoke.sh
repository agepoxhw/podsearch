#!/usr/bin/env bash
# End-to-end check on a running cluster: make sure the sample audio is indexed, then assert that the query
# "Iran" returns at least one hit from ep334. Usage: scripts/smoke.sh [DATA_DIR]
set -euo pipefail
cd "$(dirname "$0")/.."
DATA_DIR=${1:-./source_data}
API=${API:-http://localhost:8080}
want=$(ls "$DATA_DIR"/*.mp3 2>/dev/null | wc -l | tr -d ' ')
[ "$want" -ge 1 ] || { echo "no mp3 in $DATA_DIR: extract the assignment's audio tar there first" >&2; exit 1; }

in_bucket() { kubectl -n podsearch exec deploy/toolbox -- aws s3 ls "s3://podcasts/$1/" 2>/dev/null | grep -c '\.mp3' || true; }
have=$(( $(in_bucket incoming) + $(in_bucket library) ))
if [ "$have" -lt "$want" ]; then scripts/upload.sh "$DATA_DIR"/*.mp3; fi

echo "waiting for $want file(s) to be indexed (moved from incoming/ to library/)"
start=$SECONDS
until [ "$(in_bucket library)" -ge "$want" ]; do
  [ $((SECONDS - start)) -lt 3600 ] || { echo "timeout: files still in incoming/" >&2; exit 1; }
  sleep 15
done
echo "indexed in $((SECONDS - start))s"

resp=$(curl -fsS "$API/search?q=Iran&k=10")
hits=$(printf '%s' "$resp" | python3 -c 'import json,sys; print(sum(1 for h in json.load(sys.stdin)["hits"] if h["episode"].startswith("ep334")))')
[ "$hits" -ge 1 ] && echo "SMOKE OK: 'Iran' returned $hits hit(s) from ep334" || { echo "SMOKE FAILED: no ep334 hit for 'Iran'" >&2; exit 1; }
