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
