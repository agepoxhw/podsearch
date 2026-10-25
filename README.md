# Podcast semantic search on local Kubernetes

Search podcast audio by meaning, with no transcript at any point. Audio goes into an S3 bucket, is cut into
10-second chunks, embedded with Meta's SONAR speech encoder and stored in a vector database. A text query is
embedded with SONAR's text encoder into the same vector space, and the API returns the chunks with cosine
similarity of at least 0.20, with a link to play each one.

## Prerequisites and bring-up

- Docker (with the `buildx` plugin, which Docker Desktop includes) with at least 16 GB of memory and 50 GB free disk.
  macOS with Homebrew: `brew install colima docker docker-buildx k3d kubernetes-cli helm`, then
  `mkdir -p ~/.docker/cli-plugins && ln -sfn "$(brew --prefix)/opt/docker-buildx/bin/docker-buildx" ~/.docker/cli-plugins/docker-buildx`
  and `colima start --vm-type vz --cpu 8 --memory 20 --disk 200`
- `k3d` 5.6+, `kubectl` 1.28+, `helm` 3.12+, `openssl`, `curl`, `python3`
- The assignment's audio: extract the tar into `./source_data/` (the audio is not in git)

```
git clone https://github.com/agepoxhw/podsearch.git && cd podsearch
make up       # prereqs check, cluster, images, deploy, wait until ready
make load     # upload source_data/*.mp3 to the bucket; ingestion starts by itself
make smoke    # wait until indexed, then assert that "Iran" returns a hit from ep334
```

Then open http://localhost:8080 (search UI and API: `/search?q=Iran`). Ray dashboard: http://localhost:8265.
`make down` removes everything. Every target has a one-line comment in the `Makefile`.

**Time and size.** Measured on a clean Mac (Colima VM, Docker images and build cache all deleted, then a fresh
`git clone` from GitHub): `make up` 11 min 12 s (cluster 28 s, images 9 min 12 s, deploy 44 s, wait 41 s), then
indexing the three episodes (1.5 h of audio, 540 chunks) 27 min, whole run 38 min 32 s. The image stage includes
downloading torch and the two SONAR models (about 10 GB of images), so it depends on your network. The speech
image alone is about 10 GB, and you need about 40 GB of free disk. All downloads are pinned. Re-runs of
`make up` with a warm cache take about 2 min.

**Tested on:** macOS arm64 (Colima, Apple `vz`). The ML images also build for linux/amd64 (official CPU wheel),
but I did not run the cluster on x86 Linux. Windows/WSL is untested.

## Architecture

```
                     host ports: 8080 API/UI   8333 S3   8265 Ray dashboard
 upload ──► RustFS (S3) ──webhook "object created in incoming/"──► controller ──submit──► Ray job (KubeRay)
              │  incoming/ ─► library/ (or rejected/)              ▲ reconcile: list incoming/ every 5 min
              │                                                    │ (safety net for missed events)
              │            Ray Data pipeline on the speech worker:
              │            decode (ffmpeg, 16 kHz mono) ─► 10 s chunks ─► SONAR speech encoder ─► hub score
              │                                                                      │
              │                                                                      ▼
              └── /audio/<file> (range requests) ◄── search-api ◄── Qdrant (collection chunks_sonar_v1, cosine)
                                                         │
                                                         └─ embedder (SONAR text encoder, /embed)
```

- **RustFS**: S3-compatible store. Folders are the state: `incoming/` is the backlog, `library/` is done, `rejected/` holds bad files with a reason.
- **controller**: receives S3 bucket notifications by webhook, plus a periodic listing of `incoming/`. It debounces bursts into one Ray job and adopts a running job after a restart. It only schedules; it never touches audio.
- **Ray on KubeRay**: a head (scheduler, no models) and a speech worker (model baked into the image). The pipeline is Ray Data: decode, embed, write.
- **Qdrant**: one collection, true cosine. Chunk ids are deterministic, so re-running a file never duplicates points.
- **search-api** and **embedder** are separate deployments: a model OOM or reload must not take down the UI and audio streaming.

Everything runs in the namespace `podsearch`. Every container has requests and limits and runs as non-root, and
every service has probes, JSON logs and `/healthz`, `/readyz` and `/metrics`.

## Design choices, and what I rejected

Full table with reasons: `docs/DECISIONS.md`. The ones I would defend first:

| Choice | Rejected | Why |
|---|---|---|
| S3 bucket notifications (webhook) plus a reconciler | polling only; a vendor-specific gRPC stream | New audio is picked up in seconds, any S3 store works, and a lost event is caught by the listing. |
| Folder as state (`incoming/` to `library/`), idempotent writes | Redis or Postgres queue | A restart loses nothing, and one fewer stateful component. |
| One batched Ray job per burst | one job per file | Each job pays a model load; one worker is full with one job anyway. |
| Search API and embedder split | one service | Failure isolation and separate release cycles. Throughput is capped by the embedder either way. |
| Hub score computed per chunk at ingest | corpus-wide z-score job after each ingest | Stateless, never starves, no rescan. See the search quality section. |
| Weights baked into images, exact pins, no runtime downloads | downloading in pods | Reproducible, and works offline once images are built. |
| Native arm64 images with a self-built `fairseq2n` wheel (3 MB, in `wheels/`) | amd64 under Rosetta | Rosetta has no AVX: minutes per 10 s clip instead of 1.3 s. |
| Qdrant on a PVC | vectors in object storage | Qdrant needs a real filesystem. Object storage is for snapshots. |

**Adapting for air-gapped installs (written up, not built).** Vendor the Helm charts, bundle the images with
`docker save` or `skopeo` and k3s air-gap images, use registry mirrors, turn off telemetry, and prove it with an
egress-blocked install. In production use Zarf or Hauler. The design already helps: weights are baked into images
and nothing is installed at runtime.

## Results

The four required queries are in `results/queries.md` (every hit listened to and marked correct or wrong by me).
Summary: 40 of 55 hits correct. Details and the analysis are in `results/analysis.md`.

## What I learned, and what was hardest

**The hardest part was making SONAR fast enough on a Mac, and then trusting its results.**

- *Speed.* Under Rosetta the amd64 build has no AVX and runs on one thread, so a 10 s clip took minutes. I built
  `fairseq2n` for arm64 myself (about 9 min, a 3 MB wheel that I committed under `wheels/`) and got 1.3 s per chunk.
- *Quality.* The same non-speech chunk came out on top for every query, even nonsense, because SONAR gives such
  audio a generic vector that is close to every text query. I measured it and built a hub score. A design review
  then showed that my first version would starve and rescan the whole corpus, so I rebuilt it to score each chunk
  at ingest. I also learned that the 0.20 threshold alone does not reject unrelated queries on this model.

**What surprised me**

- Kubernetes operators have gaps. KubeRay does not replace running pods when the template changes, and Helm fights
  the Ray autoscaler over the same field (I use `--force-conflicts`). Ray Data also has its own autoscaler, which
  added an idle worker every few minutes until I switched its trigger off.
- A memory limit has to cover the peak, not the steady state. The embedder was OOM-killed while loading the model
  at a limit that was fine afterwards. Measuring beat estimating.
- A small end-to-end job found bugs that unit tests could not: a missing dashboard extra made every Ray probe fail.
- Silent fallbacks hide problems. `|| true` hid a pip version mismatch, and a log-watching monitor missed a failed build.

**What I would do differently.** I would ask about the object store and the event interface before building, and
I would get a design review sooner. Both of my big rewrites (SeaweedFS to RustFS, and the hub scoring job) came from that.

## At 1000x the audio

Today: 1.5 h of audio, 540 chunks, about 12 min to embed on one CPU worker (1.3 s per chunk). At 1000x:
1,500 h, 540,000 chunks, about 190 CPU-worker-hours: about 8 days on one worker, about 2 h on 100.

| What | Breaks? | Change |
|---|---|---|
| Embedding | Yes, the bottleneck | GPU worker group (fp16, bigger batches) scaled from zero by the Ray autoscaler plus a cluster autoscaler. I would measure the speed-up, not assume it. |
| Cluster | Yes | Multi-node, separate CPU and GPU pools. Today one 20 GB VM fits one speech worker. |
| Events | Partly | RustFS keeps an on-disk retry queue, but a durable broker (NATS JetStream or Kafka) would allow replay and several consumers. |
| Controller and state | Only at a high arrival rate | Several consumers, and a Postgres table for per-file status instead of S3 objects. |
| Ray head | Single point of failure | Head fault tolerance with Redis, or a short-lived RayJob per large batch. |
| Object store | No durability | Multi-node, replicated store. |
| Qdrant | Capacity is fine (540k vectors is about 2 GB) | Replication for availability, int8 quantization to cut RAM, Snapshots to RustFS as backup. |
| Ray upgrades | Version skew | KubeRay does not roll pods on a template change (I roll them by script). Use a new cluster per version (`RayService` upgrade). |
| Search API | Only if traffic grows | Autoscale, shared query cache, and a circuit breaker once there are many API pods. |
| Hub filter | Scales: it is one dot product per chunk | The cut-off was calibrated on 3 episodes; add a periodic job that watches the score distribution. |

I would not change the vector database at 1000x. Spend the effort on GPU embedding, a durable queue, state in
Postgres and high availability.

**More data makes quality worse at a fixed 0.20.** With 1000x more chunks, many more unrelated chunks land above
0.20 by chance. Mitigations: metadata filters, merging adjacent hits into one segment, scoring over neighbouring
chunks, re-ranking the top 50, and choosing the threshold per corpus from a labelled set (my labels are the seed).

## Search quality: hub chunks, and what I would do next

**What happened.** Searching the first three episodes, the same 10-second chunk came out on top for every query,
even nonsense. It is audio with no words (music or a jingle). SONAR's speech encoder gives such audio a
generic vector that sits close to every text query, so it matches everything. I measured it: two chunks
averaged about 0.40 cosine against 40 unrelated queries, while a normal chunk averages 0.03.

**What I did (the hub filter).** Each chunk gets a hub score: its average similarity to 40 unrelated queries
("banana bread", "chess", ...). Normal speech is about something, so it scores low; a hub scores high. The
average over 40 queries is one dot product with their mean vector, so that mean is computed once (`make probes`),
committed as a 10 KB file, and used by the speech workers to score every chunk while it is embedded. The API
hides chunks above 0.17 (mean + 3 standard deviations, calibrated on the first 540 chunks). The score depends only
on the chunk, never on other audio, so nothing is rescanned or rewritten when new audio arrives. No vector
is changed, so scores stay true cosine. `docs/hubs-explained.html` explains this with charts.
The hub filter returns a subset of the hits that pass 0.20: hubs are hidden, and `below_threshold=true` (a debug
opt-in) shows everything including them.

**At 1000x the audio.** Hub filtering scales because it is per-chunk and stateless: one 1024-dim dot product per
chunk, computed inside the Ray Data embedding stage and stored with the vector. My first design (a corpus-wide
z-score recomputed after every ingest, scrolling all vectors into one driver and rewriting every point) would
need about 20 GB of driver memory, take tens of minutes, block ingest, and starve under continuous arrivals,
leaving new hubs unfiltered. A periodic sample-based job to watch the score distribution (not to rewrite it)
is the follow-up.

**Caveat: the cut-off is calibrated on three episodes.** The hub score of a chunk never depends on other audio
(it uses 40 fixed probe queries), but the cut-off 0.17 was chosen from the first 540 chunks. Hubs score about 0.40
and normal speech about 0.03, so the gap is wide and the cut-off is not fragile, but I have only seen it on three
episodes. With other shows, languages or recording quality the distribution may shift. Retuning is cheap:
`HUB_MAX` is an API setting applied at query time, so it needs no re-ingest, and `make hubness stats` prints the
current distribution. What is missing is a periodic job that watches that distribution and alerts on drift.
The filter also cannot remove speech that is merely near a topic: the recurring wrong hits in `results/` score
below the cut-off.

**What I would do with more time (speech detection).** The root cause is that non-speech audio gets indexed at
all. A voice-activity detector in the ingest step would drop chunks without speech before they are embedded, so
they never reach the index. It needs a detector in the speech image and a re-ingest of all audio, which is why
it is not in this version. The hub filter would stay as a safety net for odd chunks the detector misses.

**Result quality.** Over the four required queries 40 of 55 hits are correct (73%). Putin 90%, Iran 81%,
Ukraine 58%, Middle East 29%. The wrong hits are systematic: five general-geopolitics chunks cause 13 of the 15
errors, and none of them mention the query word. The one change I would make is averaging scores over neighbouring
chunks (a hit needs support from the 20-30 s around it); it is untested. Details: `results/analysis.md`.

**Related finding.** On this model the 0.20 cosine threshold alone does not reject unrelated queries: before
the filter, most nonsense queries still returned hits above 0.20.
