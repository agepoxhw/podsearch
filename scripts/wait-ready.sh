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
if kubectl -n "$NS" get sts -o name | grep -q .; then
  for sts in $(kubectl -n "$NS" get sts -o name); do kubectl -n "$NS" rollout status "$sts" --timeout=600s; done
fi
# KubeRay sets this condition once the head and all wanted workers are Ready.
kubectl -n "$NS" wait --for=condition=RayClusterProvisioned raycluster/ray --timeout=600s
echo "ready"
