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
