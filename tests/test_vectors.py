import numpy as np
import pytest
from qdrant_client import QdrantClient

from podsearch import vectors as V


@pytest.fixture
def qc():
    c = QdrantClient(":memory:")
    V.ensure_collection(c, "t", size=4)
    return c


def payloads(n):
    return [{"chunk_index": i, "name": "a.mp3", "episode": "ep1"} for i in range(n)]


def test_normalize_gives_unit_vectors_and_survives_zero():
    out = V.normalize(np.array([[3.0, 4.0], [0.0, 0.0]]))
    assert np.allclose(np.linalg.norm(out[0]), 1.0)
    assert not np.isnan(out).any()


def test_ensure_collection_is_idempotent(qc):
    V.ensure_collection(qc, "t", size=4)         # second call must not raise
    assert qc.collection_exists("t")


def test_search_returns_cosine_and_applies_threshold(qc):
    vecs = np.array([[1, 0, 0, 0], [0, 1, 0, 0], [1, 1, 0, 0]], dtype=np.float32)
    V.upsert_chunks(qc, "t", "fid", vecs, payloads(3))
    hits = V.search(qc, "t", np.array([2.0, 0, 0, 0]), k=5, threshold=0.20)   # unnormalized query
    by_idx = {h.payload["chunk_index"]: h.score for h in hits}
    assert by_idx[0] == pytest.approx(1.0, abs=1e-5)
    assert by_idx[2] == pytest.approx(np.sqrt(0.5), abs=1e-5)
    assert 1 not in by_idx                       # orthogonal: cosine 0 < 0.20


def test_reupsert_overwrites_instead_of_duplicating(qc):
    vecs = np.random.default_rng(0).normal(size=(3, 4)).astype(np.float32)
    V.upsert_chunks(qc, "t", "fid", vecs, payloads(3))
    V.upsert_chunks(qc, "t", "fid", vecs, payloads(3))
    assert qc.count("t").count == 3


def test_delete_file_removes_only_that_file(qc):
    vecs = np.eye(4, dtype=np.float32)[:2]
    V.upsert_chunks(qc, "t", "f1", vecs, payloads(2))
    V.upsert_chunks(qc, "t", "f2", vecs, payloads(2))
    V.delete_file(qc, "t", "f1")
    assert qc.count("t").count == 2
