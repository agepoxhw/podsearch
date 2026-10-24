#!/usr/bin/env bash
# Fail fast, with an install hint, if the host can't run `make up`. Phase 8 extends this.
set -uo pipefail
fail=0
ver_ge() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" = "$2" ]; }  # ver_ge have want
need() {  # need <cmd> <min-version> <version-command>
  if ! command -v "$1" >/dev/null; then echo "MISSING  $1  (macOS: brew install $1)"; fail=1; return; fi
  have=$(eval "$3" | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -1)
  if ver_ge "$have" "$2"; then echo "ok       $1 $have"; else echo "TOO OLD  $1 $have (need >= $2)"; fail=1; fi
}
need docker 24.0 "docker --version"
need k3d 5.6 "k3d version"
need kubectl 1.28 "kubectl version --client"
need helm 3.12 "helm version --short"
for t in openssl curl; do command -v $t >/dev/null && echo "ok       $t" || { echo "MISSING  $t"; fail=1; }; done

if docker info >/dev/null 2>&1; then
  mem_gb=$(( $(docker info --format '{{.MemTotal}}') / 1024 / 1024 / 1024 ))
  if [ "$mem_gb" -ge 16 ]; then echo "ok       docker memory ${mem_gb} GB"
  else echo "TOO LOW  docker memory ${mem_gb} GB (need >= 16). macOS: colima start --vm-type vz --cpu 8 --memory 20 --disk 120"; fail=1; fi
else
  echo "NOT RUNNING  docker daemon (macOS: colima start --vm-type vz --cpu 8 --memory 20 --disk 120)"; fail=1
fi
[ $fail -eq 0 ] && echo "prereqs OK" || { echo "prereqs FAILED"; exit 1; }
