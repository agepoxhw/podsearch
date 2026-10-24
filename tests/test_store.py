import boto3
import pytest
from moto import mock_aws

from podsearch.config import Settings
from podsearch.store import FileState, Store


@pytest.fixture
def store():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="podcasts")
        yield Store(Settings(), client=client)


def test_state_round_trip(store):
    store.write_state(FileState(name="ep1.mp3", sha256="abc", status="failed", attempts=2, last_error="boom"))
    got = store.read_state("ep1.mp3")
    assert (got.name, got.sha256, got.status, got.attempts, got.last_error) == ("ep1.mp3", "abc", "failed", 2, "boom")
    assert got.updated_at                        # stamped on write


def test_missing_state_is_none(store):
    assert store.read_state("never-seen.mp3") is None


def test_state_is_a_plain_s3_object(store):
    store.write_state(FileState(name="x.mp3"))
    assert store.list_keys("_state/") == ["_state/x.mp3.json"]


def test_move_copies_then_deletes(store):
    store.put_bytes("incoming/a.mp3", b"audio")
    store.move("incoming/a.mp3", "library/a.mp3")
    assert not store.exists("incoming/a.mp3")
    assert store.get_bytes("library/a.mp3") == b"audio"


def test_exists_and_list_keys_are_prefix_scoped(store):
    store.put_bytes("incoming/a", b"1")
    store.put_bytes("library/b", b"2")
    assert store.exists("incoming/a") and not store.exists("incoming/zzz")
    assert store.list_keys("incoming/") == ["incoming/a"]
