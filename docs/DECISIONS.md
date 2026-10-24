# DECISIONS

Pinned versions and deviations from PLAN.md, with reasons.

## Phase 0: toolchain versions

Installed on the host with Homebrew on 2026-10-23 (macOS arm64):

| Tool | Version |
|---|---|
| colima | 0.10.3 (Docker engine 29.5.2 inside the VM) |
| docker CLI | 29.9.2 |
| docker buildx | v0.38.1 |
| k3d | v5.9.0 (default k3s v1.35.5-k3s1) |
| kubectl | v1.37.1 |
| helm | v4.3.1 |
| uv | 0.13.5 |
| ffmpeg | 9.0.2 |

Colima: `--vm-type vz --vz-rosetta --cpu 8 --memory 20 --disk 120`. Verified that `docker run --platform linux/amd64 alpine uname -m` prints `x86_64`.

Host setup note: `~/.docker/config.json` was created with `cliPluginsExtraDirs` pointing at `/opt/homebrew/lib/docker/cli-plugins`, so that `docker buildx` is found. (`make prereqs` in Phase 8 should check for this.)

## Phase 0: SONAR version pins (spike)

| Package | Pin | Why |
|---|---|---|
| torch / torchaudio | 2.8.0+cpu | fairseq2n 0.5.2 is built against torch 2.8.0 (not 2.6.0 as the `sonar-space[cpu]` extra suggests) |
| fairseq2n | 0.5.2+cpu (Meta index `fair.pkg.atmeta.com/fairseq2/whl/pt2.8.0/cpu`) | the PyPI wheel is the CUDA 12.8 build and refuses CPU torch |
| fairseq2 | 0.5.2 | |
| sonar-space | 0.5.0 | |

## Deviation: ML images are built for arm64 too (fallback (b) chosen)

- amd64 under Rosetta: PyTorch reports `cpu capability NO AVX` and `threads 1`. 3 clips took more than 10 minutes. Too slow.
- Decision: build `fairseq2n` from source for linux/arm64 and run natively.
- Decision (user): commit that wheel to the repo under `wheels/`, with SHA256 and build script. This overrides the "no binaries" hard rule for this one file, and CLAUDE.md was updated. Reason: Mac reviewers should not wait about an hour for a C++ build.
- Model weights: text encoder 2.85 GB, speech encoder 7.45 GB (bigger than assumed, mind the image size and disk).

## Phase 0: measured facts (native linux/arm64, 8 threads)

| Fact | Value |
|---|---|
| fairseq2n arm64 wheel | 3.2 MB, built in about 9 min; SHA256 in `wheels/SHA256SUMS` |
| Speech embed | 1.29 s per 10 s chunk (batch of 3; may include warm-up) |
| Text embed | 0.155 s per query |
| Model load (cached) | speech 7.5 s, text about 47 s on first load |
| Weights | text 2.85 GB, speech 7.45 GB |
| Peak RAM, both models loaded | about 11 GB RSS (each pod loads only one) |
| Similarity range | normal clips: -0.05 to 0.32; matching topic about 0.15-0.32; hub clips 0.40-0.57 |

## Phase 1: cluster

- k3d cluster `podsearch`: 1 server, 0 agents, Traefik disabled. Services are exposed by fixed **NodePorts** (30080 API, 30333 S3, 30265 Ray, 30300 Grafana) which k3d's `serverlb` maps to host ports 8080, 8333, 8265, 3000. Reason: fewer moving parts than an Ingress.
- Registry `podsearch-registry`: push from host as `localhost:5001/<img>`, pods pull as `podsearch-registry:5000/<img>`. Created by k3d with the cluster and deleted with it.
- S3 credentials Secret `s3-credentials` (access_key `podsearch`, random secret_key) is generated once and never overwritten.
- `make up` = prereqs, then `scripts/up.sh` (4 timed stages). Images stage is added in Phase 3 (no images exist yet).
- Helm chart `charts/podsearch` is a placeholder (one ConfigMap) that grows each phase.

- Container runtime: Colima (Lima + Apple `vz`) with 8 CPU / 20 GB / 120 GB. Any Docker engine with >= 16 GB works (Docker Desktop, OrbStack); `make prereqs` checks the memory. `--vz-rosetta` is no longer needed for our ML images (native arm64), kept only for amd64-only third-party images. To verify in Phase 8: a clean-clone `make up` on Docker Desktop.
- To check before Phase 3 image builds: the VM root disk shows 19 GB although 120 GB was requested; confirm where Docker stores its data.

## Phase 2: object store and vector DB

| Item | Pin / choice | Reason |
|---|---|---|
| SeaweedFS | **official Helm chart `seaweedfs/seaweedfs` 4.48.0** (app 4.48, image `chrislusf/seaweedfs:4.48`, multi-arch), as a pinned dependency in `Chart.yaml` / `Chart.lock` | User preference: use the official chart, like Qdrant. 4.49 was one day old, so we pinned the previous release. I first wrote my own StatefulSet; it is deleted. |
| SeaweedFS mode | `allInOne.enabled=true` (one `weed server -filer -s3` pod, Deployment with Recreate strategy, PVC 10Gi). `master`, `volume`, `filer` set `enabled: false` | Laptop scope. The chart still renders the three split StatefulSets unless they are disabled explicitly. At scale: use the split layout (separate master, volume, filer pods). |
| S3 auth | `s3.credentials.admin.existingSecret: s3-credentials`, `enableAuth: true` | One credentials Secret, generated once by `make up`. Anonymous requests get 403 (checked). |
| Bucket creation | chart value `allInOne.s3.createBuckets: [podcasts]` (post-install/upgrade hook Job) | Must be set under `allInOne.s3`, not `s3` (the latter is ignored in all-in-one mode). |
| S3 NodePort | own small Service `seaweedfs-s3` (NodePort 30333) selecting the chart's pod labels | The chart has a single Service for all ports; we only want S3 on the host. Filer gRPC 18888 is reachable in-cluster at `seaweedfs-all-in-one:18888`. |
| Qdrant | official chart 1.19.2 (app v1.19.2), 1 replica, PVC 10Gi, `fullnameOverride: qdrant` so the Service is `qdrant:6333` | 1.20.0 was the newest; 1.19.2 is a mature patch release. Collection `chunks_sonar_v1` is created by the API at startup (Phase 5), not now. |
| aws-cli | `amazon/aws-cli:2.38.4` (multi-arch) in the `toolbox` Deployment | Reviewers need no aws-cli on their host. |
| Upload path | `make upload FILE=…`, `make upload-all`, `make load`, `make ls` run `scripts/upload.sh`, which streams the file via `kubectl exec -i … aws s3 cp - s3://podcasts/incoming/<name>` | No temp copy in the pod. SHA-256 of 3 uploaded mp3s matched the local files. |
| `make up` | now runs `helm dependency build` before install | The sub-chart `.tgz` files are gitignored, so a fresh clone needs them fetched from the pinned lock. |

### Filer metadata events (mini-spike results, `weed filer.meta.tail -pathPrefix=/buckets/podcasts/incoming/`)

The Phase 4 subscriber depends on these.

- A small upload gives exactly **1 `create` event** (`oldEntry=null`, `newEntry` set).
- A 20 MB **multipart** upload (aws-cli, 5 chunks) also gives **1 create event**, carrying the final size. No `.uploads/` events appear under `incoming/`.
- Our `CopyObject` to `library/` gives **no event** under the `incoming/` prefix (the prefix filter excludes it).
- Our `DeleteObject` of `incoming/x` gives a **delete event** (`newEntry=null`). The subscriber must handle only creates and ignore deletes, or our own commit step would re-trigger it.
- Not tested yet: overwriting an existing key (expected: one event with both `oldEntry` and `newEntry`), and resuming from a saved `since_ns`.
