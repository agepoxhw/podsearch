#!/usr/bin/env bash
# Build the ML images for this machine's architecture and push them to the k3d registry.
# Tags are content hashes of each image's inputs (never `latest`): unchanged inputs give the same tag,
# so an existing tag is skipped, and a changed input gives a new tag the cluster is forced to pull.
# Writes the resulting tags to .build/images.env for the Helm install.
set -euo pipefail
cd "$(dirname "$0")/.."
REG_PUSH=localhost:5001          # how the host reaches the registry
REG_POD=podsearch-registry:5000  # how pods reach it (what goes into the chart)
case "$(uname -m)" in arm64|aarch64) PLATFORM=linux/arm64; ARCH=arm64 ;; *) PLATFORM=linux/amd64; ARCH=amd64 ;; esac

hash_of() { cat "$@" 2>/dev/null | shasum -a 256 | cut -c1-12; }
tree_hash() { find "$@" -type f ! -name '*.pyc' | LC_ALL=C sort | xargs shasum -a 256 | shasum -a 256 | cut -c1-12; }
in_registry() { curl -fsI -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.v2+json' \
                  "http://$REG_PUSH/v2/$1/manifests/$2" >/dev/null 2>&1; }

build() {   # build <name> <tag> [extra docker args...]
  local name=$1 tag=$2; shift 2
  if in_registry "$name" "$tag"; then echo "  $name:$tag already in registry, skipping"; return; fi
  echo "  building $name:$tag ($PLATFORM)"
  docker buildx build --platform "$PLATFORM" -f "images/$name/Dockerfile" "$@" \
    -t "$REG_PUSH/$name:$tag" --load --progress=quiet .
  docker push -q "$REG_PUSH/$name:$tag" >/dev/null
}

BASE_TAG=$(hash_of images/ml-base/Dockerfile "images/ml-base/requirements.$ARCH.txt" wheels/SHA256SUMS)
SRC_HASH=$(tree_hash src)
APP_TAG=$(echo "$BASE_TAG $SRC_HASH $(hash_of images/ml-app/Dockerfile)" | shasum -a 256 | cut -c1-12)
SPEECH_TAG=$(echo "$BASE_TAG $SRC_HASH $(hash_of images/ml-speech/Dockerfile)" | shasum -a 256 | cut -c1-12)
TEXT_TAG=$(echo "$BASE_TAG $SRC_HASH $(hash_of images/ml-text/Dockerfile)" | shasum -a 256 | cut -c1-12)

echo "images for $PLATFORM"
build ml-base "$BASE_TAG"
# The base is tagged in the registry namespace; the model images start FROM that exact tag.
build ml-app "$APP_TAG" --build-arg "BASE=$REG_PUSH/ml-base:$BASE_TAG"
build ml-speech "$SPEECH_TAG" --build-arg "BASE=$REG_PUSH/ml-base:$BASE_TAG"
build ml-text "$TEXT_TAG" --build-arg "BASE=$REG_PUSH/ml-base:$BASE_TAG"

mkdir -p .build
cat > .build/images.env <<ENV
ML_BASE_IMAGE=$REG_POD/ml-base:$BASE_TAG
ML_APP_IMAGE=$REG_POD/ml-app:$APP_TAG
ML_SPEECH_IMAGE=$REG_POD/ml-speech:$SPEECH_TAG
ML_TEXT_IMAGE=$REG_POD/ml-text:$TEXT_TAG
ENV
cat .build/images.env
