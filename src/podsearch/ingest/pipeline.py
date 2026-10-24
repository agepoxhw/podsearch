"""The Ray job: index a list of incoming/ files.   python -m podsearch.ingest.pipeline incoming/a.mp3 ...

Ray Data streams rows through three stages, each on the hardware it needs:
  1. flat_map  hash_decode_and_chunk   CPU tasks: sha256, pre-checks, ffmpeg, 10 s chunks (1 file -> N rows)
  2. map_batches SpeechEmbedder        an ACTOR POOL: each actor loads the model once and embeds many batches
  3. map_batches QdrantWriter          upsert vectors (deterministic ids, so retries overwrite)
The driver (this process, on the Ray head) watches rows come out and finalizes each file in lifecycle.py
as soon as all of its chunks are written.

Every row has the same columns (Ray Data needs one schema). `kind` says what the row is:
  chunk | error | duplicate | finish_move | gone      (all but `chunk` are "this file is already decided")
"""
import os
import sys
from collections import defaultdict

import numpy as np

# Ray Data's own autoscaler asks for an extra node whenever CPU use >= 75% (we always use 3 of 4), which
# only makes the Ray autoscaler add an idle worker every few minutes. Real demand (an actor that needs 3
# CPUs and does not fit) still scales the cluster; this only switches off the utilization trigger.
# Must be set before ray.data is imported: the value is read at import time.
os.environ.setdefault("RAY_DATA_CLUSTER_SCALING_UP_UTIL_THRESHOLD", "1.1")
import ray  # noqa: E402

from .. import audio, lifecycle, vectors
from ..config import get_settings
from ..hubness import hub_score
from ..ids import file_sha256
from ..logs import get_logger
from ..store import INCOMING, Store

log = get_logger("pipeline")
EMPTY = b""


def _row(name, sha="", kind="chunk", n_chunks=0, idx=0, start=0.0, end=0.0, rms=0.0, samples=EMPTY, info=""):
    return {"name": name, "sha": sha, "kind": kind, "n_chunks": n_chunks, "chunk_index": idx,
            "start_s": start, "end_s": end, "rms_db": rms, "samples": samples, "info": info}


def hash_decode_and_chunk(row: dict) -> list[dict]:
    """One incoming key in, a list of rows out. Never raises: failures become rows, so one bad file
    can't kill the job for the others."""
    from botocore.exceptions import ClientError
    settings = get_settings()
    store = Store(settings)
    key = row["key"]
    name = key.removeprefix(INCOMING)
    sha = ""
    try:
        data = store.get_bytes(key)
        sha = file_sha256(data)
        check = lifecycle.precheck(store, _qdrant(), settings, name, sha)
        if check.action == lifecycle.FINISH_MOVE:
            return [_row(name, sha, "finish_move")]
        if check.action == lifecycle.DUPLICATE:
            return [_row(name, sha, "duplicate", info=check.duplicate_of)]
        chunks = audio.chunk(audio.decode(data))
        return [_row(name, sha, "chunk", len(chunks), c.index, c.start_s, c.end_s, c.rms_db,
                     c.samples.astype(np.float32).tobytes()) for c in chunks]
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return [_row(name, sha, "gone")]          # deleted between listing and now
        return [_row(name, sha, "error", info=f"S3: {e}")]
    except Exception as e:                            # AudioError, or a bug: both count as an attempt
        log.warning("file failed", extra={"file": name, "error": repr(e)})
        return [_row(name, sha, "error", info=f"{type(e).__name__}: {e}")]


def _qdrant():
    from qdrant_client import QdrantClient
    return QdrantClient(url=get_settings().qdrant_url)


class SpeechEmbedder:
    """Ray Data builds ONE instance per actor, so the 7.5 GB model loads once and serves many batches."""

    def __init__(self):
        from ..hubness import load_centroid
        from ..models import load_speech
        self.model = load_speech()
        self.hub_c, self.probe_sha = load_centroid()      # 4 KB file in the image: no text model at ingest

    def __call__(self, batch: dict) -> dict:
        is_chunk = batch["kind"] == "chunk"
        vecs = np.zeros((len(is_chunk), get_settings().vector_size), dtype=np.float32)
        idx = np.flatnonzero(is_chunk)
        if len(idx):
            import torch
            waves = [torch.from_numpy(np.frombuffer(batch["samples"][i], dtype=np.float32).copy()).unsqueeze(0)
                     for i in idx]
            vecs[idx] = self.model.predict(waves).detach().cpu().numpy()
        batch["vector"] = vecs
        batch["hub"] = hub_score(vecs, self.hub_c)        # zeros for non-chunk rows: ignored by the writer
        del batch["samples"]                          # 640 KB per chunk we no longer need
        return batch


class QdrantWriter:
    def __init__(self):
        from ..hubness import load_centroid
        self.settings = get_settings()
        self.qc = _qdrant()
        self.probe_sha = load_centroid()[1]

    def __call__(self, batch: dict) -> dict:
        written = np.zeros(len(batch["kind"]), dtype=bool)
        by_file = defaultdict(list)
        for i, (kind, sha) in enumerate(zip(batch["kind"], batch["sha"])):
            if kind == "chunk":
                by_file[sha].append(i)
        for sha, rows in by_file.items():
            payloads = [{"chunk_index": int(batch["chunk_index"][i]), "name": str(batch["name"][i]),
                         "episode": str(batch["name"][i]).rsplit(".", 1)[0],
                         "start_s": float(batch["start_s"][i]), "end_s": float(batch["end_s"][i]),
                         "rms_db": float(batch["rms_db"][i]), "hub": round(float(batch["hub"][i]), 4),
                         "probe_sha": self.probe_sha} for i in rows]
            vectors.upsert_chunks(self.qc, self.settings.collection, sha, batch["vector"][rows], payloads)
            written[rows] = True
        batch["written"] = written
        del batch["vector"], batch["hub"]
        return batch


def build_dataset(keys: list[str]):
    settings = get_settings()
    ds = ray.data.from_items([{"key": k} for k in keys])
    ds = ds.flat_map(hash_decode_and_chunk, num_cpus=1)
    ds = ds.map_batches(SpeechEmbedder, batch_size=settings.embed_batch_size,
                        concurrency=(1, settings.embed_actors_max), num_cpus=3)
    return ds.map_batches(QdrantWriter, batch_size=64, concurrency=1, num_cpus=0)   # network only: 1 decode + 3 embed = the 4 CPUs of one worker


@ray.remote(num_cpus=0.25)    # a worker's CPUs are all spoken for; a little is enough for S3 and Qdrant calls
def finalize(action: str, name: str, sha: str, info: str = "") -> str:
    """Decide the fate of one file. Runs on a worker, so the driver only coordinates.
    Every step is idempotent: if this task dies halfway, the next reconcile repeats it safely."""
    settings, store = get_settings(), Store(get_settings())
    if action == "success":
        lifecycle.on_success(store, _qdrant(), settings, name, sha)
        return "indexed"
    if action == "finish_move":
        lifecycle.finish_move(store, name)
        return "already_indexed"
    if action == "duplicate":
        lifecycle.reject(store, name, "duplicate", sha, duplicate_of=info)
        return "rejected_duplicate"
    return lifecycle.on_failure(store, settings, name, sha, info)     # "retry" or "rejected"


def run(keys: list[str]) -> dict:
    settings, qc = get_settings(), _qdrant()
    vectors.ensure_collection(qc, settings.collection, settings.vector_size)   # idempotent; the API does the same
    progress = defaultdict(int)                       # name -> chunks written so far
    pending = {}                                      # name -> ObjectRef of its finalize task

    # The driver only counts rows. When a file is decided it hands the work to a finalize task.
    for r in build_dataset(keys).iter_rows():
        name, sha, kind = r["name"], r["sha"], r["kind"]
        if kind == "gone":
            log.info("file gone", extra={"file": name})
        elif kind in ("finish_move", "duplicate", "error"):
            pending[name] = finalize.remote(kind, name, sha, r["info"])
        elif r["written"]:
            progress[name] += 1
            if progress[name] == r["n_chunks"]:
                pending[name] = finalize.remote("success", name, sha)
    results = dict(zip(pending, ray.get(list(pending.values()))))
    for name, outcome in results.items():
        log.info("file done", extra={"file": name, "outcome": outcome, "chunks": progress[name]})
    return results


if __name__ == "__main__":
    keys = sys.argv[1:]
    if not keys:
        sys.exit("usage: python -m podsearch.ingest.pipeline incoming/<name> ...")
    ray.init(address="auto", log_to_driver=True)
    log.info("job start", extra={"files": len(keys)})
    out = run(keys)
    log.info("job done", extra={"results": out})
