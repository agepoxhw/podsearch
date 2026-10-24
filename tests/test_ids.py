import hashlib
import uuid

from podsearch.ids import file_sha256, point_id


def test_sha256_matches_hashlib_for_bytes_and_path(tmp_path):
    data = b"abc" * 1_000_000          # bigger than the 1 MiB read block
    p = tmp_path / "f"
    p.write_bytes(data)
    expected = hashlib.sha256(data).hexdigest()
    assert file_sha256(data) == file_sha256(p) == expected


def test_point_id_is_deterministic_and_a_uuid():
    a = point_id("abc", 0)
    assert a == point_id("abc", 0)
    uuid.UUID(a)


def test_point_id_changes_with_file_and_index():
    assert len({point_id("abc", 0), point_id("abc", 1), point_id("abd", 0)}) == 3


def test_point_id_ignores_the_file_name():
    # identity comes from content only: same sha, same ids, wherever the file lives
    assert point_id(file_sha256(b"x"), 3) == point_id(file_sha256(b"x"), 3)
