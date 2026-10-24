"""Identity: a file is its content hash; a chunk is (file, position). Both are deterministic."""
import hashlib
import uuid
from pathlib import Path

# Fixed namespace so the same (file, chunk) always maps to the same Qdrant point id.
_NS = uuid.UUID("6f0a7c52-3d1e-5b8a-9c44-70d2e1a9b6f3")


def file_sha256(src: "str | Path | bytes") -> str:
    """Content hash = file_id. Keys change when we move files; content does not."""
    h = hashlib.sha256()
    if isinstance(src, bytes):
        h.update(src)
    else:
        with open(src, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
    return h.hexdigest()


def point_id(file_id: str, chunk_index: int) -> str:
    """Re-indexing the same file overwrites its points instead of duplicating them."""
    return str(uuid.uuid5(_NS, f"{file_id}:{chunk_index}"))
