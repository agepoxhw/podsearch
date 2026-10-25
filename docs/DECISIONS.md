# DECISIONS

Pinned versions and deviations from the original plan, with reasons.

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

Colima: `--vm-type vz --vz-rosetta --cpu 8 --memory 20 --disk 200` (was 120; grown in Phase 4a, see below). Verified that `docker run --platform linux/amd64 alpine uname -m` prints `x86_64`.

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
- Decision (user): commit that wheel to the repo under `wheels/`, with SHA256 and build script. This overrides the "no binaries" hard rule for this one file. Reason: Mac reviewers should not wait about an hour for a C++ build.
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

- k3d cluster `podsearch`: 1 server, 0 agents, Traefik disabled. Services are exposed by fixed **NodePorts** (30080 API, 30333 S3, 30265 Ray) which k3d's `serverlb` maps to host ports 8080, 8333, 8265 (Grafana's 3000 was removed with the skipped Phase 7). Reason: fewer moving parts than an Ingress.
- Registry `podsearch-registry`: push from host as `localhost:5001/<img>`, pods pull as `podsearch-registry:5000/<img>`. Created by k3d with the cluster and deleted with it.
- S3 credentials Secret `s3-credentials` (access_key `podsearch`, random secret_key) is generated once and never overwritten.
- `make up` = prereqs, then `scripts/up.sh` (4 timed stages). Images stage is added in Phase 3 (no images exist yet).
- Helm chart `charts/podsearch` is a placeholder (one ConfigMap) that grows each phase.

- Container runtime: Colima (Lima + Apple `vz`) with 8 CPU / 20 GB / 120 GB. Any Docker engine with >= 16 GB works (Docker Desktop, OrbStack); `make prereqs` checks the memory. `--vz-rosetta` is no longer needed for our ML images (native arm64), kept only for amd64-only third-party images. To verify in Phase 8: a clean-clone `make up` on Docker Desktop.
- Checked (start of Phase 3): Docker data (`/var/lib/docker`) is on a separate 118 GB disk (`/dev/vdb1`, 77 GB free). The 19 GB root disk holds only the OS. Image builds have room; the spike's 20 GB build cache is reclaimable (`docker builder prune`).

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

## Phase 3 decisions

| Item | Decision | Why |
|---|---|---|
| Python lock | `images/ml-base/requirements.in` plus one lock file per arch, made by `make lock` (`uv pip compile --python-platform`) | arm64 resolves `fairseq2n` to our wheel in `wheels/`; amd64 resolves to the official CPU wheel via extra indexes. One lock cannot be right for both. |
| Key pins | torch/torchaudio 2.8.0, fairseq2 and fairseq2n 0.5.2, sonar-space 0.5.0, ray[data] 2.59.0, qdrant-client 1.20.0, boto3 1.43.118, numpy 1.26.4, uv 0.13.5, moto 5.2.4 (tests only) | Pinned from Phase 0 findings; everything is exact. |
| `psutil` builder stage | The Dockerfile has a builder stage with build-essential; the runtime stage copies only `/opt/venv` | psutil 5.9.8 has no aarch64 wheel and must be compiled. The compiler stays out of the final image. |
| Short audio | A file under 2 s raises `AudioTooShortError` | Otherwise it would give zero chunks and look indexed while being unsearchable. The controller will reject it. |
| Chunk policy | 10 s windows, tail under 2 s dropped, tail 2 to 10 s zero-padded, RMS from real audio only | Keeps the vector input at a fixed length without letting padding change the loudness number. |
| Model cache | Weights baked into `/models`, `.baked-<kind>` marker, loader refuses without it | fairseq2 has no offline flag, so we enforce no runtime download ourselves. |
| Layer order | Weights layer first, then the rest of `src/` | Code edits never re-download 7+ GB. |
| Image tags | Content hash of each image's inputs, pushed to `localhost:5001`, skipped if already there. Written to `.build/images.env` | Unique pinned tags (no `latest`) so IfNotPresent works; re-runs are instant. |
| `make up` | New stage 3 "build and push images" (5 stages now) | The registry exists once the cluster exists, so it comes after stage 1. |
| Test dependency | `moto[s3]` runs S3 tests in-process; Qdrant tests use the in-memory client | Tests need no running cluster. |
| Deferred | JSON-log helper | It is first needed by the Phase 4 controller. |

## Phase 4a decisions

| Item | Decision | Why / alternatives rejected |
|---|---|---|
| KubeRay | Official chart `kuberay-operator` 1.7.1 (operator image `v1.7.1`), pinned in `Chart.lock` | Operator turns the `RayCluster` CR into pods. |
| Head image | `ml-base` (Ray, no weights); workers use `ml-speech` | Head has `num-cpus: 0` and only runs GCS, dashboard and the job driver. |
| Probes | Custom exec probes: Python `urllib` against Ray's health endpoints | Image has no wget/curl. Rejected: adding wget to ml-base (forces a full rebuild); `ray health-check` (marked not a public API, head-only). |
| Autoscaler | In-tree autoscaler on, 1 to 2 workers, 120 s idle timeout | Demand-driven (pending tasks/actors), edits the RayCluster spec; KubeRay creates the pods. |
| Helm vs autoscaler | `--force-conflicts` on `helm upgrade` | Helm takes `workerGroupSpecs` back each run; the autoscaler re-adjusts in seconds. Rejected: `--server-side=false` (turns off SSA for the whole chart); autoscaler off (the plan wants it). |
| CRDs | `up.sh` applies them with `--server-side` before Helm | Helm never upgrades CRDs. |
| Weights download | BuildKit cache mount (`id=fairseq2-<kind>`) in both model Dockerfiles | Removes the network dependency on rebuilds. Measured: base change still costs about 8 min (local 8 GB layer rebuild), 0 bytes downloaded. A separate weights image or a weights PVC would be alternatives (PVC rejected for now: "no runtime downloads" and clean-clone reproducibility). `docker builder prune` also empties the mount. |
| pyarrow | pinned 25.0.0 | See journal. |
| Disk | Done: Colima disk grown to 200 GB, `make clean-images` added, `prereqs.sh` requires 50 GB free on Docker's disk | Each image generation is about 10 GB on the host plus registry and node copies. |

### Phase 4b decisions
| Topic | Decision | Why / alternatives rejected |
|---|---|---|
| Head image | New `ml-app` (ml-base + src) for the head. Workers use ml-speech. | Driver imports our code. Rejected: weights on the head (7.5 GB unused), `runtime_env working_dir` (code not pinned to an image tag). |
| Rolling Ray pods | `scripts/roll-ray.sh`, Helm without `--wait` | KubeRay never replaces running pods after a template change. At scale: a new cluster per version (`RayService` upgrade). |
| Row schema | One schema with a `kind` column (chunk, error, duplicate, finish_move, gone), samples as bytes | Ray Data needs a uniform schema. Failures become rows, so one bad file can't kill the job. |
| Embedder actors | `EMBED_ACTORS_MAX=1`, 3 CPUs per actor | Two models need two workers, which exceed the 20 GiB VM at the placeholder limits. Revisit after measuring RSS. |
| Finalize | Runs as a `finalize` Ray task on a worker (0.25 CPU). The driver only counts chunks and dispatches. | User's suggestion. The head still imports our code to reference the task; this only moves execution. Idempotent steps make a dead task safe to repeat. |
| Ray Data autoscaler | Utilization-triggered scale-up switched off (threshold 1.1) | It added an idle second worker every few minutes. Real demand still scales. |
| Worker memory | Measured 6.2 GiB RSS. Set request 7Gi, limit 9Gi (needs a pod delete to take effect) | Two workers must fit the 19 GB VM with Qdrant, SeaweedFS and the head. |

### Phase 4c/4d decisions
| Topic | Decision | Why / alternatives rejected |
|---|---|---|
| Controller state | Two in-memory sets (pending, in flight). No database. | Folder location is the state; a restart loses nothing because unfinished files are still in `incoming/`. Rejected: Redis/Postgres queue (another stateful component for no gain). |
| One Ray job at a time | A burst is debounced (5 s or 10 files) into one job, later arrivals wait for the running job | One embedder actor means one job saturates the worker anyway. Simple and easy to reason about. |
| Restart behavior | The controller re-adopts a running job via Ray job metadata (`managed_by`, `keys`) | Avoids a second job embedding the same files after a controller crash. Idempotent pipeline would still be correct, only wasteful. |
| Stuck jobs | A job past `JOB_TIMEOUT_S` (3600) is stopped; files stay in `incoming/` and retry through reconcile | No lease bookkeeping needed. |
| `reconcile_found_total` | Counts reconciler finds the event path never announced (tracks event-seen keys still in incoming/) | A retry of a failed file must not look like a lost event. |
| Events | Only creates/renames into `incoming/` become work. Deletes (our moves), directories, nested paths and same-path updates are ignored. | Reconcile covers a real overwrite. Overwrite events untested (see journal). |
| Batch files into one job, not one job per file | The controller debounces arrivals (5 s or 10 files, `batch_max` 50) into one Ray job | (1) Fixed cost per job: a new driver and a new embedder actor that loads the speech model. In the pods that start-up is most of the gap between ~4 min of compute and ~10 min per episode, and a batch pays it once. (2) One 4-CPU worker (3 CPU embedder + 1 CPU decode) is full with one job. Several concurrent jobs would only queue, each loading its own ~6 GiB model copy, and a second worker does not fit the memory budget (`embedActorsMax: 1`). (3) Inside one job Ray Data overlaps decoding file B with embedding file A; separate jobs cannot. Costs: a wait of up to 5 s before the first file, and a late file waits for the next job (no joining a running batch). A job-level crash re-queues the whole batch, which is safe because chunk IDs are deterministic. With many workers or GPUs the answer would change: several jobs, or one long-lived streaming pipeline. |
| One embedder, one worker (`embedActorsMax: 1`) although `maxReplicas: 2` | Default stays at one speech worker | Memory is the limit, not CPU. Each embedder holds its own model copy (measured 6.2 GiB RSS, 2.9 CPUs). The Colima VM has 20 GiB (~19 usable); head, Qdrant, RustFS, controller, operator and k3s take ~3-4 GiB. Two workers need 14-18 GiB (request 7Gi, limit 9Gi each), so the second would not schedule or would hit OOM. CPU alone would allow it (2 x 3 + decode fits in 8). More cores in one worker do not help: torch scales poorly beyond ~3-4 threads on this model. To get two: `colima start --cpu 10 --memory 26` (host has 12 cores / 32 GiB, leaves ~6 GiB for macOS) and `ray.embedActorsMax: 2`; expect ~1.7x throughput (estimate, not measured). Not the default because reviewers' machines may have 16 GiB RAM; documented as a tuning option for the README. |


### Object store switched from SeaweedFS to RustFS (Phase 4, user decision after a spike)
Why: the user wanted a **standard S3 event interface**. SeaweedFS events needed its own gRPC API (vendored proto, generated stubs, `grpcio-tools`, a resume checkpoint). RustFS emits **S3 bucket notifications** (the AWS "Records" JSON) over a webhook, so the controller is store-agnostic: AWS S3, MinIO, Ceph and RustFS all work with the same code. The spec allows any S3-compatible store.

| Topic | Decision | Why / alternatives rejected |
|---|---|---|
| Store | **RustFS 1.0.3**, official chart `rustfs/rustfs` 1.0.3 (https://charts.rustfs.com), standalone mode, image `rustfs/rustfs:1.0.3` (multi-arch, arm64 verified), PVC 10Gi | Spike results (all on arm64): webhook fires on PUT, multipart (one `CompleteMultipartUpload` event), delete; prefix filter works (copies into `library/` fire nothing); events survive a receiver outage (disk queue, redelivered). Rejected: staying on SeaweedFS (works, but gRPC-specific), MinIO (upstream community edition abandoned, see PLAN section 0). |
| Event delivery | Webhook POST to `controller:8000/events` with a bearer token (Secret `webhook-token`), HEAD on the origin answered, durable queue dir `/data/notify-primary` in RustFS | Push model: no checkpoint or resume to maintain (RustFS queues and retries). Weaker than a replayable stream if RustFS loses its queue; the reconciler covers that. |
| Bucket setup | Helm post-install/upgrade Job `bucket-init` (aws-cli): create bucket, `PutBucketNotificationConfiguration` (prefix `incoming/`, `ObjectCreated:*`) | Idempotent. The same S3 API works on any store, only the ARN differs (`arn:rustfs:sqs:us-east-1:primary:webhook`). |
| Credentials | Secret `s3-credentials` now also holds `RUSTFS_ACCESS_KEY/RUSTFS_SECRET_KEY` (same values); `up.sh` adds them to older clusters | The chart reads those env names from `secret.existingSecret`. |
| Ports | In-cluster `rustfs:9000`; NodePort 30333 kept so host port 8333 and `cluster/k3d.yaml` did not change | Changing k3d port mappings would need a cluster recreate. |
| Qdrant stays on a PVC | Not on object storage | Qdrant memory-maps segments and fsyncs its WAL; it needs a POSIX filesystem. Object storage is for Qdrant **snapshots** (a possible backup add-on). |
| Removed | `proto/`, `subscriber.py`, `grpcio-tools` stage in `ml-app`, `_state/_subscriber.json`, SeaweedFS chart | |

## Phase 5 decisions (search API split from the embedder; reviewed by an Opus architect subagent)
Why split: **isolation and release cycle**, not API replica count. A model OOM or a 47 s reload must not take down the UI and `/audio`; audio streaming must not compete with torch for CPU; a UI/API change must not reload the model. Search throughput is capped by the embedder (~0.15-0.3 s/query, roughly 3-6 queries/s per replica), so more API pods do not add throughput, and memory fits only one text-model copy anyway (VM ~19 GiB usable, speech worker 7Gi, rest ~4 GiB). A single service would also work at one replica; the split is chosen for failure isolation.

| Topic | Decision | Why / alternatives rejected |
|---|---|---|
| Services | `search-api` (new slim image: fastapi, qdrant-client, boto3, httpx; no torch) and `embedder` (`ml-text` image, `POST /embed`, stdlib HTTP server like the controller) | New slim image has its own uv lock: adding fastapi to `ml-base` would rebuild every ML image (~8 min) and roll the Ray pods. |
| Sync HTTP API -> embedder | Plain request/response inside `/search` | A user waits for the answer. Rejected: Ray Serve (ingest cluster memory is full, adds the RayService CRD), gRPC (we just removed proto/stubs; a ~20 KB JSON body is nothing next to ~150 ms of compute), a queue (only adds polling). |
| Timeouts | connect 0.5 s, read 2 s. One retry on connect errors only; never retry timeouts or 503 | Rule: queue depth x encode time must stay under the read timeout, otherwise requests time out while queued and the encode work is wasted. A long timeout would pin API threads and amplify load. A cold embedder is handled by readiness (no endpoints), not by waiting. |
| Load shedding in the embedder | 1 concurrent encode, queue of 3, else `503` + `Retry-After: 1`; `OMP_NUM_THREADS` and `torch.set_num_threads` = CPU limit (2) | torch already uses all its threads, so more concurrent encodes only slow each other. Fail fast beats queueing. |
| Bulkhead | `/search` is an `async def` route with an `httpx.AsyncClient`: `Limits(max_connections=8)`, `Timeout(connect=.5, read=2, write=1, pool=.1)` | The connection pool is the bulkhead: a full pool fails within 0.1 s, so `/audio` and the UI keep working. No hand-written semaphore. |
| Circuit breaker | **Not built** (user decision after review) | An unready embedder has no Service endpoints, so connections are refused at once, and shedding already returns fast 503s. A breaker only helps against an embedder that hangs while marked ready. README: the next step once there are many API pods. |
| Readiness | API `/readyz` = local checks only (`ensure_collection` run once at startup with retries). Dependency health shows as `/search` 503s and metrics. Embedder `/readyz` = model loaded and one warm-up encode done | If every pod failed readiness on a shared dependency (Qdrant or embedder), the Service would drop them all and users would get connection refused instead of a clear JSON 503. |
| Embedder load time | Warm load 5.5 s (weights in the page cache); 47 s was a cold first load (Phase 0). startupProbe still allows 180 s | A fresh node or a cold cache can take much longer than the warm number. |
| Embedder rollout | Deployment `Recreate`, startupProbe allowing ~180 s | A rolling surge needs a second ~4-5 GiB copy, which stays Pending. Cost: about 60 s without search on each deploy; `/search` returns 503 + `Retry-After: 30`, cached queries still answer, `/audio` and UI keep working. |
| Model/collection contract | `/embed` returns `{vector, model, dim}`; the API checks `dim == 1024` and `model` equals the one configured for `chunks_sonar_v1`, else refuses to search. Cache key includes the model | A model change must go with a new collection name, never mix vector spaces silently. |
| Query cache | LRU in the API pod, ~1024 entries, key = model + whitespace-trimmed query, hit/miss metrics | A repeated query never reaches the embedder. At scale: a shared cache (Redis), not built. |
| Replicas and resources | API: 2 replicas (shows it is stateless), 100m/128Mi request, 500m/256Mi limit. Embedder: 1 replica, request 1 CPU/4Gi, limit 2 CPU/8Gi, **measured** (cgroup `memory.peak`): 6.66 GiB peak while loading, ~3.85 GiB after. The first guess (limit 6Gi, from an estimate) was OOMKilled 6 s after start, because the limit has to cover the load peak, not the steady state. No HPA | No memory to scale the embedder into; API scaling does not raise throughput. |
| Validation | `q` non-empty and at most 500 characters, enforced in both services | The embedder must not trust its caller. |

### Hub chunks (found by the user in the UI, Phase 5)
| Topic | Decision | Why / alternatives rejected |
|---|---|---|
| Problem | Chunks of non-speech audio (music/jingles/noise; the worst ones are the quietest in the index) get a "generic" SONAR vector close to every text query, so they were the top hit for any query, even "asdfgh". Measured with 40 unrelated probe queries: two chunks averaged ~0.40 cosine (z = 7.7 and 7.3) while a normal chunk averages 0.03 | Chunk-to-chunk similarity did not reveal them (the problem is text-to-audio), and RMS loudness alone is not a usable rule (they are quiet but not silent). |
| Chosen fix: hub score at ingest | Each chunk gets `hub` = its mean cosine to 40 fixed generic probe queries. That mean is one dot product, `v . c` with `c` = the mean of the 40 unit probe vectors (checked on the real index: correlation 0.9999998 with the 40-comparison version). `c` is one 1024-float vector, generated once by `make probes` through the embedder and committed as `src/podsearch/probe_centroid.json` (10 KB, with the model name and a hash of the probe list), baked into the image. The `SpeechEmbedder` stage loads it and stores `hub` and `probe_sha` next to each vector. The API passes `must_not hub > HUB_MAX (0.17)` to Qdrant; the debug checkbox shows everything, with `hub` | Design review by an Opus subagent. Per chunk and stateless: a score never changes when other audio arrives, there is no unscored window, no second job, no scroll of all vectors and no text model at ingest. Qdrant applies the filter before `limit` and `score_threshold`, so hubs take no result slots. Scores stay true cosine and no vector changes (the 0.20 rule holds). A chunk with no `hub` is kept. Rejected: CSLS-style score correction (changes what a score means), RMS cut-off (not reliable), neighbour-based hubness (corpus-dependent, needs a kNN graph) |
| Cut-off 0.17 | Calibrated once as mean + 3 std of `hub` over the 540 chunks (mean 0.0303, std 0.0477, so 0.1734), rounded to 0.17. The same 8 chunks as the old z > 3 rule are hidden. It is an API setting (`HUB_MAX`), so changing it needs no rewrite of data. `make hubness-stats` shows the distribution | A z-score over the whole corpus drifts: a batch of music-heavy episodes raises the std and lowers every z, so hubs come back without a code change. A frozen mean and std is just a rescaling, so the raw value is stored and the cut-off lives in config |
| Result | 8 of 540 chunks (1.5%) are above the cut-off. "world cup" returns nothing, "Iran" is unchanged. Proven on the real cluster: a 30 s clip cut from a hub region, uploaded, ingested in one job, its chunks had `hub` 0.44, 0.22, -0.01 and `probe_sha` set, with no manual step | Cut-off was chosen by looking at three episodes; chunks near it may be real speech. The Phase 6 labels say whether it is right |
| Probe list is a versioned artifact | `load_centroid()` refuses to run if the file's probe hash or model differs from the code, so the workers fail loudly instead of scoring with a stale centroid. After a change: `make probes`, rebuild, then `make hubness` (a backfill that scores points whose `hub` is missing or has another `probe_sha`, page by page) | Silent drift of every score is the failure to avoid. A model change re-embeds everything anyway |
| Controller is a pure ingest scheduler | The first version had the controller submit a scoring Ray job after each ingest job (kind `hubness`). Removed after the review: scoring only started when nothing was pending, so under continuous arrivals it never ran and new hubs stayed unfiltered; each run rescanned the whole corpus (O(N^2) total work) and at 1000x would need about 20 GB on the Ray head | Replaced by scoring inside the Ray Data pipeline. Backfill is the only separate job and is manual and rare |
| Known limits | The cut-off is calibrated on 3 episodes. Chunks of non-speech that are not "generic" (for example noise or near-silence with a distinct vector) may get a low score | Speech detection at ingest is the root-cause fix, see below. A periodic sample-based job to watch the score distribution is the 1000x follow-up |
| Not done: speech detection at ingest (VAD) | Written up in the README as the root-cause fix | It would stop non-speech chunks from ever being indexed, but needs a VAD library in the speech image and a re-ingest of everything, and it would not catch other odd chunks. |


## Phase 8 decisions (clean-clone reproducibility)
| Topic | Decision | Why |
|---|---|---|
| Size guard | `.githooks/pre-commit` refuses files over 500 KB (except `wheels/fairseq2n-*.whl`); `make hooks` enables it (`git config core.hooksPath .githooks`) | Plain shell, so reviewers need no extra tool. Rejected: the `pre-commit` Python framework (one more dependency for a ten-line check). Tested with a 585 KB file. |
| Pin audit | No `latest` anywhere. Images by tag; Helm sub-charts by `Chart.lock`; Python by per-arch lock files | Base image `python:3.11-slim-bookworm` is a tag, not a digest, so a rebuild months later could pick up a newer patch release. Digest pinning is the next step. apt packages (ffmpeg, compiler) float on purpose: exact apt versions disappear from the Debian mirror after a security update, so pins would break builds within weeks; a digest alone does not help because `apt-get update` fetches the current index. Strict reproducibility needs the base by digest plus a dated snapshot.debian.org mirror (also what an air-gapped install needs). Impact of floating: a new ffmpeg patch release could shift decoded samples slightly. |
| `make smoke` | `scripts/smoke.sh`: uploads the audio if it is not in the bucket yet, waits until every file has moved to `library/`, then asserts that `Iran` returns at least one ep334 hit | One command proves the whole path from audio to search. Needs `source_data/` (not committed). |
| Clean-clone test | The tracked and new files are copied to a scratch dir (what a clone would contain), then `make down`, registry deleted, `make prereqs && make up && make load && make smoke` | See JOURNAL for the timings. The Docker build cache was warm; a cold build is the 38 min measured in Phase 4b. |
| Not done | CI, prebuilt images on GHCR, air-gapped install | Air-gapped is a README note only. |

## Review fixes (after an Opus review of the whole repo)
| Topic | Decision | Why |
|---|---|---|
| Shell quoting | The controller quotes every S3 key with `shlex.quote` in the Ray job entrypoint | Ray runs the entrypoint through a shell. A space split `my episode.mp3` into two keys (never indexed, retried forever); an apostrophe failed the job; a crafted name could run code. Test added. |
| Lost jobs | A 404 from the Ray Jobs API releases the job; an unreachable Ray releases it after `JOB_TIMEOUT_S` | A restarted Ray head forgets its job records, and the controller used to wait forever (no new audio would be indexed). Two tests added. |
| Network exposure | All host ports (8080, 8333, 8265, 6443) and the registry bind to 127.0.0.1; the k3s image is pinned; the unused Grafana port is gone | The Ray Jobs API (code execution) and the registry have no authentication. |
| Prereqs | Disk check runs `df` in a container instead of bind-mounting Docker's data dir (fails on Docker Desktop); CPU (6+), python3 and shasum are checked | Found by review, not yet tested on Docker Desktop. |
| Known and not fixed | A file that kills the whole job is retried forever (attempts count only for errors inside the decode step); whole files are decoded in RAM (a 10 h file would hit the 9 Gi worker limit); no CPU limits on third-party pods; no hashes in the lock files | Documented for the interview. At scale: count attempts per key in the controller when a job fails, stream the decode with ranged S3 reads. |
