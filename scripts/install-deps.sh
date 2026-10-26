#!/usr/bin/env bash
# Install whatever `make up` needs and the host is missing. Runs before the prereqs check.
#   macOS  : Homebrew (colima, docker, docker-buildx, k3d, kubernetes-cli, helm), then starts Colima if no Docker daemon
#   Linux  : apt (docker.io, buildx, openssl, curl, python3, perl); k3d/kubectl/helm are downloaded at pinned versions
# It only installs what is MISSING and never upgrades anything. SKIP_INSTALL=1 turns it off (prereqs then just checks).
set -euo pipefail

[ "${SKIP_INSTALL:-0}" = "1" ] && { echo "install-deps: SKIP_INSTALL=1, not installing anything"; exit 0; }

K3D_VERSION=v5.9.0        # the versions the cluster was developed and tested with
KUBECTL_VERSION=v1.37.1
HELM_VERSION=v4.3.1
BIN_DIR=${BIN_DIR:-/usr/local/bin}
OS=$(uname -s)
case "$(uname -m)" in x86_64|amd64) ARCH=amd64 ;; arm64|aarch64) ARCH=arm64 ;; *) ARCH=$(uname -m) ;; esac

have() { command -v "$1" >/dev/null 2>&1; }
say() { echo "install-deps: $*"; }
as_root() { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi; }
sha256_of() { if have sha256sum; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }

# download <url> <expected sha256> <dest>: fetch, verify, fail loudly on a mismatch
download() {
  local url=$1 want=$2 dest=$3
  curl -fsSL "$url" -o "$dest"
  [ "$(sha256_of "$dest")" = "$want" ] || { echo "install-deps: checksum mismatch for $url" >&2; exit 1; }
}

install_linux_tools() {
  tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
  if ! have k3d; then
    say "installing k3d $K3D_VERSION"
    base=https://github.com/k3d-io/k3d/releases/download/$K3D_VERSION
    want=$(curl -fsSL "$base/checksums.txt" | awk -v f="_dist/k3d-linux-$ARCH" '$2==f {print $1}')
    download "$base/k3d-linux-$ARCH" "$want" "$tmp/k3d"
    as_root install -m 0755 "$tmp/k3d" "$BIN_DIR/k3d"
  fi
  if ! have kubectl; then
    say "installing kubectl $KUBECTL_VERSION"
    base=https://dl.k8s.io/release/$KUBECTL_VERSION/bin/linux/$ARCH
    download "$base/kubectl" "$(curl -fsSL "$base/kubectl.sha256")" "$tmp/kubectl"
    as_root install -m 0755 "$tmp/kubectl" "$BIN_DIR/kubectl"
  fi
  if ! have helm; then
    say "installing helm $HELM_VERSION"
    file=helm-$HELM_VERSION-linux-$ARCH.tar.gz
    want=$(curl -fsSL "https://get.helm.sh/$file.sha256sum" | cut -d' ' -f1)
    download "https://get.helm.sh/$file" "$want" "$tmp/helm.tgz"
    tar -xzf "$tmp/helm.tgz" -C "$tmp"
    as_root install -m 0755 "$tmp/linux-$ARCH/helm" "$BIN_DIR/helm"
  fi
}

linux() {
  if ! have apt-get; then
    say "this Linux has no apt-get; install docker (+buildx), k3d, kubectl, helm, openssl, curl, python3 yourself (see README)"
    return 0
  fi
  local pkgs=()
  have docker  || pkgs+=(docker.io)
  have openssl || pkgs+=(openssl)
  have curl    || pkgs+=(curl ca-certificates)
  have python3 || pkgs+=(python3)
  have shasum  || pkgs+=(perl)                       # shasum ships in the perl package
  if [ ${#pkgs[@]} -gt 0 ]; then
    say "apt: installing ${pkgs[*]}"
    as_root apt-get update -qq
    as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${pkgs[@]}"
  fi
  if have docker && ! docker buildx version >/dev/null 2>&1; then
    say "apt: installing the docker buildx plugin"
    as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker-buildx \
      || as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker-buildx-plugin \
      || say "could not install buildx from apt: https://docs.docker.com/build/install-buildx/"
  fi
  install_linux_tools
  if have docker && ! docker info >/dev/null 2>&1 && [ "${SKIP_DAEMON:-0}" != "1" ]; then
    have systemctl && as_root systemctl enable --now docker || true
    if ! docker info >/dev/null 2>&1; then
      [ "$(id -u)" -ne 0 ] && as_root usermod -aG docker "$USER" || true
      say "docker is installed but this shell cannot use it yet. Log out and in (or run: newgrp docker), then run make up again."
      exit 1
    fi
  fi
}

macos() {
  if ! have brew; then
    say "Homebrew is missing. Install it from https://brew.sh, then run make up again."
    exit 1
  fi
  local pkgs=()
  have colima  || pkgs+=(colima)
  have docker  || pkgs+=(docker)
  have k3d     || pkgs+=(k3d)
  have kubectl || pkgs+=(kubernetes-cli)
  have helm    || pkgs+=(helm)
  if ! docker buildx version >/dev/null 2>&1; then pkgs+=(docker-buildx); fi
  if [ ${#pkgs[@]} -gt 0 ]; then
    say "brew: installing ${pkgs[*]}"
    brew install "${pkgs[@]}"
  fi
  if ! docker buildx version >/dev/null 2>&1; then          # Homebrew's docker CLI finds plugins in ~/.docker/cli-plugins
    mkdir -p ~/.docker/cli-plugins
    ln -sfn "$(brew --prefix)/opt/docker-buildx/bin/docker-buildx" ~/.docker/cli-plugins/docker-buildx
  fi
  # No Docker daemon at all (Docker Desktop not running, Colima not started): start Colima with the sizes the build needs.
  if ! docker info >/dev/null 2>&1; then
    local cpu mem
    cpu=$(sysctl -n hw.ncpu); [ "$cpu" -gt 8 ] && cpu=8
    mem=20
    say "no Docker daemon: starting Colima ($cpu CPUs, ${mem} GB memory, 200 GB disk)"
    colima start --vm-type vz --cpu "$cpu" --memory "$mem" --disk 200
  fi
}

case "$OS" in
  Darwin) macos ;;
  Linux)  linux ;;
  *) say "unsupported OS '$OS' (macOS and Linux only)"; exit 1 ;;
esac
say "done"
