"""Find "hub" chunks: audio (music, jingles, noise) whose vector is close to every text query.

SONAR maps non-speech audio to a generic vector, so such chunks come out on top for any query. We score each
chunk by its mean cosine to a fixed bank of generic probe queries, as a z-score over the corpus, and store it
as `hub_z` in the point payload. The API then excludes chunks above HUB_Z_MAX. Offline and run once after
ingest (`make hubness`): searching costs nothing extra, only one payload condition in Qdrant.
Scores stay true cosine; no vector is changed. See docs/DECISIONS.md and the README.
"""
import httpx
import numpy as np
from qdrant_client import QdrantClient, models

from .config import get_settings
from .logs import get_logger
from .vectors import ensure_collection, normalize

log = get_logger("hubness")

# Deliberately unrelated to each other and to the episodes: a chunk that scores high on ALL of them is not
# about anything in particular. Changing this list changes every hub_z, so rerun `make hubness` afterwards.
PROBES = (
    "banana bread", "football world cup", "quantum physics", "the weather today", "hello", "how to change a tire",
    "stock market crash", "ancient rome", "chocolate cake", "dinosaurs", "piano music", "climate change",
    "basketball", "the ocean", "space travel", "vaccine", "medieval castle", "jazz", "yoga", "electric cars",
    "the moon landing", "soup recipe", "tennis match", "volcano", "cats and dogs", "birthday party",
    "computer programming", "mountain hiking", "train station", "ice cream", "poetry", "chess", "gardening",
    "airplane", "coffee", "rain", "school", "painting", "dance", "a long walk")


def hub_scores(chunks: np.ndarray, probes: np.ndarray) -> np.ndarray:
    """z-score of each chunk's mean cosine to the probe queries. Pure function: easy to test."""
    mean_cos = (normalize(chunks) @ normalize(probes).T).mean(axis=1)
    return (mean_cos - mean_cos.mean()) / max(float(mean_cos.std()), 1e-9)


def run(client: QdrantClient, collection: str, embed, probes=PROBES) -> dict:
    """Score every chunk in `collection` and write hub_z into its payload. `embed(text) -> vector`."""
    ensure_collection(client, collection)
    ids, vecs, offset = [], [], None
    while True:
        points, offset = client.scroll(collection, limit=256, offset=offset, with_vectors=True, with_payload=False)
        ids += [p.id for p in points]
        vecs += [p.vector for p in points]
        if offset is None:
            break
    if not ids:
        return {"chunks": 0}
    z = hub_scores(np.array(vecs, dtype=np.float32), np.array([embed(q) for q in probes], dtype=np.float32))
    for pid, score in zip(ids, z):
        client.set_payload(collection, {"hub_z": round(float(score), 3)}, points=[pid], wait=False)
    client.set_payload(collection, {}, points=ids[:1], wait=True)   # flush: wait for the writes above
    return {"chunks": len(ids), "max_z": round(float(z.max()), 2), "z_gt_3": int((z > 3).sum()), "z_gt_5": int((z > 5).sum())}


def main() -> None:
    s = get_settings()
    http = httpx.Client(base_url=s.embedder_url, timeout=10)

    def embed(text: str):
        r = http.post("/embed", json={"text": text})
        r.raise_for_status()
        return r.json()["vector"]

    result = run(QdrantClient(url=s.qdrant_url), s.collection, embed)
    log.info("hubness scored", extra=result)
    print(result)


if __name__ == "__main__":
    main()
