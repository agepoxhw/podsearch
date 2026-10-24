# JOURNAL

Running log: what I learned, surprises, errors and fixes.

## Phase 0
- Rosetta works out of the box with Colima vz: an amd64 alpine container ran on the arm64 VM and printed x86_64.
- brew installs docker-buildx but docker does not find it until ~/.docker/config.json has cliPluginsExtraDirs.
- Surprise: sonar-space[cpu] pins torch 2.6.0 but fairseq2n 0.5.2 needs torch 2.8.0. PyPI fairseq2n is the CUDA build ("requires a CUDA 12.8 build"); the CPU wheel lives only on Meta index.
- My `|| true` in the Dockerfile hid a pip mismatch (torch 2.8 with torchaudio 2.11). Lesson: never mask install failures; use a constraints file.
- Rosetta has no AVX: torch showed "NO AVX", 1 thread, ~minutes per 10 s clip. This is why fallback (b) (native arm64 fairseq2n) was chosen.
- amd64 under Rosetta (NO AVX, 1 thread): several minutes per 10 s clip. Native arm64 (our fairseq2n wheel, 8 threads): **1.29 s per chunk**, speech model loads in 7.5 s once cached. 540 chunks is about 12 min on one worker.
- fairseq2n arm64 build: about 9 min on the Mac, wheel 3.2 MB. First attempt failed on missing `zlib1g-dev` (vendored libpng needs it). My log-watching Monitor silently missed that failure for 30 min: prefer a background command that exits, over grepping a log.
- psutil 5.9.8 (pinned by fairseq2) has no aarch64 wheel here, so pip compiles it and needs gcc. For Phase 3: builder stage, copy only site-packages to the final image.
- **Similarity findings (18 clips, 3 episodes, 6 offsets each, 8 text queries; no labels, I cannot listen):**
  - Genuine topical matches are weak: Putin->289@1020 0.32, Ukraine->289@1020 0.17, Iran->334@1020 0.15, Middle East ~0.16-0.17 spread over all episodes. Most clips score 0.0-0.15 for any query.
  - **Hub clips exist:** `310@420` scores 0.40-0.57 for *every* query (even "football match", "weather forecast"); the earlier `289@600` did too. `310@420` is also 6-8 dB quieter than the others (mean -26.8 dB, peak -8.6 dB), so it is probably non-speech or a different audio texture. (Hypothesis, not verified by listening.)
  - Text-to-text cosine "Iran" vs "cooking recipes" is already 0.52, so SONAR text vectors share a large common component.
  - Clip-to-clip cosine is low (mean 0.09, max 0.25).
  - Consequence for 0.20: real hits sit around the threshold (strict), while junk hub chunks pass it easily and outrank real hits. `rms_db` per chunk (already in the payload) is the first mitigation to try; Phase 6 should report this honestly.
- Spike B passed: arm64 image pushed to a k3d registry (`spike-registry`), pod ran on a native arm64 k3d node and logged the identical cosine matrix, 1.28 s/chunk, 8 threads. Weights were mounted from a hostPath for the spike only; the real images bake them in. Throwaway cluster deleted; docker volume `spike-models` (about 10 GB of weights) kept to avoid re-downloads during development.

## Phase 1
- k3d runs a cluster as 4 containers: the node (`server-0`), `serverlb` (nginx that forwards host ports to NodePorts), a tools container, and the registry.
- `make up` from nothing took 34 s; a re-run on a live cluster 4 s (secret unchanged); after `k3d cluster stop` it restarts the same cluster.

## Phase 2 (SeaweedFS + Qdrant)

- Learned: I wrote my own SeaweedFS StatefulSet first, and the user asked for the official chart. I should have flagged the choice before building it; the official chart exists. Switching took about 15 minutes, because the shape was the same (PVC, Secret, S3 port).
- Surprise: the official chart ignores `allInOne.enabled` for the split components. The master, volume and filer StatefulSets still rendered until I set `enabled: false` on each. Found with `helm template` before deploying.
- Surprise: `createBuckets` under top-level `s3:` does nothing in all-in-one mode. It has to be under `allInOne.s3`. Found by rendering and looking for the hook Job.
- Learned: `helm template` shows namespace `default` in service addresses. It is only a render artifact, and real installs use the release namespace.
- Error: `sed -i` with a multi-line replacement failed on macOS (BSD sed). Use the Edit tool or python for such edits.
- Error: `kubectl wait` right after `delete pod` raced with the pod being recreated (NotFound). Sleep, or wait on the rollout.
- Learned: the Qdrant image has no `curl`. Check it without a pod via `kubectl get --raw /api/v1/namespaces/podsearch/services/qdrant:6333/proxy/readyz`.
- Result: a file uploaded to SeaweedFS survives deleting the pod (PVC `local-path`). Clean `make down && make up` takes 72 s including image pulls, and the bucket is created by the hook.

## Phase 3 (images and shared library)

- Learned: fairseq2 has no offline switch and downloads a model when it is missing from its cache. In a pod that is a silent multi-GB download. We bake the weights at build time and write a marker file `/models/.baked-<kind>`; `models.load_*` refuses to run without it. Proof: both models load with `docker run --network none`, and the loader raises `ModelCacheError` when the marker is removed.
- Learned: Docker layer order is what makes two model images cheap. The weights layer is built from only `__init__.py`, `models.py` and `prefetch.py`; the rest of `src/` is copied after it. Editing code re-sends a ~37 kB layer, not 8 GB. ml-base (2.3 GB) is shared by both model images.
- Learned: ffmpeg's stereo to mono downmix scales each channel by about 0.707, so an identical-L/R tone of amplitude 0.5 comes out near 0.707 (+3 dB). Harmless for embeddings, but my first test assumed 0.5 and failed.
- Learned: `docker images` shows ml-speech as 16.6 GB, but the layers add up to about 10 GB (8 GB weights, 1.2 GB venv, 0.4 GB apt, python base). The listing size is how the image store counts, not real content. Use `docker history` for truth.
- Learned: the amd64 base builds with no CUDA libraries (`torch 2.8.0+cpu`, `fairseq2n 0.5.2+cpu`). Compressed size is 648 MB, not comparable with the 2.26 GB uncompressed arm64 figure.
- Error: I started the base build and only then noticed `moto` was missing for the S3 tests. Killed it, added `moto[s3]`, re-locked. Lesson: list the test dependencies before the first long build.
- Error: reading a build log with `cat` dumped 33 kB of progress bars. Filter with `tr '\r' '\n' | tail`.
- Result: `make test` 31 passed. A real 30 min mp3 decodes in 1.9 s into 180 chunks. `scripts/build-images.sh` builds all three, pushes them to the registry, and a second run skips all three in 0.1 s.

## Phase 4a (Ray on Kubernetes)

- Error: pinned `ray[data]`, which has no dashboard (no aiohttp etc.). The head's health endpoints and the Jobs API never started, probes failed, and the kubelet restarted every Ray pod in a loop. Fix: `ray[default,data]`. Lesson: the smoke job caught what unit tests could not.
- Error: `pyarrow` 26.0.0 needs NumPy 2 at import, but fairseq2 forces numpy 1.x. pyarrow declares no numpy dependency, so the resolver saw no conflict. Fix: pin `pyarrow==25.0.0` (tested with numpy 1.26.4; 20 to 25 all import).
- Error: `helm upgrade` fails with a server-side-apply conflict on `RayCluster.spec.workerGroupSpecs`. The autoscaler patches that list and owns it entirely (`managedFields`). Fix: `--force-conflicts`. KubeRay documents no pattern for this.
- Error: Helm installs a chart's `crds/` only on first install, not upgrade. `up.sh` now applies them (`helm show crds ... | kubectl apply --server-side`; the RayCluster CRD is too big for client-side apply).
- Error: the Docker disk filled to 96%. Each image generation is about 10 GB on the host plus copies in the registry and in the node. The kubelet evicted every pod (`DiskPressure`, threshold about 15% free) and kept the taint about 5 min after space was freed. Lesson: check `df` before long builds; a Docker prune is not instant relief for the kubelet.
- Learned: ml-base has no wget/curl, so KubeRay's default probes would fail; ours call the Ray health endpoints with Python's urllib.
- Wrong prediction: I said a cache mount would make base changes rebuild in about a minute. Measured: 489 s with 0 bytes downloaded. The download is gone, but Docker still copies, packs and hashes an 8 GB layer on one thread. Base changes stay expensive.

## Phase 4b (pipeline and lifecycle), in progress
- **KubeRay does not roll pods on a template change.** After `helm upgrade` changed the image, the operator logged `shouldDelete=false` and left the old pods running. Helm `--wait` then timed out (`generation 8, observed 7`). Fix: `up.sh` runs Helm without `--wait`, then `scripts/roll-ray.sh` deletes Ray pods whose image differs from the spec. Real-world answer for versioned rollouts: a new RayCluster and drain (`RayService` upgrade). The window where head and workers run different code is the version-skew risk the user raised.
- **The Ray head needs our code too.** The job driver imports `podsearch.ingest.pipeline`, so the head can't be plain `ml-base`. New thin image `ml-app` (= ml-base + `src/`). Head: ml-app; workers: ml-speech. Both tags hash the same `src/`.
- **Warm rebuilds are cheap.** A code-only change rebuilds three images in about 1.5 s. The 38 min stage earlier was a one-off after pruning the cache.
- **Ingestion must create the Qdrant collection itself.** The first job failed with "Collection doesn't exist" (the plan had the API create it in Phase 5). `ensure_collection` is idempotent, so pipeline and API both call it. The failure also proved the error path: attempt counted, `_state/<name>.json` readable.
- **The job's CPU ask must fit one worker.** 1 (decode) + 3 (embedder) + 0.5 (writer) = 4.5 > 4 CPUs made the autoscaler add a second worker for a 10 s clip. The writer now asks for 0 CPUs.
- A worker pod restarts once when it starts at the same time as a new head (it can't reach GCS). Benign.
- **Ray Data has its own autoscaler.** Its cluster autoscaler requests an extra worker-shaped node whenever CPU use is at least 75% (we use 3 of 4), holds the request about 180 s, and asks for more nodes than `maxReplicas`, which printed "No available node types can fulfill cluster constraint". Result: a useless second worker added and removed every ~2.5 min. Fix: `RAY_DATA_CLUSTER_SCALING_UP_UTIL_THRESHOLD=1.1` set in `pipeline.py` before importing ray.data. Real demand still scales the cluster. Verified: 6 min with no add/remove and no `request_resources`.
- **Measured (full episode, 21.5 MB mp3, 180 chunks):** speech worker about 6.2 GiB RSS and 2.9 CPUs steady, about 10 min wall time (including worker start). The placeholder 12 Gi limit is too generous. A worker pod that starts together with a new head restarts once (benign).
- Edge cases verified on the cluster: duplicate under a new name goes to `rejected/` with `duplicate_of`. A fake mp3 is rejected after exactly 3 attempts with a readable reason. Failures are rows, never job crashes.
- Gotcha: `roll-ray.sh` only compares images. A change to resources or env in the pod template does not roll running pods; they need a manual delete.

## Phase 4c/4d, and the switch to RustFS
- **Reconcile path verified** (events off): upload, the reconciler queued it with `source=reconcile`, the job ran 25 s, the file left `incoming/`. `reconcile_found_total` = 1.
- **First subscriber design crashed on start** (`DuplicateTimeseries`): the controller runs as `python -m ...controller` (`__main__`), and `subscriber.py` did `from .controller import ...`, which imported the module a second time and registered the metrics twice. Lesson: never import the entry-point module from its own helpers. The webhook handler now lives inside `controller.py`.
- **User asked to switch to RustFS** for an AWS-style notification interface. I spiked it for about 20 min before agreeing (web results said alpha and buggy, but the current chart is a stable 1.0.3). Verified: arm64 image, webhook on PUT/DELETE/multipart, prefix filter, redelivery after an outage. Event keys arrive URL-encoded (`incoming%2Fa.mp3`, spaces as `+`), so the parser uses `unquote_plus`. The webhook receiver must answer `HEAD`.
- **"Stuck" `make up`:** RustFS was not accepting connections yet when the controller started, so the first reconcile failed and the loop waited the full 300 s. `/readyz` waits for the first reconcile, so rollout waited too. Fix: after a failed loop run, retry in 5 s (`RETRY_AFTER_ERROR_S`).
- **Migration clash (one time):** Helm creates new resources before deleting old ones, so the new `rustfs-s3` NodePort 30333 collided with the old `seaweedfs-s3`. Fixed by deleting the old Service by hand. A fresh clone never sees this.
- **First event-path proof on RustFS:** upload finished 22:29:50, controller logged `file queued source=event` in the same second, job submitted 7 s later (5 s debounce).

## Phase 4 proofs on RustFS, and Phase 5 build (2026-10-24)
- **Event path:** upload logged `file queued source=event` in the same second; `reconcile_found_total` stayed 0.
- **Reconcile-only path** (`EVENT_SOURCE=none`, `RECONCILE_INTERVAL=30`): three files found by listing, batched into one job (33 s). Duplicate content under another name went to `rejected/` with a reason file without being embedded; a fake mp3 was retried on three cycles, then rejected; a real clip landed in `library/`.
- **Crash test:** killed the Ray worker and then the controller mid-job. The new controller **adopted the running job**, the job finished (126 s), the file is in `library/` once, and there are no duplicate `(name, chunk)` pairs across 577 points.
- **Fix found by reading logs earlier:** the controller used to try adopting running jobs only once at start; if Ray was rolling, it failed and never retried, so it could have started a second job. Now `tick()` retries the check every second and submits nothing until it succeeds (test added).
- **Stale test data polluted search:** the first real query returned `ep289-kotkin.wav` (a 10 s test clip from before the store switch) as the top hit, with exactly the Phase 0 benchmark score (0.539). Deleted those 3 points and all `proof-*` data. Lesson: clean test data out of the index before looking at search quality.
- **Embedder OOM:** placeholder limit 6Gi was OOMKilled 6 s after start. Measured with cgroup `memory.peak`: 6.66 GiB peak while loading, ~3.85 GiB after; warm load 5.5 s (cold was 47 s in Phase 0). The Opus review's 4-5 GiB figure was an estimate; measuring beat estimating. Set request 4Gi / limit 8Gi.
- **Decision review by a subagent** changed the design: dropped the circuit breaker (an unready embedder has no endpoints, so connections are refused at once; shedding already gives fast 503s), shortened timeouts to connect 0.5 s / read 2 s, readiness checks local things only, `Recreate` for the embedder, and a model/dimension contract on `/embed`.
- **Open observation for Phase 6:** the control query "cooking recipes" still returns hits above the 0.20 threshold (best 0.39). Cosine 0.20 does not separate unrelated queries on this model; the user's labels will show how much, and the README should say so.
- **Hub filter built** after the user spotted the same silent-looking chunk first for every query: `podsearch.hubness`, `make hubness`, `hub_z` payload, API filter, 87 tests. Verified on the real index (see DECISIONS, Hub chunks). Also: the API now sets the audio content type from the file extension (S3 held `binary/octet-stream`).
- **Hub scoring made automatic, as a Ray job (user's idea to move it into the Ray flow).** First version ran in a controller thread; the user pointed out the controller should only schedule. Now the controller submits `python -m podsearch.hubness` as a Ray job after each finished ingest job and at start, retries a failed run after 30 s, and adopts a running one after a restart. Proven with a real upload (ingest 37 s, scoring 23 s, all chunks scored). 92 tests. The spec's "without a manual step" and "clean checkout returns sensible results" were the reason: with a manual `make hubness`, a fresh `make up` would show the junk chunk first.
- **Hub scoring redesigned after an Opus review (the user asked "is it scalable, is it the right design?").** The answer was no: the controller-submitted scoring job could starve, rescanned everything after every ingest, needed about 20 GB at 1000x, and the corpus-wide z-score drifts as audio arrives. Key insight: the mean cosine to 40 probes is one dot product with their mean vector, so the probes become a 10 KB file and each chunk is scored inside the embedding stage. New cut-off 0.17 (mean + 3 std, calibrated on the 540 chunks) hides the same 8 chunks. Proven with a real upload. The controller lost all hub code. The user asked for a plain-language explainer: `docs/hubs-explained.html`.
- **Lesson:** I measured the shortcut on real data (correlation 0.9999998 with the old scores) before deleting the old path. A unit test of the algebra on random vectors alone would not have shown the real cut-off lands on the same chunks.
