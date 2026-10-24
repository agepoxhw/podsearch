"""API tests: real FastAPI app, in-memory Qdrant, moto S3, and a fake embedder behind httpx.MockTransport.
Runs in the search-api test image (scripts/test.sh), because only that image has fastapi."""
import boto3
import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws
from qdrant_client import QdrantClient

from podsearch import vectors
from podsearch.ids import point_id
from podsearch.ids import point_id
from podsearch.api import app as A
from podsearch.api.embed_client import EmbedClient
from podsearch.config import Settings
from podsearch.store import Store

DIM = 4
S = Settings(vector_size=DIM, collection="t", text_model="m", score_threshold=0.2, s3_bucket="podcasts", hub_max=0.3)
AUDIO = bytes(range(100))


class Embedder:
    """Fake embedder: maps a query to a vector, counts calls, can be told to fail."""
    VECS = {"iran": [1, 0, 0, 0], "cooking": [0, 0, 0, 1], "mixed": [1, 1, 0, 0]}

    def __init__(self):
        self.calls, self.fail = 0, None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.fail:
            if isinstance(self.fail, Exception):
                raise self.fail
            return self.fail
        text = request.read().decode()
        key = next((k for k in self.VECS if k in text), "cooking")
        return httpx.Response(200, json={"vector": self.VECS[key], "model": "m", "dim": DIM})


@pytest.fixture
def env():
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="podcasts")
        s3.put_object(Bucket="podcasts", Key="library/ep1.mp3", Body=AUDIO, ContentType="binary/octet-stream")   # what the CLI stores
        s3.put_object(Bucket="podcasts", Key="incoming/new.mp3", Body=AUDIO)
        q = QdrantClient(":memory:")
        vectors.ensure_collection(q, "t", DIM)
        vectors.upsert_chunks(q, "t", "f1", np.array([[1, 0, 0, 0], [1, 1, 0, 0], [0, 1, 0, 0]], dtype=np.float32), [
            {"chunk_index": i, "name": "ep1.mp3", "episode": "ep1", "start_s": i * 10.0, "end_s": i * 10.0 + 10} for i in range(3)])
        emb = Embedder()
        client = EmbedClient("http://embedder", "m", DIM, transport=httpx.MockTransport(emb))
        tc = TestClient(A.create_app(S, q, Store(S, s3), client, init_collection=False))
        tc.qdrant = q
        yield tc, emb


def test_search_ranks_by_cosine_and_applies_threshold(env):
    c, _ = env
    r = c.get("/search", params={"q": "iran"})
    hits = r.json()["hits"]
    assert r.status_code == 200
    assert [h["chunk_index"] for h in hits] == [0, 1]            # chunk 2 is orthogonal (cosine 0 < 0.2)
    assert hits[0]["score"] == pytest.approx(1.0) and hits[1]["score"] == pytest.approx(0.7071, abs=1e-3)
    assert hits[0]["audio_url"] == "/audio/ep1.mp3" and hits[1]["start_s"] == 10.0


def test_no_hit_is_200_with_empty_list(env):
    c, _ = env
    r = c.get("/search", params={"q": "cooking"})                # only chunk 2 has a y component: [0,1,0,0] vs [0,0,0,1] = 0
    assert r.status_code == 200 and r.json()["hits"] == []


def test_below_threshold_debug_shows_everything(env):
    c, _ = env
    assert len(c.get("/search", params={"q": "cooking", "below_threshold": "true"}).json()["hits"]) == 3


@pytest.mark.parametrize("params", [{"q": ""}, {"q": "  "}, {"q": "x" * 501}, {"q": "a", "k": 0}, {"q": "a", "k": 51}, {"q": "a", "k": "x"}, {}])
def test_bad_input_is_400_and_never_reaches_the_embedder(env, params):
    c, emb = env
    assert c.get("/search", params=params).status_code == 400
    assert emb.calls == 0


def test_hub_chunks_are_hidden_unless_debug_and_do_not_use_up_result_slots(env):
    c, _ = env
    c.qdrant.set_payload("t", {"hub": 0.5}, points=[point_id("f1", 0)])        # chunk 0 is the best match for "iran"
    r = c.get("/search", params={"q": "iran", "k": 1}).json()
    assert [h["chunk_index"] for h in r["hits"]] == [1]                           # the hub is skipped, k=1 is still filled
    dbg = c.get("/search", params={"q": "iran", "k": 1, "below_threshold": "true"}).json()
    assert dbg["hits"][0]["chunk_index"] == 0 and dbg["hits"][0]["hub"] == 0.5  # debug shows hubs, with their score


def test_repeated_query_is_served_from_the_cache(env):
    c, emb = env
    c.get("/search", params={"q": "iran"}); c.get("/search", params={"q": "  iran "})
    assert emb.calls == 1


def test_embedder_overloaded_is_503_with_its_retry_after_and_not_retried(env):
    c, emb = env
    emb.fail = httpx.Response(503, headers={"Retry-After": "1"}, json={"error": "overloaded"})
    r = c.get("/search", params={"q": "iran"})
    assert r.status_code == 503 and r.headers["Retry-After"] == "1" and emb.calls == 1


def test_embedder_down_retries_connect_once_then_503(env):
    c, emb = env
    emb.fail = httpx.ConnectError("refused")
    r = c.get("/search", params={"q": "iran"})
    assert r.status_code == 503 and r.headers["Retry-After"] == "30" and emb.calls == 2


def test_embedder_timeout_is_not_retried(env):
    c, emb = env
    emb.fail = httpx.ReadTimeout("slow")
    assert c.get("/search", params={"q": "iran"}).status_code == 503 and emb.calls == 1


def test_wrong_model_from_embedder_is_refused(env):
    c, emb = env
    emb.fail = httpx.Response(200, json={"vector": [1, 0, 0, 0], "model": "other", "dim": DIM})
    assert c.get("/search", params={"q": "iran"}).status_code == 502


def test_audio_without_range_is_whole_file(env):
    c, _ = env
    r = c.get("/audio/ep1.mp3")
    assert r.status_code == 200 and r.content == AUDIO and r.headers["Accept-Ranges"] == "bytes"
    assert r.headers["content-type"] == "audio/mpeg"


def test_audio_range_is_206_with_content_range(env):
    c, _ = env
    r = c.get("/audio/ep1.mp3", headers={"Range": "bytes=10-19"})
    assert r.status_code == 206 and r.content == AUDIO[10:20] and r.headers["Content-Range"] == "bytes 10-19/100"


def test_audio_falls_back_to_incoming(env):
    c, _ = env
    assert c.get("/audio/new.mp3").content == AUDIO


def test_audio_missing_traversal_and_bad_range(env):
    c, _ = env
    assert c.get("/audio/nope.mp3").status_code == 404
    assert c.get("/audio/..%2Fsecret.mp3").status_code == 404
    assert c.get("/audio/notes.txt").status_code == 404
    assert c.get("/audio/ep1.mp3", headers={"Range": "bytes=500-600"}).status_code == 416


def test_probes_metrics_and_ui(env):
    c, _ = env
    assert c.get("/healthz").status_code == 200 and c.get("/readyz").status_code == 200
    assert "search_requests_total" in c.get("/metrics").text
    page = c.get("/")
    assert page.status_code == 200 and 'createElement("audio")' in page.text   # the player is built per hit in JS
