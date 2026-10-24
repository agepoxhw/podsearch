import json

import numpy as np
import pytest
from qdrant_client import QdrantClient

from podsearch import hubness, vectors
from podsearch.config import Settings


def corpus():
    rng = np.random.default_rng(0)
    chunks = rng.normal(size=(200, 16)).astype(np.float32)
    probes = rng.normal(size=(20, 16)).astype(np.float32)
    chunks[7] = probes.mean(axis=0) * 5          # chunk 7 points at the middle of all probes: a hub
    return chunks, probes


def test_one_dot_product_equals_the_mean_cosine_to_all_probes():
    chunks, probes = corpus()
    c = hubness.centroid(probes)
    mean_cos = np.array([[v @ p / (np.linalg.norm(v) * np.linalg.norm(p)) for p in probes] for v in chunks]).mean(axis=1)
    assert np.allclose(hubness.hub_score(chunks, c), mean_cos, atol=1e-5)


def test_hub_chunk_scores_far_above_the_rest():
    chunks, probes = corpus()
    h = hubness.hub_score(chunks, hubness.centroid(probes))
    rest = np.delete(h, 7)
    assert int(h.argmax()) == 7 and h[7] > rest.mean() + 3 * rest.std()


def test_score_does_not_depend_on_the_other_chunks():
    chunks, probes = corpus()
    c = hubness.centroid(probes)
    assert np.isclose(hubness.hub_score(chunks, c)[7], hubness.hub_score(chunks[7:8], c)[0])


def centroid_file(tmp_path, **over):
    d = {"model": "m", "probe_sha": hubness.probe_sha(), "centroid": [0.5, 0.25]} | over
    f = tmp_path / "c.json"
    f.write_text(json.dumps(d))
    return f


def test_centroid_file_is_loaded_and_a_stale_one_is_refused(tmp_path):
    s = Settings(text_model="m")
    c, sha = hubness.load_centroid(s, centroid_file(tmp_path))
    assert list(c) == [0.5, 0.25] and sha == hubness.probe_sha()
    with pytest.raises(RuntimeError, match="make probes"):
        hubness.load_centroid(s, centroid_file(tmp_path, probe_sha="old"))
    with pytest.raises(RuntimeError, match="make probes"):
        hubness.load_centroid(Settings(text_model="other"), centroid_file(tmp_path))


def test_committed_centroid_matches_the_probe_list_and_model():
    c, _ = hubness.load_centroid()               # fails if someone edits PROBES without `make probes`
    assert c.shape == (1024,)


def seeded():
    chunks, probes = corpus()
    client = QdrantClient(":memory:")
    vectors.ensure_collection(client, "t", 16)
    vectors.upsert_chunks(client, "t", "f", chunks, [{"chunk_index": i, "name": "a.mp3", "episode": "a"} for i in range(200)])
    return client, chunks, hubness.centroid(probes)


def test_backfill_scores_unscored_points_and_search_can_exclude_the_hub():
    client, chunks, c = seeded()
    assert hubness.backfill(client, "t", c, "s1", page=64) == {"scored": 200}
    assert hubness.backfill(client, "t", c, "s1") == {"scored": 0}          # idempotent: nothing left to do
    payloads = [p.payload for p in client.scroll("t", limit=300)[0]]
    assert all(p["hub"] is not None and p["probe_sha"] == "s1" for p in payloads)
    cut = float(hubness.hub_score(chunks, c)[[i for i in range(200) if i != 7]].max()) + 1e-3
    q = chunks[7]
    assert vectors.search(client, "t", q, 1, -1.0)[0].payload["chunk_index"] == 7
    assert 7 not in [h.payload["chunk_index"] for h in vectors.search(client, "t", q, 200, -1.0, hub_max=cut)]


def test_backfill_rescores_points_carrying_another_probe_sha():
    client, _, c = seeded()
    hubness.backfill(client, "t", c, "old")
    assert hubness.backfill(client, "t", c, "new") == {"scored": 200}


def test_stats_reports_the_distribution_and_unscored_points():
    client, _, c = seeded()
    assert hubness.stats(client, "t", 0.0)["unscored"] == 200
    hubness.backfill(client, "t", c, "s")
    st = hubness.stats(client, "t", 0.0)
    assert st["chunks"] == 200 and st["unscored"] == 0 and st["mean_plus_3std"] > st["mean"]


def test_chunk_without_a_score_is_kept_by_the_filter():
    client = QdrantClient(":memory:")
    vectors.ensure_collection(client, "t", 4)
    vectors.upsert_chunks(client, "t", "f", np.eye(4, dtype=np.float32), [{"chunk_index": i, "name": "a.mp3", "episode": "a"} for i in range(4)])
    assert len(vectors.search(client, "t", np.ones(4), 10, -1.0, hub_max=0.1)) == 4
