import numpy as np
from qdrant_client import QdrantClient

from podsearch import hubness, vectors


def corpus():
    rng = np.random.default_rng(0)
    chunks = rng.normal(size=(200, 16)).astype(np.float32)
    probes = rng.normal(size=(20, 16)).astype(np.float32)
    chunks[7] = probes.mean(axis=0) * 5          # chunk 7 points at the middle of all probes: a hub
    return chunks, probes


def test_hub_chunk_gets_the_highest_z_and_the_rest_stay_ordinary():
    chunks, probes = corpus()
    z = hubness.hub_scores(chunks, probes)
    assert int(z.argmax()) == 7 and z[7] > 3
    assert abs(float(z.mean())) < 1e-6 and (z > 3).sum() == 1


def test_identical_chunks_do_not_divide_by_zero():
    z = hubness.hub_scores(np.ones((4, 3), dtype=np.float32), np.ones((2, 3), dtype=np.float32))
    assert np.isfinite(z).all()


def test_run_writes_hub_z_to_every_point_and_search_can_exclude_the_hub():
    chunks, probes = corpus()
    client = QdrantClient(":memory:")
    vectors.ensure_collection(client, "t", 16)
    vectors.upsert_chunks(client, "t", "f", chunks, [{"chunk_index": i, "name": "a.mp3", "episode": "a"} for i in range(200)])
    lookup = {f"probe{i}": p for i, p in enumerate(probes)}
    out = hubness.run(client, "t", lambda q: lookup[q], probes=tuple(lookup))
    assert out["chunks"] == 200 and out["z_gt_3"] == 1
    payloads = [p.payload for p in client.scroll("t", limit=300)[0]]
    assert all("hub_z" in p for p in payloads)
    q = chunks[7]                                          # the hub is its own best match
    assert vectors.search(client, "t", q, 1, -1.0)[0].payload["chunk_index"] == 7
    assert 7 not in [h.payload["chunk_index"] for h in vectors.search(client, "t", q, 200, -1.0, hub_z_max=3.0)]


def test_chunk_without_a_score_is_kept_by_the_filter():
    client = QdrantClient(":memory:")
    vectors.ensure_collection(client, "t", 4)
    vectors.upsert_chunks(client, "t", "f", np.eye(4, dtype=np.float32), [{"chunk_index": i, "name": "a.mp3", "episode": "a"} for i in range(4)])
    assert len(vectors.search(client, "t", np.ones(4), 10, -1.0, hub_z_max=3.0)) == 4
