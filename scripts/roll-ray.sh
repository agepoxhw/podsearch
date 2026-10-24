#!/usr/bin/env bash
# Recreate Ray pods that run a different image than the RayCluster spec asks for.
# (KubeRay never replaces running pods after a pod-template change; see up.sh.)
set -euo pipefail
NS=podsearch
want_head=$(kubectl -n $NS get raycluster ray -o jsonpath='{.spec.headGroupSpec.template.spec.containers[0].image}')
want_worker=$(kubectl -n $NS get raycluster ray -o jsonpath='{.spec.workerGroupSpecs[0].template.spec.containers[0].image}')
rolled=()
while read -r pod type image; do
  want=$want_worker; [ "$type" = head ] && want=$want_head
  if [ "$image" != "$want" ]; then
    echo "rolling $pod ($image -> $want)"; kubectl -n $NS delete pod "$pod" --wait=false >/dev/null; rolled+=("$pod")
  fi
done < <(kubectl -n $NS get pods -l ray.io/cluster=ray \
  -o jsonpath='{range .items[*]}{.metadata.name} {.metadata.labels.ray\.io/node-type} {.spec.containers[0].image}{"\n"}{end}')
# Old pods stay "Ready" while terminating, so wait for them to be gone before waiting for the new ones.
for pod in "${rolled[@]:-}"; do [ -n "$pod" ] && kubectl -n $NS wait --for=delete "pod/$pod" --timeout=180s >/dev/null; done
if [ "${#rolled[@]}" -gt 0 ]; then
  until kubectl -n $NS get pods -l ray.io/node-type=head -o name 2>/dev/null | grep -q .; do sleep 2; done
  kubectl -n $NS wait --for=condition=Ready pod -l ray.io/cluster=ray --timeout=300s >/dev/null
fi
echo "ray pods match the spec"
