"""What happens to a file around indexing. No Ray and no models here, so it is testable with moto.

Folder = state:  incoming/ -> library/ (indexed) or rejected/ (gave up).  `_state/<name>.json` counts attempts.
Every step is safe to repeat, so a crash anywhere just means the next reconcile runs the file again.
"""
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from qdrant_client import QdrantClient

from . import vectors
from .config import Settings
from .ids import file_sha256
from .store import INCOMING, LIBRARY, REJECTED, STATE as STATE_PREFIX, FileState, Store

PROCEED, FINISH_MOVE, DUPLICATE = "proceed", "finish_move", "duplicate"


@dataclass(frozen=True)
class Check:
    action: str                  # PROCEED | FINISH_MOVE | DUPLICATE
    duplicate_of: str = ""


def precheck(store: Store, qc: QdrantClient, settings: Settings, name: str, sha: str) -> Check:
    """Cheap checks before any ffmpeg or model work."""
    if store.exists(f"{LIBRARY}{name}") and file_sha256(store.get_bytes(f"{LIBRARY}{name}")) == sha:
        return Check(FINISH_MOVE)       # an earlier run crashed between "copy" and "delete incoming"
    other = vectors.indexed_name(qc, settings.collection, sha)
    if other and other != name:
        return Check(DUPLICATE, duplicate_of=other)
    return Check(PROCEED)               # includes: same name + same sha indexed but not moved yet (re-embed is idempotent)


def finish_move(store: Store, name: str) -> None:
    store.delete(f"{INCOMING}{name}")
    store.delete(f"{STATE_PREFIX}{name}.json")


def on_success(store: Store, qc: QdrantClient, settings: Settings, name: str, sha: str) -> None:
    """Called after the vectors are durably in Qdrant. Order matters: old points out, copy, delete source."""
    vectors.delete_other_versions(qc, settings.collection, name, keep_file_id=sha)
    store.move(f"{INCOMING}{name}", f"{LIBRARY}{name}")
    store.delete(f"{STATE_PREFIX}{name}.json")


def reject(store: Store, name: str, reason: str, sha: str = "", **extra) -> None:
    """Park the file in rejected/ with a human-readable <name>.reason.json next to it."""
    store.move(f"{INCOMING}{name}", f"{REJECTED}{name}")
    store.put_bytes(f"{REJECTED}{name}.reason.json", json.dumps(
        {"name": name, "sha256": sha, "reason": reason, **extra,
         "rejected_at": datetime.now(timezone.utc).isoformat()}, indent=2).encode())
    store.delete(f"{STATE_PREFIX}{name}.json")


def on_failure(store: Store, settings: Settings, name: str, sha: str, error: str) -> str:
    """Count the attempt. Returns "retry" (stays in incoming/) or "rejected"."""
    state = store.read_state(name) or FileState(name=name)
    state.sha256, state.attempts, state.last_error, state.status = sha, state.attempts + 1, error, "failed"
    if state.attempts >= settings.max_attempts:
        reject(store, name, error, sha, attempts=state.attempts)
        return "rejected"
    store.write_state(state)
    return "retry"
