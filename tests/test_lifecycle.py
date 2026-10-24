import boto3
import pytest
from moto import mock_aws
from qdrant_client import QdrantClient

import numpy as np

from podsearch import lifecycle as L, vectors as V
from podsearch.config import Settings
from podsearch.ids import file_sha256
from podsearch.store import FileState, Store

S = Settings(collection="t", max_attempts=3)


@pytest.fixture
def store():
    with mock_aws():
        c = boto3.client("s3", region_name="us-east-1")
        c.create_bucket(Bucket="podcasts")
        yield Store(S, client=c)


@pytest.fixture
def qc():
    c = QdrantClient(":memory:")
    V.ensure_collection(c, "t", size=4)
    return c


def index(qc, name, data):
    sha = file_sha256(data)
    V.upsert_chunks(qc, "t", sha, np.ones((2, 4)), [{"chunk_index": i, "name": name, "episode": name} for i in range(2)])
    return sha


def count(qc):
    return qc.count("t").count


def test_fresh_file_proceeds(store, qc):
    store.put_bytes("incoming/a.mp3", b"x")
    assert L.precheck(store, qc, S, "a.mp3", file_sha256(b"x")).action == L.PROCEED


def test_crash_after_copy_is_finished_not_reembedded(store, qc):
    store.put_bytes("incoming/a.mp3", b"x")
    store.put_bytes("library/a.mp3", b"x")                 # copy happened, delete did not
    assert L.precheck(store, qc, S, "a.mp3", file_sha256(b"x")).action == L.FINISH_MOVE
    L.finish_move(store, "a.mp3")
    assert not store.exists("incoming/a.mp3") and store.exists("library/a.mp3")


def test_same_content_new_name_is_duplicate(store, qc):
    index(qc, "a.mp3", b"x")
    c = L.precheck(store, qc, S, "copy-of-a.mp3", file_sha256(b"x"))
    assert (c.action, c.duplicate_of) == (L.DUPLICATE, "a.mp3")


def test_indexed_but_not_moved_proceeds(store, qc):      # crash between upsert and copy: redo is idempotent
    index(qc, "a.mp3", b"x")
    assert L.precheck(store, qc, S, "a.mp3", file_sha256(b"x")).action == L.PROCEED


def test_success_moves_file_clears_state(store, qc):
    store.put_bytes("incoming/a.mp3", b"x")
    store.write_state(FileState(name="a.mp3", attempts=1))
    L.on_success(store, qc, S, "a.mp3", index(qc, "a.mp3", b"x"))
    assert store.exists("library/a.mp3") and not store.exists("incoming/a.mp3")
    assert store.read_state("a.mp3") is None


def test_new_version_under_same_name_replaces_old_points(store, qc):
    index(qc, "a.mp3", b"old")
    store.put_bytes("library/a.mp3", b"old")
    store.put_bytes("incoming/a.mp3", b"new")
    new = index(qc, "a.mp3", b"new")
    assert count(qc) == 4
    L.on_success(store, qc, S, "a.mp3", new)
    assert count(qc) == 2
    assert V.indexed_name(qc, "t", file_sha256(b"old")) is None
    assert store.get_bytes("library/a.mp3") == b"new"


def test_failures_retry_then_reject_with_reason(store):
    store.put_bytes("incoming/bad.mp3", b"junk")
    assert L.on_failure(store, S, "bad.mp3", "s", "decode error") == "retry"
    assert L.on_failure(store, S, "bad.mp3", "s", "decode error") == "retry"
    assert store.read_state("bad.mp3").attempts == 2 and store.exists("incoming/bad.mp3")
    assert L.on_failure(store, S, "bad.mp3", "s", "decode error") == "rejected"
    assert store.exists("rejected/bad.mp3") and not store.exists("incoming/bad.mp3")
    assert b"decode error" in store.get_bytes("rejected/bad.mp3.reason.json")
    assert store.read_state("bad.mp3") is None


def test_duplicate_rejection_records_original(store):
    store.put_bytes("incoming/b.mp3", b"x")
    L.reject(store, "b.mp3", "duplicate", "s", duplicate_of="a.mp3")
    assert b'"duplicate_of": "a.mp3"' in store.get_bytes("rejected/b.mp3.reason.json")
