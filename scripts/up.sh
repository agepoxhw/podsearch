#!/usr/bin/env bash
# Idempotent bring-up: safe to run on nothing, on a running cluster, or on a stopped one.
set -euo pipefail
cd "$(dirname "$0")/.."
CLUSTER=podsearch
NS=podsearch
TOTAL=4
stage() { echo; echo "[$1/$TOTAL] $2"; T0=$SECONDS; }
done_() { echo "    done in $((SECONDS - T0))s"; }
START=$SECONDS

stage 1 "cluster"
if k3d cluster list "$CLUSTER" >/dev/null 2>&1; then
  k3d cluster start "$CLUSTER"        # no-op if running; restarts it after a Docker/Colima restart
else
  k3d cluster create --config cluster/k3d.yaml --wait
fi
k3d kubeconfig merge "$CLUSTER" --kubeconfig-merge-default --kubeconfig-switch-context >/dev/null
done_

stage 2 "namespace and secrets"
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -
# Generated once. Never overwritten, so a re-run doesn't rotate credentials under running pods.
if ! kubectl -n "$NS" get secret s3-credentials >/dev/null 2>&1; then
  kubectl -n "$NS" create secret generic s3-credentials \
    --from-literal=access_key=podsearch \
    --from-literal=secret_key="$(openssl rand -hex 16)"
else
  echo "secret s3-credentials already exists, keeping it"
fi
done_

stage 3 "deploy chart"
# Sub-charts (Qdrant, SeaweedFS) are not committed; fetch the exact versions pinned in Chart.lock.
helm dependency build charts/podsearch >/dev/null
helm upgrade --install podsearch charts/podsearch --namespace "$NS" --wait
done_

stage 4 "wait until ready"
scripts/wait-ready.sh
done_

echo; echo "make up finished in $((SECONDS - START))s"
