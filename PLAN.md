# PLAN — Podcast audio semantic search on local Kubernetes

> Written by Opus for execution by Sonnet, phase by phase, **with the user in the loop**.
> Assignment: `exercise_cross.html`. Input audio: `source_data/*.mp3` (3 × 30 min, 96 kbps).
> Execution rules for the agent live in `CLAUDE.md`. Read both before starting any phase.

---

## 0. What we found before planning (facts, not guesses)

| Fact | Consequence |
|---|---|
| Host is **Apple Silicon (arm64)**, 12 CPU, 32 GB RAM, macOS | Plenty of RAM for a single-node cluster, but see the next row. |
| `fairseq2n` (native core of fairseq2, which SONAR needs) ships wheels only for **linux x86_64** and **macOS arm64**. **No linux/aarch64 wheel.** | ML containers must be **linux/amd64**. On the Mac they run under Rosetta emulation. **This is the #1 project risk**, so Phase 0 is a spike that proves it works before anything else gets built. |
| `sonar-space` 0.5.0 needs Python ≥3.10, `fairseq2>=0.5.2`, torch/torchaudio (`[cpu]` extra pins torch 2.6.0+cpu) | Pin exact versions in Phase 0 and never let them float. fairseq2n is compiled against one specific torch version. |
| Nothing is installed: no docker, kind/k3d, kubectl, helm, uv, ffmpeg. Host Python is 3.9. | Phase 0 installs the toolchain. All Python runs **in containers**, so the host Python doesn't matter. |
| **MinIO community edition**: official Docker images stopped in Oct 2025, repo in maintenance mode since Dec 2025, then archived. | Don't pick MinIO, and be ready to explain why in the interview (no patched images means supply-chain and CVE risk). Use **SeaweedFS** (Apache-2.0, active, S3 API, arm64 images). |
| The assignment does **not** require air-gap support. Reviewers will `git clone` the GitHub repo and run one command, with internet access. | The repo stays **light**: code, manifests, lock files and docs only. Nothing heavy goes in git (no audio, model weights, images, vendored chart `.tgz` files or binaries). Heavy things are **fetched or built by `make up`** from pinned versions. Air-gap is only a short "how I'd adapt it" note in the README, because the company often works air-gapped. |
| Assignment says "about one working day", but you need to *understand* everything. | Budget about 2–3 focused days. The core path (Phases 0–6) works on its own. Monitoring, clean-clone hardening and polish (7–9) are layered on top. |

---

## 1. Target architecture

```
                    ┌──────────────────────────── k3d cluster (namespace: podsearch) ───────────────────────────┐
  make upload ──S3──▶  SeaweedFS (S3 API + filer)                                                               │
  (or UI upload)    │   bucket "podcasts"                                                                        │
                    │     incoming/<name>       ◀── upload here; holds ONLY the backlog (small, cheap to list)   │
                    │     library/<name>        ◀── indexed audio (moved here after success; the API plays from here) │
                    │     rejected/<name>       ◀── failed after N attempts, or duplicate content (+ .reason.json) │
                    │     _state/<name>.json    ◀── attempts, last_error, sha256, status (for retry accounting)   │
                    │        │                                                                                   │
                    │        │ (1) filer metadata subscription (gRPC, prefix incoming/) → seconds-level latency   │
                    │        │ (2) reconcile: list incoming/ every RECONCILE_INTERVAL (default 5 min) → safety net │
                    │        ▼                                                                                   │
                    │   ingest-controller (Deployment, 1 replica, tiny image)                                    │
                    │        both sources → one de-duplicated work queue → debounce/batch → submit Ray job       │
                    │        ▼                                                                                   │
                    │   RayCluster (KubeRay operator): head + worker group (autoscale 1..N)                      │
                    │     Ray Data pipeline:                                                                     │
                    │       read keys → sha256 + decode (ffmpeg → 16 kHz mono) → 10 s chunks (CPU tasks)         │
                    │       → SonarSpeechEmbedder (actor pool, loads model once) → batch upsert ─────┐           │
                    │       → "commit": copy incoming/<name> → library/<name>, then delete incoming   │           │
                    │                                                                                 ▼           │
                    │   Qdrant (StatefulSet + PVC)  collection "chunks_sonar_v1", 1024-d, Cosine              │
                    │                                                                                 ▲           │
  browser ──HTTP──▶ │   search-api (FastAPI): text encoder → query vector → Qdrant search             │           │
                    │     score_threshold=0.20 → hits; serves UI + /audio/{name} (Range streaming from S3)        │
                    │                                                                                            │
                    │   [optional] Prometheus + Grafana: API, controller, Ray, Qdrant, SeaweedFS metrics         │
                    └────────────────────────────────────────────────────────────────────────────────────────────┘
```

**Data contract for a chunk (Qdrant point):**
- `file_id` = **sha256 of the file content**, not the key or etag. Keys change when we move files, and S3 copies can change etags (multipart objects), so identity must come from the content.
- `id` = UUIDv5(`file_id` + `chunk_index`). Deterministic, so re-runs overwrite instead of duplicating, no matter which folder the file is in.
- `vector` = 1024-d SONAR speech embedding.
- `payload` = `{file_id, name, audio_key: "library/<name>", episode, chunk_index, start_s, end_s, duration_s, rms_db, model: "sonar_speech_encoder_eng", pipeline_version, indexed_at}`. `audio_key` is the file's *final* location, known before the move happens.

**File lifecycle (folder = state, "move" = server-side CopyObject + DeleteObject):**
```
incoming/x.mp3 ──index ok──▶ upsert vectors (wait=true) ──▶ copy → library/x.mp3 ──▶ delete incoming/x.mp3
       │
       ├─decode/embed error──▶ _state/x.mp3.json attempts++ ──(attempts ≥ 3)──▶ move → rejected/x.mp3 + rejected/x.mp3.reason.json
       └─same sha already indexed under another name──▶ move → rejected/ (reason: duplicate_of=<name>)
```
Order matters: **vectors first, then copy, then delete.** A crash at any point leaves the file in `incoming/`, so it gets picked up again and re-processed idempotently (same sha → same point IDs). Before re-embedding, the pipeline checks whether `library/<name>` already exists with the same sha. If it does, the earlier run crashed mid-move, so it just finishes the delete.

**Search hit (API response):**
`{rank, score, episode, name, start_s, end_s, start_hms, audio_url: "/audio/<name>#t=<start>,<end>"}`, plus top-level `{query, threshold, k, took_ms, model}`.

---

## 2. Key design decisions (the interview talks about these)

| Area | Choice | Rejected alternatives and why |
|---|---|---|
| Cluster | **k3d** (k3s in Docker) | **kind**: also good. k3d wins on a built-in local registry (`k3d registry create`), fast startup, and k3s being a real, small production distro (bonus: k3s has a documented air-gap path, if that ever matters). **minikube**: heavier, VM-in-VM on a Mac. |
| Container runtime on the Mac | **Colima** (`--vm-type vz --vz-rosetta`) | Docker Desktop has licensing concerns. OrbStack is also fine. Rosetta is required to run the amd64 ML images fast. |
| Object store | **SeaweedFS** (single `weed server -s3` pod) | **MinIO**: upstream community edition is abandoned (see §0). **Garage**: no bucket notifications and less familiar. **Ceph RGW**: far too heavy. Our ingestion only needs `ListObjects`, `GetObject` and `PutObject`, so the store can be swapped. |
| New-file detection | **Both, by default:** (1) SeaweedFS **filer metadata subscription** (gRPC `SubscribeMetadata`, prefix `/buckets/podcasts/incoming/`, resumes from a saved `since_ns` checkpoint) for seconds-level latency, plus (2) a **reconcile loop** listing `incoming/` every `RECONCILE_INTERVAL` (default 5 min) as the correctness safety net. Both feed one de-duplicated queue. | **Polling only**: simplest and works with any S3, but latency equals the interval and LIST cost grows with the bucket. **Events only**: fast, but misses files when the subscriber is down or the config breaks, gives no backfill for files that already exist, and can't re-index. **Kafka/NATS via `notification.toml`**: a durable queue is the right next step at scale, but it adds a broker to operate on a laptop. Trade-off to say out loud: the subscription is **SeaweedFS-specific** (not the S3 API), so it sits behind an `EventSource` interface and can be switched off (`EVENT_SOURCE=none`). Polling alone still works against any customer S3. |
| Folder layout | **Folder = lifecycle state**: `incoming/` → `library/` or `rejected/`. Processed files are moved out of `incoming/`. | **Immutable raw bucket plus state markers only**: never mutates user data, but every scan lists *all* files ever uploaded (O(total) instead of O(backlog)). Moving keeps `incoming/` small, so reconcile stays cheap at any scale, and "what's pending?" is just `ls incoming/`. Cost: a move isn't atomic (copy then delete), handled with content-hash identity and ordering (see §1). |
| Ingest state | Folder location **plus** `_state/<name>.json` (attempts, last_error, sha256) | Postgres or Redis means another stateful component. S3 PUT is atomic, the state is inspectable with any S3 tool, and it survives restarts. At 1000× this moves to Postgres. |
| Orchestration | **KubeRay** RayCluster plus Ray Data (recommended by the assignment) | Ray in local mode inside one pod is simpler but loses the distributed and autoscaling story. A RayJob CR per batch spins up a cluster per job, which means very slow cold starts with multi-GB images. Celery or Argo would ignore the recommendation. |
| Vector DB | **Qdrant** | **Milvus**: needs etcd, MinIO and Pulsar, too heavy. **pgvector**: viable, but weaker ANN tuning and quantization. **Weaviate**: fine, but heavier. Qdrant is one binary, has native Cosine and `score_threshold`, payload filters, snapshots (backup and migration), quantization, and an arm64 image. |
| Model weights | **Downloaded at image build time** and baked into images (`FAIRSEQ2_CACHE_DIR`), never committed to git. Two images share one base: `ml-speech` (speech encoder, used by Ray workers) and `ml-text` (text encoder, used by the API) | **Download on pod start**: smaller images, but every new pod or node re-downloads GBs, startup is slow and depends on Meta's CDN being up, and the running artifact isn't immutable. **PVC model cache filled by a Job**: one download, small images, but more moving parts, and a ReadWriteOnce volume doesn't share across nodes. Baked images are immutable and start fast; the cost is a slow *first* build (Docker layer cache makes later builds fast). ImageVolume (K8s 1.33+) is elegant but too new. Splitting by role means each pod loads only the model it needs, which saves about 3 GB of RAM per pod. |
| Chunking | Non-overlapping 10 s windows (as required). Tail under 2 s is dropped, tail of 2–10 s is zero-padded. RMS energy stored per chunk. | 5 s-stride overlap would improve boundary recall. Mention it as an improvement, but stick to the spec. |
| Similarity | Qdrant `Distance.Cosine` (normalizes on insert). Query vector L2-normalized. `score_threshold=0.20`. | Dot product without normalization would make "confidence" mean something other than cosine, which breaks requirement 6. |
| Audio playback | API proxies `/audio/{name}` with HTTP Range support. Browser uses a media fragment `#t=start,end`. | Presigned S3 URLs would point at an in-cluster hostname the browser can't reach, which is a classic edge case. |
| Packaging and delivery | **Light GitHub repo**, one **Helm umbrella chart** with pinned dependency versions (`Chart.yaml` + `Chart.lock` committed; `helm dependency build` fetches the `.tgz` files during `make up`). Images are **built locally** by `make up` from pinned bases and lock files, then pushed to the k3d registry. | **Commit vendored charts and binaries**: works offline, but bloats the repo for no benefit here. **Prebuilt images on GHCR** (CI publishes them, `make up` pulls): faster first run for reviewers, but multi-GB images strain free CI runners, and it adds a registry dependency. A good stretch goal. **Kustomize**: fine, but Helm gives versioned dependencies and a lock file. |

---

## 3. Repository layout (target)

```
homework/                         # git repo root (git init in Phase 0)
├── Makefile                      # single entrypoint: prereqs, up, down, load, eval, smoke, test …
├── README.md                     # the deliverable write-up (Phase 9)
├── PLAN.md  CLAUDE.md
├── docs/
│   ├── JOURNAL.md                # running learning log → feeds README "what I learned"
│   └── DECISIONS.md              # ADR-style log of decisions and deviations from this plan
├── cluster/k3d.yaml              # k3d config: ports, registry, k3s args
├── images/
│   ├── ml-base/Dockerfile        # python3.11 + ffmpeg + torch-cpu + fairseq2 + sonar + ray (linux/amd64)
│   ├── ml-speech/Dockerfile      # FROM ml-base + bake speech encoder weights + code
│   ├── ml-text/Dockerfile        # FROM ml-base + bake text encoder weights + code
│   └── controller/Dockerfile     # python-slim + boto3 + httpx (multi-arch)
├── src/podsearch/
│   ├── config.py                 # env-driven settings (pydantic-settings)
│   ├── audio.py                  # decode + chunk (pure functions, unit-tested)
│   ├── ids.py                    # deterministic point IDs
│   ├── store.py                  # S3 helpers (list, get, Range streaming, copy/delete)
│   ├── vectors.py                # Qdrant collection bootstrap, upsert, delete-by-key, search
│   ├── ingest/controller.py      # work queue, dedupe, debounce/batch, Ray job submission, /metrics
│   ├── ingest/sources.py         # EventSource interface: SeaweedFsSubscriber + Reconciler
│   ├── ingest/lifecycle.py       # move (copy+delete), reject, _state read/write, crash-recovery checks
│   ├── ingest/proto/             # vendored SeaweedFS filer.proto → stubs generated at image build
│   ├── ingest/pipeline.py        # Ray Data job entrypoint
│   └── api/app.py + static/index.html
├── charts/podsearch/             # umbrella chart
│   ├── Chart.yaml  Chart.lock    # deps: kuberay-operator, qdrant (pinned; .tgz fetched at `make up`, gitignored)
│   ├── values.yaml  values-monitoring.yaml
│   └── templates/                # seaweedfs, raycluster, controller, api, secrets, netpol, monitors
├── monitoring/                   # grafana dashboard JSON, prometheus rules
├── scripts/                      # prereqs.sh, up.sh, wait-ready.sh, upload.sh, eval.py, smoke.sh
├── tests/                        # unit (chunking, ids, threshold) + smoke (e2e)
├── results/queries.md            # required query results with ✓/✗ labels
└── source_data/                  # input mp3s (gitignored: 68 MB, and copyrighted audio). Reviewers drop their tar contents here
```

---

## 4. How to execute this plan (Opus planned, Sonnet executes)

1. In this folder, start Claude Code on Sonnet: `claude --model sonnet` (or `/model sonnet` in a running session).
2. Run one phase at a time: *"Execute Phase N of PLAN.md."* `CLAUDE.md` tells Sonnet to teach as it builds: explain before acting, verify after, quiz you at the checkpoint, and stop for your go-ahead.
3. Don't skip the **checkpoint questions**. If you can't answer one, ask Sonnet to explain until you can, because the interview will ask the same things.
4. If something stays stuck after two attempts, switch back to Opus for the debugging step (`/model opus`), then return to Sonnet.
5. Commit at the end of every phase (`git commit`), so each phase is a reviewable unit in the history the reviewers will see.

---

## 5. Phases

Each phase lists **Goal → Tasks → Done when (verification) → Checkpoint (you must be able to answer)**.

### Phase 0 — Toolchain and the de-risking spike *(most important; ~2–3 h)*

**Goal:** prove that SONAR runs in a linux/amd64 container on this Mac, that such a container runs inside k3d, and learn the real similarity numbers. Don't build anything else until this passes.

Tasks:
1. `git init`, `.gitignore` (`source_data/`, `*.mp3`, `*.wav`, `charts/*/charts/*.tgz`, `.venv/`, `__pycache__`, `spike/`), first commit with the assignment HTML and these plan files.
2. Install the toolchain: `brew install colima docker docker-buildx k3d kubectl helm uv ffmpeg jq`. Start Colima:
   `colima start --vm-type vz --vz-rosetta --cpu 8 --memory 20 --disk 120`.
   Record the exact versions in `docs/DECISIONS.md`.
3. **Spike A (model in a container):** a throwaway `spike/Dockerfile` (`--platform linux/amd64`, python 3.11-slim, ffmpeg, libsndfile). Pin a torch, fairseq2 and sonar-space combination that resolves. Start from the SONAR README compatibility table and the `sonar-space[cpu]` extra. If the fairseq2n wheel must come from the fairseq2 index for the matching torch variant, use that.
   Script: load `text_sonar_basic_encoder` and `sonar_speech_encoder_eng`, embed 3 × 10 s clips cut with ffmpeg from different episodes plus the texts "Putin", "Iran", "CIA spy", "cooking recipes". Print the cosine matrix, the load time and the per-chunk embed time.
4. **Spike B (amd64 image in k3d on arm64):** create a throwaway k3d cluster with a k3d-managed registry, push the spike image (single-platform manifest: `docker buildx build --platform linux/amd64 --provenance=false`), and run it as a pod. Confirm it runs (Rosetta binfmt reaches the k3d node containers).
5. Write down the facts: image size, weight sizes, RAM per model, seconds per chunk, and the observed similarity range (this tells us whether 0.20 is a strict or loose bar).

Done when: the cosine matrix shows topical text closer to the matching episode's clip than to the others, and the spike pod logs that matrix from inside k3d.

Fallbacks, in this order, if the spike fails:
- (a) Use Colima's built-in k3s (`colima start --kubernetes`), which shares the Docker image store, so no pull or platform issues.
- (b) Build `fairseq2n` from source for linux/arm64 (C++ and CMake, about an hour, but everything then runs natively). **CHOSEN in Phase 0** after the amd64 spike ran with no AVX and 1 thread under Rosetta. The built wheel is **committed to the repo under `wheels/`** (user decision, an exception to the no-binaries rule) with its SHA256 and the build script, so a Mac reviewer's arm64 image installs it in seconds instead of compiling. amd64 images keep using the official `fairseq2n==<ver>+cpu` wheel from Meta's index.
- (c) Develop on a cloud x86 VM. Note any choice in DECISIONS.md.

Checkpoint:
- Why can't we just use arm64 images? What is Rosetta doing here, and what would change on the interviewer's Linux x86 laptop?
- What does a "1024-d embedding" represent, and why can a text vector be compared with a speech vector at all?
- What similarity values did you actually see, and what does that imply for the 0.20 threshold?

---

### Phase 1 — Cluster and the single bring-up command (~1 h)

**Goal:** `make up` creates the cluster from nothing and is idempotent. `make down` destroys it.

Tasks:
1. `cluster/k3d.yaml`: 1 server, 0–1 agents, registry `podsearch-registry:5001`, port mappings `8080→search-api`, `8333→S3`, `8265→Ray dashboard`, `3000→Grafana`. Disable Traefik if we use NodePorts, or keep it and use Ingress (pick one and document it).
2. `Makefile` targets: `prereqs` (checks tools and versions, then prints install hints), `up`, `down`, `status`, `logs`. `up` = create cluster if missing → build images → push to the local registry → `helm upgrade --install` → `scripts/wait-ready.sh`.
3. Namespace `podsearch`, a generated S3 credentials Secret (created once and not overwritten on re-run).

Done when: `make down && make up` works twice in a row and `kubectl get nodes` is Ready.

Checkpoint: what does k3d actually run (containers acting as nodes)? Where do images come from (local registry vs `k3d image import`), and why does `imagePullPolicy` matter when you push a new image under the same tag?

---

### Phase 2 — Object store and vector DB (~1 h)

**Goal:** SeaweedFS and Qdrant are running, persistent, and reachable.

Tasks:
1. SeaweedFS: a Deployment or StatefulSet running `weed server -filer -s3 -dir=/data` with a PVC, an S3 identities config from the Secret, and a post-install Job that creates bucket `podcasts`. Readiness probe on the S3 port. Expose the **filer gRPC port** (default 18888) as a ClusterIP service for the subscriber. Uploads go to `incoming/` (`make upload` targets it).
2. **Mini-spike (30 min):** run `weed filer.meta.tail -pathPrefix=/buckets/podcasts/incoming/` in the pod, upload a file, and look at the events you get. Check for: a single create event per object? For multipart uploads, do temporary `.uploads/` paths show up? Do our own copy and delete operations also produce events? Write the answers in JOURNAL.md, because the subscriber's filters depend on them.
3. Qdrant via its official Helm chart (version pinned in `Chart.yaml`), single replica, PVC. A bootstrap step (in the API startup, idempotent) creates collection `chunks_sonar_v1`: size 1024, Cosine, payload indexes on `file_id`, `name` and `episode`.
4. `make upload FILE=…` (and `make upload-all`) uses `aws s3 cp` against `localhost:8333` with the generated creds, *or* an in-cluster toolbox pod so the host needs no aws-cli. Prefer the toolbox pod, so reviewers need no aws-cli on their host. `make load` = `make upload-all` from `DATA_DIR` (default `./source_data`).

Done when: you can upload a file, list it, delete a pod, and the file is still there after it restarts.

Checkpoint: what is a PVC/StorageClass in k3s (local-path-provisioner), and what would happen on a multi-node cluster? Why is the collection name versioned?

---

### Phase 3 — Images and shared library code (~2 h)

**Goal:** production-quality images and unit-tested core logic.

Tasks:
1. `images/ml-base` (linux/amd64): python 3.11-slim, ffmpeg, libsndfile, torch CPU, fairseq2, sonar-space, `ray[data]` (same Ray version as the RayCluster image tag), qdrant-client, boto3. Everything pinned (use `uv pip compile` to produce a lock file). Non-root user.
2. `ml-speech` and `ml-text`: `FROM ml-base`. A `RUN python -m podsearch.prefetch --model …` loads the model once so weights land in `FAIRSEQ2_CACHE_DIR=/models`. Set fairseq2 to offline or cache-only mode at runtime, so a misconfigured cache path fails loudly instead of quietly downloading GBs on every pod start.
3. `src/podsearch/audio.py`: `decode(path_or_bytes) -> np.float32[16k mono]` via ffmpeg subprocess, and `chunk(signal, sr, 10.0) -> list[Chunk(start_s, end_s, samples, rms_db)]` with the tail policy from §2.
4. `ids.py`, `config.py`, `store.py` (state markers), `vectors.py`.
5. `tests/`: chunking (exact multiple of 10 s, 0.5 s tail dropped, 7 s tail padded, stereo or 44.1 kHz input resampled, empty or corrupt input raises a typed error), ID determinism, state-marker round-trip. Run in the ml-base container: `make test`.

Done when: `make test` is green and the images build reproducibly.

Checkpoint: why two model images instead of one? How do Docker layers make that cheap (shared base layers are built, stored and pulled only once)? What happens if someone uploads a WAV at 44.1 kHz stereo?

---

### Phase 4 — Ingestion: controller, event subscription and Ray Data pipeline (~4.5 h, the heart of the system)

**Goal:** dropping a file into `incoming/` gets it indexed automatically within seconds (event path), or within `RECONCILE_INTERVAL` if the event was missed (reconcile path). Indexed files end up in `library/`. No manual step or restart.

Build order: get **reconcile-only** working end to end first (steps 1–3), then add the subscription (step 4). That way there's always a working system, and you'll understand each path separately.

Tasks:
1. KubeRay operator (official chart, version pinned) and a `RayCluster` CR: head (`num-cpus: 0` so it only coordinates, plus the dashboard), worker group `speech` with image `ml-speech`, min 1 and max 2 replicas, requests and limits sized from the Phase 0 numbers. Enable the in-tree autoscaler.
2. `ingest/pipeline.py` (the Ray job entrypoint, args = list of `incoming/<name>` keys):
   ```
   ds = ray.data.from_items(keys)
   ds = ds.flat_map(hash_decode_and_chunk)            # CPU tasks: sha256, recovery/duplicate checks, ffmpeg, 10 s chunks
                                                       # per-file errors become {"error": …} rows and never crash the job
   ds = ds.map_batches(SonarSpeechEmbedder,            # class UDF → actor pool, model loaded once per actor
                       concurrency=(1, N), batch_size=8)
   ds = ds.map_batches(QdrantWriter, batch_size=64)    # upsert(wait=True), IDs = UUIDv5(sha256, chunk_index)
   summary = per-file chunk counts and errors
   → per file, in lifecycle.py:
       success: if library/<name> exists with a DIFFERENT sha (new version under the same name), delete its old points by file_id first
                → copy to library/<name> → delete incoming/<name> → delete _state/<name>.json
       error:   _state attempts++ ; if attempts ≥ MAX_ATTEMPTS (3) → move to rejected/ + .reason.json
   ```
   Checks done before any expensive work (inside `hash_decode_and_chunk`):
   - `library/<name>` exists with the **same** sha → an earlier run crashed mid-move. Skip embedding and just finish by deleting from `incoming/`.
   - The sha is already indexed under a **different** name (Qdrant filter on `file_id`) → duplicate. Move to `rejected/` with `duplicate_of`.

   The code ships **inside the image**. No `runtime_env` pip installs at job time: they're slow, unpinned and can fail mid-job. The image is the single source of truth for dependencies.
3. `ingest/controller.py` + `sources.py` (Deployment, 1 replica, `strategy: Recreate`):
   - **Work queue:** an in-memory set of keys that are pending or in flight, so the same key arriving from both sources is only processed once. A **debounce** (e.g. wait 5 s or until 10 files arrive) batches bursts into one Ray job.
   - **Reconciler** (always on): every `RECONCILE_INTERVAL` (default 300 s, plus one run at startup to backfill), list `incoming/` and enqueue anything not already in flight. Because processed files leave `incoming/`, this list contains only the backlog.
   - **Stuck-job guard:** each submitted Ray job has a timeout. If a job dies, or the controller restarts, its keys are still in `incoming/`, so the next reconcile picks them up. No lease bookkeeping needed, because folder location *is* the state.
   - Ignore non-audio extensions (count and log once). Submit via the Ray Jobs REST API (`ray-head:8265`) and poll job status.
   - Expose `/metrics` and `/healthz`.
4. **SeaweedFS subscriber** (`EVENT_SOURCE=seaweedfs`, the default):
   - Vendor SeaweedFS's `filer.proto`. Generate Python gRPC stubs **at image build time** (generated code stays out of git, and the proto version is pinned to the SeaweedFS version).
   - Stream `SubscribeMetadata(path_prefix="/buckets/podcasts/incoming/", since_ns=<checkpoint>)`. Enqueue **create/rename-into** events for audio files only. Ignore deletes (those are our own moves), directories, and temporary multipart paths (per the Phase 2 mini-spike).
   - **Checkpoint** the last processed `ts_ns` to `_state/_subscriber.json` every N events or seconds. On restart, resume from it, so nothing is lost during short outages. If the gRPC stream drops, reconnect with exponential backoff, and log and count it.
   - Health: if the stream has been down longer than a threshold, `/healthz` stays OK but a `subscriber_connected` gauge goes to 0 and the reconciler still covers it. Degraded, not dead.
5. Edge-case behavior to implement and test (see §6 for the full matrix). Write tests for `lifecycle.py` against a real SeaweedFS (in-cluster) or `moto`.

Done when:
- `make upload-all` → files leave `incoming/` and appear in `library/` within about a minute each, and Qdrant point count ≈ 540.
- **Event path proof:** the controller log shows `source=event` and indexing starts within a few seconds of the upload.
- **Reconcile path proof:** scale the subscriber off (`EVENT_SOURCE=none`, or kill the stream), upload, and see it picked up on the next reconcile with `source=reconcile`.
- Uploading the same file again under a new name → `rejected/` as a duplicate. Uploading a fake `.mp3` → `rejected/` after 3 attempts, with a readable `.reason.json`.
- **Crash test:** kill the Ray worker mid-job, and later the controller. The file ends up in `library/` exactly once, with no duplicate points.

Checkpoint:
- Walk through the life of one uploaded file, component by component, on both the event path and the reconcile path.
- Why both? What does each path catch that the other misses? What metric tells you the event path is unhealthy? (`reconcile_found_total`: files the reconciler found that events should have delivered.)
- A "move" in S3 is copy + delete. What happens if we crash between them, and why is that safe here?
- Why is the chunk ID based on a content hash and not the key or etag?
- How does at-least-once processing combined with deterministic IDs give effectively exactly-once results?
- Why is depending on the SeaweedFS subscription a portability trade-off, and how does the design limit the damage?
- What is a Ray actor vs a task, and why is the embedder an actor?

---

### Phase 5 — Search API and UI (~2 h)

**Goal:** `GET /search?q=Iran&k=10` returns ranked hits ≥ 0.20, and a page lets you listen to each hit.

Tasks:
1. FastAPI app (image `ml-text`): load the text encoder at startup in a background thread. `/readyz` returns 503 until it's loaded (so the readiness probe gates traffic), and `/healthz` checks liveness.
2. `/search`: validate q (non-empty, ≤ 500 chars, else 400). `k` defaults to 10, max 50. Embed with `source_lang="eng_Latn"`, L2-normalize, Qdrant `search(score_threshold=0.20, limit=k, with_payload=True)`. An empty result is 200 with `hits: []`. Small LRU cache on query embeddings.
3. `/audio/{name}`: stream `library/<name>` from S3 with Range support (forward `Range` to S3 GetObject and return 206 plus `Content-Range`). This lets `<audio src="/audio/ep289-kotkin.mp3#t=120,130">` seek. If `library/` doesn't have the file yet, fall back to `incoming/<name>`, because there's a brief window between the vector upsert and the move.
4. `static/index.html`: a search box, a results list with score, episode, mm:ss and an `<audio>` player per hit, plus an optional "show below-threshold" debug toggle (handy for the write-up). Optional upload form (POST → S3 `incoming/`).
5. `/metrics`: request latency histogram, hits-per-query histogram, zero-hit counter, top-score histogram.

Done when: the UI at `http://localhost:8080` works end to end and you've clicked through results for the four required queries.

Checkpoint: why does the browser need the API to proxy audio? What does `score_threshold` do inside Qdrant vs filtering in Python? What happens during the first 60 s after an API pod restarts?

---

### Phase 6 — Results and evaluation (~1.5 h)

**Goal:** the required query results, labeled honestly.

Tasks:
1. `make eval` → `scripts/eval.py` runs `Putin`, `Ukraine`, `Middle East`, `Iran` (k=20). It writes `results/queries.md` (rank, score, episode, timestamp, link) and `results/raw.json`, and also logs the top-5 *below* threshold for context.
2. **You** listen to each hit and mark ✓/✗ with a one-line reason. This is a human judgment, so don't let the AI invent labels.
3. Write the analysis: per-query precision; expected patterns (Putin/Ukraine → ep289, Iran → ep334, Middle East likely mixed 334/310); where it fails (proper nouns are hard for a speech encoder, a short one-word query is a weak sentence embedding, topic cut at a 10 s boundary, intro or ad segments, silence).
4. Optional: a quick threshold sweep (precision at 0.15, 0.20, 0.25 and 0.30) gives you a data-backed opinion on 0.20.

Checkpoint: Are your wrong hits random or systematic? Why? What one change would help most?

---

### Phase 7 — Monitoring and operability (~2 h; toggle `MONITORING=1`)

**Goal:** you can show the system's health and the ingestion backlog on one dashboard and explain the alerts.

Tasks:
1. Install a lightweight Prometheus and Grafana (prometheus-community `prometheus` and `grafana` charts, versions pinned; skip the full kube-prometheus-stack, which is heavy on a laptop). Scrape annotations or ServiceMonitors for: search-api, controller, Ray (head and worker metrics ports), Qdrant `/metrics`, SeaweedFS metrics.
2. **Golden signals and domain metrics**:
   - Ingest: `files_discovered_total{source=event|reconcile}`, `files_indexed_total`, `files_rejected_total{reason}`, `ingest_backlog_files` (= object count in `incoming/`), **`ingest_lag_seconds`** (age of the oldest object in `incoming/`, the single most important ingestion SLI), `chunks_embedded_total`, `embed_seconds` histogram, `ray_job_duration_seconds`.
   - Event path health: `subscriber_connected` gauge, `subscriber_reconnects_total`, `subscriber_checkpoint_age_seconds`, **`reconcile_found_total`** (files the reconciler found that were *not* already queued by an event). In a healthy system this stays near 0, and if it grows, events are being lost.
   - Upload-to-searchable latency histogram (upload time → indexed time). This is the user-facing SLO.
   - Search: request rate, p50/p95/p99 latency, error rate, `zero_hit_queries_total`, `top_score` histogram (quality drift signal).
   - Infra: Qdrant points count and search latency, pod restarts, OOMKills, PVC usage.
3. Grafana dashboard JSON provisioned from `monitoring/`. Prometheus rules: IngestLagHigh (> 2 × RECONCILE_INTERVAL), EventsMissed (`increase(reconcile_found_total[1h]) > 0`), SubscriberDisconnected (> 5 min), IngestRejections, SearchP95High, ZeroHitRateHigh, QdrantDown, PodCrashLooping.
4. Logging: structured JSON logs with `name`, `file_id`, `source` (event/reconcile), `job_id` and `request_id`. Mention Loki as the next step instead of running it.
5. K8s hygiene: requests and limits everywhere, liveness, readiness and startup probes (ML pods need generous `startupProbe`s because model load takes tens of seconds), non-root, `readOnlyRootFilesystem` where possible.

Done when: uploading a file visibly moves backlog → lag → indexed on the dashboard, and killing the worker pod shows up and recovers.

Checkpoint: Which metric would page you at 3 a.m.? Why is lag better than "files processed/min"? How do you detect silent quality degradation without labels?

---

### Phase 8 — Clean-clone reproducibility (~1.5 h)

**Goal:** a reviewer on a different machine runs `git clone … && make up && make load` and gets a working system, with nothing heavy stored in git and nothing depending on "it worked on my laptop".

Tasks:
1. **Repo hygiene:** `.gitignore` covers audio, chart `.tgz` files, venvs, spike leftovers and generated stubs. Add a pre-commit `check-added-large-files` hook (e.g. 500 KB limit), so a model or mp3 can never slip into git. `git count-objects -vH` should show a repo of a few hundred KB.
2. **Pin everything that gets fetched:** base images by tag (no `latest`), Python deps by `uv` lock file, Helm deps by `Chart.lock`, the SeaweedFS proto at the same version as the SeaweedFS image. Pinning is what makes "fetch at `make up`" reproducible.
3. **`make prereqs`:** checks docker (running and with enough memory), k3d, kubectl and helm, with minimum versions, plus free disk space. Detects arm64 and checks that Rosetta/amd64 emulation is available, with a clear fix-it message. Fails fast with an install hint instead of failing 20 minutes into the build.
4. **First-run experience:** `make up` prints numbered stages with timings (cluster → charts → image build → deploy → wait-ready). The README states the measured first-run time and download size (torch plus model weights, several GB) and that re-runs are fast thanks to the Docker build cache. `make up` stays idempotent.
5. **Input data:** `make load DATA_DIR=…` (default `./source_data`). The README tells reviewers to extract the assignment's tar there. Audio isn't committed (size plus copyright).
6. **Clean-clone test:** clone the GitHub repo into a scratch dir, `make down`, delete the k3d registry, then `make prereqs && make up && make load && make smoke`, and record the time. On the Mac, also try it once with an emptied Docker build cache to measure a true cold start.
7. **Platforms:** tested on macOS arm64 (Colima + Rosetta). Linux amd64 is the native target for the ML images. Say plainly in the README what was and wasn't tested (e.g. Windows/WSL untested).
8. Optional: GitHub Actions CI with lint, unit tests, `helm lint` plus `kubeconform` on the rendered chart, and a controller image build. Optional stretch: publish prebuilt images to GHCR so `make up IMAGES=prebuilt` skips the long build.
9. README note **"Adapting for air-gapped installs"** (written up, not built; the company works air-gapped, so expect the question): vendor the charts, bundle images with `docker save`/`skopeo` plus k3s airgap images, use registry mirrors, turn off telemetry, prove it with an egress-blocked install, and use **Zarf** or **Hauler** in production. The design already helps, because weights are baked into images and nothing is pip-installed at runtime.

Done when: a fresh clone on a clean cluster reaches a working search with no manual steps beyond `make prereqs`/`up`/`load`, and the repo is small.

Checkpoint: what gets downloaded during `make up`, from where, and what pins each one? What would a reviewer on an x86 Linux laptop experience differently from you? If the company asked for an air-gapped version tomorrow, what would change?

---

### Phase 9 — README, polish, interview prep (~2 h)

1. `README.md` with the required sections: prerequisites and `make up`, architecture (diagram from §1), design choices and rejected alternatives (from §2 plus DECISIONS.md), what you learned and what was hardest (from JOURNAL.md, in your own words), the 1000× section (§7), and results (link `results/queries.md`).
2. `make smoke`: clean checkout → `make up` → `make upload-all` → wait for indexed → assert that `Iran` returns ≥ 1 hit from ep334. Run it from a fresh `git clone` into `/tmp` to prove the "clean checkout" requirement.
3. Optional CI (GitHub Actions on an ubuntu **amd64** runner, where fairseq2 is native): lint, unit tests, image build; the e2e job is optional because of model size.
4. Rehearse: explain the system in 3 minutes, then go deep on any box in the diagram.

---

## 6. Edge-case matrix (implement, test, or at least document)

| # | Scenario | Behavior | Where |
|---|---|---|---|
| 1 | Same file arrives via event **and** reconcile | In-memory queue dedupes keys. Even if it's processed twice, same sha means same IDs, so nothing changes | controller |
| 2 | Same name re-uploaded with **new content** | On success, delete the old version's points (old `file_id` from `library/` metadata), then overwrite `library/<name>` | lifecycle |
| 2b | Same content under a **different name** | Duplicate → `rejected/` with `duplicate_of` | pipeline |
| 3 | User deletes a file from `library/` | A slow "library GC" pass deletes points whose `audio_key` no longer exists (or document it as out of scope) | controller |
| 4 | Corrupt or non-audio file named `.mp3` | Decode error → `_state` attempts++ → after 3, moved to `rejected/` with `.reason.json`. Metric incremented | pipeline/lifecycle |
| 4b | Crash **between copy and delete** (move isn't atomic) | File is in both `incoming/` and `library/` → next run sees the same sha in `library/`, skips embedding and finishes the delete | pipeline |
| 4c | Subscriber down or stream dropped | Resume from the `since_ns` checkpoint on reconnect. Anything still missed is caught by reconcile within `RECONCILE_INTERVAL` | sources |
| 4d | Events from our own copy and delete operations | Subscription is scoped to the `incoming/` prefix, and delete events are ignored | sources |
| 4e | Multipart upload temporary objects | Filtered by path (see Phase 2 mini-spike). S3 listing never shows incomplete objects | sources |
| 4f | Re-index everything (model upgrade) | Files live in `library/`, not `incoming/`, so `make reindex` runs a Ray job over `library/` into a new collection `chunks_sonar_v2`, then the API switches | design |
| 4g | Upload burst (100 files in 1 s) | Debounce batches them into a few Ray jobs instead of 100 | controller |
| 5 | WAV, FLAC, M4A, stereo, 44.1/48 kHz | ffmpeg → 16 kHz mono float32 | audio.py |
| 6 | Tail shorter than 10 s | Drop if under 2 s, else zero-pad. Payload keeps the real `end_s` | audio.py |
| 7 | Silent or music-only chunks | Store `rms_db`. Optionally skip chunks below −50 dB, or down-rank them at query time | audio.py |
| 8 | Very long file (hours) | Stream-decode in segments instead of loading the whole file; per-file chunk count in state | audio.py |
| 9 | Upload in progress or multipart | S3 objects appear in a listing only once complete, so no partial reads | (S3 semantics, explain) |
| 10 | Controller crashes mid-job | Unfinished files are still in `incoming/` → startup reconcile re-queues them. Deterministic IDs mean no duplicates | controller |
| 11 | Ray worker OOM or preemption | Ray retries tasks. Actor restart. Job fails → state `failed` → retry | Ray + controller |
| 12 | Qdrant or S3 temporarily down | Backoff retries. Readiness probe fails. Ingest lag alert | all |
| 13 | Two controllers running | `replicas: 1` plus `Recreate`. Even then, idempotent writes make duplicates harmless | chart |
| 14 | Empty, huge or non-English query | 400 for empty or > 500 chars. Non-English is accepted but quality isn't guaranteed (text encoder is set to `eng_Latn`) | api |
| 15 | No hit ≥ 0.20 | 200, `hits: []`, zero-hit metric | api |
| 16 | Adjacent chunks both hit | Return as is. Improvement: merge neighbors into one segment hit | api (doc) |
| 17 | Model version changes | New collection `chunks_sonar_v2`, re-index, switch the API via config. Old and new never mix | design |
| 18 | API restarts | Readiness gates traffic until the model is loaded | chart |
| 19 | Files outside `incoming/` | Ignored. Both sources are scoped to the `incoming/` prefix | controller |

---

## 7. "1000× more audio" (README section, with numbers)

**One-line answer:** *"The design scales; the deployment and the quality assumptions don't. Here's the math showing which ones break first."* Separate the design (stages are separate, the Ray pipeline scales, writes are safely repeatable, only the backlog folder is listed) from the deployment (one laptop node, CPU-only, one replica of everything). Find the real bottleneck, change only what breaks, and say what you'd deliberately *not* change.

### 7.1 The numbers (fill in the measured seconds per chunk from Phase 0)

| | Today | ×1,000 |
|---|---|---|
| Audio duration | 1.5 h | **1,500 h** |
| Audio size (96 kbps) | ~68 MB | **~68 GB** |
| 10 s chunks | ~540 | **~540,000** |
| Raw vectors (1024 × float32) | ~2 MB | **~2.2 GB** (+ index overhead) |
| Embedding time | minutes | **540k × measured s/chunk**. At 1 s/chunk that's ≈ 150 CPU-hours, about a day on 8 cores |

Conclusion: storage and vector search are **not** the hard part. **Embedding compute is the bottleneck.** Don't quote GPU speedups as facts. Say "GPUs batch the work and are typically one to two orders of magnitude faster; I'd measure it."

### 7.2 What breaks, and what to change

| Component | Today | Breaks at ×1,000? | Change |
|---|---|---|---|
| **Embedding** | CPU, 1–2 Ray workers, fp32 | **Yes, main bottleneck** | GPU worker group in Ray, fp16, bigger batches. The autoscaler scales GPU nodes from zero on backlog and back to zero when idle (cost). Decode and chunking stay on cheap CPU nodes, since Ray Data already streams between stages. |
| **Cluster** | Single-node k3d on a laptop | Yes | Multi-node cluster with separate CPU and GPU node pools, taints and tolerations, and the Ray autoscaler wired to the cluster autoscaler. |
| **Event delivery** | Direct gRPC stream into one controller with an in-memory queue | Partly: one consumer, queue lost on restart (reconcile covers it, but slowly) | Durable broker (NATS JetStream or Kafka via SeaweedFS `notification.toml`) between SeaweedFS and the controller. Events survive outages, can be replayed, and can be shared across consumers. |
| **Controller** | 1 replica | Yes, if the arrival rate is high | Several consumers on the broker (competing consumers), or split work by key hash. Safe because writes are already safely repeatable. |
| **State** | `_state/*.json` objects in S3 | Yes: listing and writing many small objects gets slow | Postgres `files` table (status, attempts, sha, timestamps), queryable with SQL. |
| **Reconcile** | Lists `incoming/` every 5 min | Mostly fine, since `incoming/` holds only the backlog | Partition by date (`incoming/2026/10/23/…`) if even the backlog gets big. Run it less often, since it's only a safety net. |
| **Object store** | One SeaweedFS pod on a local disk | Capacity is fine, but no durability (single point of failure) | Multi-node SeaweedFS or Ceph with replication or erasure coding. Lifecycle rules (cold `library/` → cheaper storage). |
| **Qdrant** | 1 replica, float32 | Capacity is fine, but no high availability | Replication factor 2–3 (availability and read throughput). int8 scalar quantization (about 4× less RAM, re-score with the original vectors). Shard for throughput, not size. |
| **Re-indexing** (model upgrade) | Ray job over `library/` | Now a large GPU job | New versioned collection while the old one keeps serving, then switch over. |
| **Search API** | 1 pod, CPU text encoder | Only if query traffic grows too | Horizontal autoscaling on latency, query-embedding cache, GPU or a dedicated model server for the text encoder if p95 latency suffers. |
| **Ray head** | Single head | It's the job coordinator | KubeRay head fault tolerance backed by Redis, or one short-lived RayJob per large batch. |

### 7.3 Search quality (the part most candidates miss)

More data makes results **worse** at a fixed 0.20 threshold. 1,000× more chunks means many more unrelated chunks land above 0.20 by chance, so precision drops even though nothing is broken. Changes:
- Metadata filters (episode, date, show, speaker) before ranking.
- Merge adjacent hit chunks into one segment result.
- Overlapping windows (5 s steps), so topics cut at a chunk boundary aren't missed.
- Re-rank only the top 50 hits with a heavier model.
- Pick the threshold per corpus from a small labelled evaluation set (Phase 6 results are the seed), not a fixed 0.20.
- Skip silence and music chunks (loudness is already stored per chunk), which also saves compute.

### 7.4 What we would NOT change yet, and at what scale that changes

- At ×1,000: **don't** change the vector database or shard for capacity. 540k vectors is small for Qdrant. Spend the effort on GPU embedding, a durable event queue, Postgres state and high availability.
- At ×1,000,000 (half a billion chunks, about 2 TB of float32 vectors): vector storage becomes a sharding and quantization problem (binary quantization, data kept on disk), embedding becomes a large GPU fleet with cost controls, and a cheap ASR or keyword stage to pre-filter candidates becomes worth it.

**README structure for this section (Phase 9):** numbers table → bottleneck → the three architectural changes (durable broker, Postgres state, high availability) → search quality → "what I deliberately wouldn't change yet."

---

## 8. Risks and mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| fairseq2 + torch version hell | High | Phase 0 pins a working set first. Lock file. Never upgrade mid-project. |
| amd64 emulation too slow or crashing | Medium | Phase 0 measures it. Fallbacks (a)–(c) listed there. Only 540 chunks total, so even 2 s/chunk is fine. |
| Laptop RAM pressure (Ray + 2 models + Qdrant + monitoring) | Medium | Separate model images, Ray head `num-cpus: 0`, worker max 2, monitoring optional. Colima 20 GB. |
| Large images (~5–8 GB) slow every rebuild | High | Order layers so deps and weights are cached and code goes in the last layer. During dev, change only code layers. |
| SeaweedFS filer gRPC from Python (proto vendoring, stub generation, event shapes) takes longer than expected | Medium | Reconcile-only works first (Phase 4 build order), so the subscriber is additive. The Phase 2 mini-spike shows real event shapes early. Fallback: subscribe through a NATS broker configured in `notification.toml`. |
| Low cross-modal similarities, so few hits ≥ 0.20 | Medium | Phase 0 measures the real range. Report it honestly in results, since it's a strong interview talking point. |
| Slow first `make up` for reviewers (torch plus model weights, several GB, build on CPU) | High | Layer ordering so the cache works. Progress output with stage timings. README states the expected first-run time and disk space. Optional prebuilt images on GHCR (Phase 8). |
| Reviewer's machine differs (Linux x86 vs Mac) | Medium | amd64 images run natively on x86. `make prereqs` checks tools. README covers both. |

---

## 9. Time budget

| Phase | Est. |
|---|---|
| 0 Toolchain + spike | 2–3 h |
| 1 Cluster + `make up` | 1 h |
| 2 S3 + Qdrant | 1 h |
| 3 Images + core lib + tests | 2 h |
| 4 Ingestion (controller + subscription + reconcile + lifecycle + Ray) | 4.5 h |
| 5 API + UI | 2 h |
| 6 Results | 1.5 h |
| 7 Monitoring | 2 h |
| 8 Clean-clone reproducibility | 1.5 h |
| 9 README + rehearsal | 2 h |
| **Total** | **~20–21 h** (Phases 0–6 alone are a complete, submittable system, about 14 h) |
