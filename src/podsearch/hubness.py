"""Find "hub" chunks: audio (music, jingles, noise) whose vector is close to every text query.

SONAR maps non-speech audio to a generic vector, so such chunks come out on top for any query. Each chunk gets
`hub` = its mean cosine to a fixed bank of 40 generic probe queries. That mean is ONE dot product:
    mean_k(v . p_k) = v . mean_k(p_k) = v . c        (v and p_k unit length)
so the whole bank is reduced to a single 1024-float vector `c`, computed once (`make probes`), committed as
probe_centroid.json and baked into the image. The speech workers score every chunk while embedding it, with no
text model and no second job. The score of a chunk never changes when other audio arrives. The API hides chunks
with hub > HUB_MAX (an absolute cut-off, see DECISIONS). Scores stay true cosine; no vector is changed.

`python -m podsearch.hubness probes`    print probe_centroid.json (needs the embedder; `make probes`)
`python -m podsearch.hubness backfill`  score points that lack `hub` or carry another probe_sha (`make hubness`)
`python -m podsearch.hubness stats`     distribution of `hub` in the collection, to (re)calibrate HUB_MAX
"""
import hashlib
import json
import sys
from pathlib import Path

import httpx
import numpy as np
from qdrant_client import QdrantClient, models

from .config import get_settings
from .logs import get_logger
from .vectors import ensure_collection, normalize

log = get_logger("hubness")

CENTROID_FILE = Path(__file__).parent / "probe_centroid.json"

# Deliberately unrelated to each other and to the episodes: a chunk that scores high on ALL of them is not
# about anything in particular. Changing this list changes every score: regenerate the centroid and backfill.
PROBES = (
    "banana bread", "football world cup", "quantum physics", "the weather today", "hello", "how to change a tire",
    "stock market crash", "ancient rome", "chocolate cake", "dinosaurs", "piano music", "climate change",
    "basketball", "the ocean", "space travel", "vaccine", "medieval castle", "jazz", "yoga", "electric cars",
    "the moon landing", "soup recipe", "tennis match", "volcano", "cats and dogs", "birthday party",
    "computer programming", "mountain hiking", "train station", "ice cream", "poetry", "chess", "gardening",
    "airplane", "coffee", "rain", "school", "painting", "dance", "a long walk")


def probe_sha(probes=PROBES) -> str:
    return hashlib.sha256("\n".join(probes).encode()).hexdigest()[:12]


def centroid(probe_vectors: np.ndarray) -> np.ndarray:
    """Mean of the unit-length probe vectors. Shorter than 1 on purpose: only scores are compared."""
    return normalize(probe_vectors).mean(axis=0)


def hub_score(chunks: np.ndarray, c: np.ndarray) -> np.ndarray:
    """Mean cosine of each chunk to the probes, as one dot product. Pure function: easy to test."""
    return normalize(chunks) @ c


def load_centroid(settings=None, path: Path = CENTROID_FILE) -> "tuple[np.ndarray, str]":
    """(c, probe_sha) from the committed file. Fails loudly if it does not match this code's probes or model:
    silently scoring with a stale centroid would make every `hub` meaningless."""
    s = settings or get_settings()
    d = json.loads(path.read_text())
    if d["probe_sha"] != probe_sha() or d["model"] != s.text_model:
        raise RuntimeError(f"{path.name} is for probes {d['probe_sha']} / {d['model']}, "
                           f"code has {probe_sha()} / {s.text_model}: run `make probes`")
    return np.array(d["centroid"], dtype=np.float32), d["probe_sha"]


def _embedder(s):
    http = httpx.Client(base_url=s.embedder_url, timeout=10)

    def embed(text: str):
        r = http.post("/embed", json={"text": text})
        r.raise_for_status()
        return r.json()["vector"]
    return http, embed


def build_centroid_file(embed, model: str) -> dict:
    c = centroid(np.array([embed(q) for q in PROBES], dtype=np.float32))
    return {"model": model, "probe_sha": probe_sha(), "n_probes": len(PROBES),
            "centroid": [round(float(x), 6) for x in c]}


def backfill(client: QdrantClient, collection: str, c: np.ndarray, sha: str, page: int = 256) -> dict:
    """Score existing points from their stored vectors. Streams page by page (bounded memory) and writes a
    page's payloads in one request. Needed once for data indexed before the centroid existed, and after a
    probe change. New audio never needs it: the workers score at ingest."""
    ensure_collection(client, collection)
    stale = models.Filter(should=[models.IsEmptyCondition(is_empty=models.PayloadField(key="hub")),
                                  models.FieldCondition(key="probe_sha", match=models.MatchExcept(**{"except": [sha]}))])
    done, offset = 0, None
    while True:
        points, offset = client.scroll(collection, scroll_filter=stale, limit=page, offset=offset,
                                       with_vectors=True, with_payload=False)
        if points:
            scores = hub_score(np.array([p.vector for p in points], dtype=np.float32), c)
            client.batch_update_points(collection, [
                models.SetPayloadOperation(set_payload=models.SetPayload(
                    payload={"hub": round(float(h), 4), "probe_sha": sha}, points=[p.id]))
                for p, h in zip(points, scores)], wait=True)
            done += len(points)
        if offset is None:
            return {"scored": done}


def stats(client: QdrantClient, collection: str, hub_max: float, page: int = 1000) -> dict:
    """Distribution of stored `hub` values; payload only, so memory is a list of floats."""
    hubs, offset, unscored = [], None, 0
    while True:
        points, offset = client.scroll(collection, limit=page, offset=offset, with_vectors=False, with_payload=["hub"])
        for p in points:
            if "hub" in p.payload:
                hubs.append(p.payload["hub"])
            else:
                unscored += 1
        if offset is None:
            break
    h = np.array(hubs)
    if not len(h):
        return {"chunks": 0, "unscored": unscored}
    mu, sd = float(h.mean()), float(h.std())
    return {"chunks": len(h), "unscored": unscored, "mean": round(mu, 4), "std": round(sd, 4),
            "mean_plus_3std": round(mu + 3 * sd, 4), "max": round(float(h.max()), 4),
            "hub_max": hub_max, "above_hub_max": int((h > hub_max).sum())}


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "backfill"
    s = get_settings()
    if cmd == "probes":
        http, embed = _embedder(s)
        try:
            print(json.dumps(build_centroid_file(embed, s.text_model)))
        finally:
            http.close()
        return
    client = QdrantClient(url=s.qdrant_url)
    if cmd == "backfill":
        c, sha = load_centroid(s)
        out = backfill(client, s.collection, c, sha)
    elif cmd == "stats":
        out = stats(client, s.collection, s.hub_max)
    else:
        sys.exit("usage: python -m podsearch.hubness probes|backfill|stats")
    log.info("hubness " + cmd, extra=out)
    print(out)


if __name__ == "__main__":
    main()
