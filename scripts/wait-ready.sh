#!/usr/bin/env bash
# Block until the cluster and everything we deployed is ready.
set -euo pipefail
NS=podsearch
kubectl wait --for=condition=Ready node --all --timeout=120s
kubectl -n kube-system rollout status deploy/coredns --timeout=120s
curl -fsS http://localhost:5001/v2/ >/dev/null && echo "registry reachable at localhost:5001"
if kubectl -n "$NS" get deploy -o name | grep -q .; then
  kubectl -n "$NS" wait --for=condition=Available deploy --all --timeout=600s
fi
echo "ready"
