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
  else echo "TOO LOW  docker memory ${mem_gb} GB (need >= 16). macOS: colima start --vm-type vz --cpu 8 --memory 20 --disk 200"; fail=1; fi
  # A first build needs ~40 GB (host images + registry copy + node copy + weights cache); the kubelet starts
  # evicting pods when free space drops below ~15%. Measure the disk Docker really stores data on.
  root=$(docker info --format '{{.DockerRootDir}}')
  free_gb=$(docker run --rm -v "$root":/d:ro alpine df -Pk /d 2>/dev/null | awk 'NR==2 {print int($4/1024/1024)}')
  if [ "${free_gb:-0}" -ge 50 ]; then echo "ok       docker disk ${free_gb} GB free"
  else echo "TOO LOW  docker disk ${free_gb:-?} GB free (need >= 50). macOS: colima stop && colima start --disk 200  (or: make clean-images)"; fail=1; fi
else
  echo "NOT RUNNING  docker daemon (macOS: colima start --vm-type vz --cpu 8 --memory 20 --disk 200)"; fail=1
fi
[ $fail -eq 0 ] && echo "prereqs OK" || { echo "prereqs FAILED"; exit 1; }
