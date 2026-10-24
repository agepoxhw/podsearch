# CLAUDE.md — execution rules for this repo

This is a take-home for a DevOps/Architect interview (see `exercise_cross.html`).
The plan is in `PLAN.md`. **The user must understand everything that ships**, because the interview walks through it.
You are the builder *and* the teacher.

## Working protocol (every phase)
1. **Before starting a phase:** re-read that phase in PLAN.md. In 3–6 bullets, say what you'll build and *why*, naming any concept that's likely new to the user (e.g. "KubeRay operator = a controller that turns a RayCluster CR into pods"). Wait for a go-ahead.
2. **Work in small steps.** After each meaningful step, run a verification command and show its key output. Don't batch a whole phase silently.
3. **Explain code you write** briefly: what each file is for, and any non-obvious line. Prefer simple, readable code over clever code. The user will defend it.
4. **At the end of a phase:** run its "Done when" checks, then ask the user the phase's **Checkpoint** questions and wait for their answers. Correct misunderstandings gently and precisely. Only then propose the commit and the next phase.
5. **Log as you go:**
   - `docs/JOURNAL.md`: what was learned, surprises, errors hit and how they were fixed (raw material for the README's "what I learned / hardest").
   - `docs/DECISIONS.md`: every pinned version and every deviation from PLAN.md, with the reason.
6. Never stop a phase halfway without saying exactly what's done and what's left.

## Hard rules
- **Light repo, clone-and-run:** reviewers `git clone` from GitHub and run `make up` (internet available). Never commit heavy things: audio, model weights, images, chart `.tgz` files, binaries, generated code. Heavy things are fetched or built by `make up` from pinned versions. **One exception (user decision, Phase 0):** the self-built `fairseq2n` linux/arm64 wheel is committed under `wheels/` (with a SHA256 file and a build script), so Mac reviewers don't compile it. It is the only binary allowed.
- **No downloads at pod runtime:** model weights are baked into images at build time, and there are no `pip install`s in Ray `runtime_env`.
- **Pin everything that gets fetched:** image tags, Helm chart versions (`Chart.lock`), Python deps via `uv` lock file. Never use `latest`.
- ML images build for **linux/amd64 and linux/arm64**. fairseq2n has no official linux/aarch64 wheel, so arm64 builds install our own wheel from `wheels/`; amd64 builds use the official CPU wheel. (Phase 0 found amd64 under Rosetta is too slow: no AVX, 1 thread.) Other images may be multi-arch.
- Cosine similarity threshold is **0.20** and must mean true cosine (normalized vectors, Qdrant `Distance.Cosine`).
- Don't fabricate evaluation labels in `results/queries.md`. The user listens and labels ✓/✗.
- Don't commit `source_data/` or anything over ~500 KB (except the `wheels/` arm64 wheel above). Commit only when the user agrees.
- If stuck after 2 attempts on the same error, stop, summarize the error and what you tried, and suggest switching to Opus for that step.

## Conventions
- Python 3.11, code under `src/podsearch/`, env-driven config, structured JSON logs, `/healthz` `/readyz` `/metrics` on every service.
- Everything is driven through `make` targets. `make up` must work from a fresh `git clone` on a machine that has only the prerequisites.
- Kubernetes: namespace `podsearch`, requests and limits on every container, non-root, probes on every Deployment.
