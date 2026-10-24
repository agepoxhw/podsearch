#!/usr/bin/env bash
# Remove superseded ml-* image tags from the host Docker (each generation is ~10 GB). Keeps the tags in
# .build/images.env. The registry's own storage is only emptied by `make down` (it is recreated by `make up`).
set -euo pipefail
cd "$(dirname "$0")/.."
keep=$(sed -E 's#.*/(ml-[a-z]+):(.*)#\1:\2#' .build/images.env 2>/dev/null || true)
removed=0
for ref in $(docker images --format '{{.Repository}}:{{.Tag}}' | grep -E '^localhost:5001/ml-'); do
  short=${ref#localhost:5001/}
  if ! grep -qx "$short" <<<"$keep"; then docker rmi "$ref" >/dev/null && echo "removed $ref" && removed=$((removed+1)); fi
done
docker image prune -f >/dev/null
echo "removed $removed old tag(s); kept: $(echo $keep)"
