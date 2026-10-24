#!/usr/bin/env bash
# Idempotent bring-up: safe to run on nothing, on a running cluster, or on a stopped one.
set -euo pipefail
cd "$(dirname "$0")/.."
CLUSTER=podsearch
NS=podsearch
TOTAL=5
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
# access_key/secret_key are read by our code; RUSTFS_* are the names the RustFS pod expects (same values).
if ! kubectl -n "$NS" get secret s3-credentials >/dev/null 2>&1; then
  SECRET_KEY="$(openssl rand -hex 16)"
  kubectl -n "$NS" create secret generic s3-credentials \
    --from-literal=access_key=podsearch --from-literal=secret_key="$SECRET_KEY" \
    --from-literal=RUSTFS_ACCESS_KEY=podsearch --from-literal=RUSTFS_SECRET_KEY="$SECRET_KEY"
else
  echo "secret s3-credentials already exists, keeping it"
  # Older clusters (SeaweedFS era) lack the RUSTFS_* keys: add them, copying the existing values.
  if [ -z "$(kubectl -n "$NS" get secret s3-credentials -o jsonpath='{.data.RUSTFS_ACCESS_KEY}')" ]; then
    kubectl -n "$NS" patch secret s3-credentials -p "{\"data\":{\"RUSTFS_ACCESS_KEY\":\"$(kubectl -n "$NS" get secret s3-credentials -o jsonpath='{.data.access_key}')\",\"RUSTFS_SECRET_KEY\":\"$(kubectl -n "$NS" get secret s3-credentials -o jsonpath='{.data.secret_key}')\"}}"
  fi
fi
# Bearer token RustFS sends with every webhook call; the controller rejects calls without it.
if ! kubectl -n "$NS" get secret webhook-token >/dev/null 2>&1; then
  kubectl -n "$NS" create secret generic webhook-token --from-literal=token="$(openssl rand -hex 16)"
fi
done_

stage 3 "build and push images"
# Tags are content hashes, so unchanged images are skipped (a warm re-run takes under a second).
scripts/build-images.sh
done_

stage 4 "deploy chart"
# Sub-charts (Qdrant, RustFS, KubeRay) are not committed; fetch the exact versions pinned in Chart.lock.
helm dependency build charts/podsearch >/dev/null
# Helm installs a chart's crds/ only on first install, never on upgrade, so apply them ourselves.
# --server-side: the RayCluster CRD is too big for the client-side "last-applied" annotation.
helm show crds charts/podsearch/charts/kuberay-operator-*.tgz | kubectl apply --server-side -f - >/dev/null
source .build/images.env   # written by build-images.sh: ML_APP_IMAGE, ML_SPEECH_IMAGE, ...
# --force-conflicts: the Ray autoscaler edits spec.workerGroupSpecs and so owns it; Helm takes it back on
# each run and the autoscaler re-adjusts within seconds. Without the flag, upgrade fails on a field conflict.
# No --wait: Helm would wait for the RayCluster, which cannot become ready until the pods are rolled below.
# wait-ready.sh does the waiting instead.
helm upgrade --install podsearch charts/podsearch --namespace "$NS" --force-conflicts \
  --set images.mlApp="$ML_APP_IMAGE" \
  --set images.mlSpeech="$ML_SPEECH_IMAGE" \
  --set images.mlText="$ML_TEXT_IMAGE" \
  --set images.searchApi="$SEARCH_API_IMAGE"
# KubeRay does NOT roll existing Ray pods when the RayCluster's pod template changes (it logs
# shouldDelete=false). So delete any Ray pod whose image differs from the spec; the operator recreates it.
scripts/roll-ray.sh
done_

stage 5 "wait until ready"
scripts/wait-ready.sh
done_

echo; echo "make up finished in $((SECONDS - START))s"
