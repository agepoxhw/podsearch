"""Where work comes from. Both sources end in controller.enqueue(key, source):
  * reconcile: list incoming/ (always on, the safety net)
  * event:     S3 bucket notification, delivered by the object store as a webhook POST (the fast path)

The event format is the standard S3 "Records" JSON (AWS, MinIO, Ceph, RustFS all emit it), so swapping the
object store does not change this code. Events are a speed-up, never the source of truth.
"""
from urllib.parse import unquote_plus

from ..store import INCOMING, Store

AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".mp4")


def is_audio(key: str) -> bool:
    return key.lower().endswith(AUDIO_EXTS)


def list_incoming(store: Store) -> list[tuple[str, "object"]]:
    """Everything currently in incoming/ (processed files leave it, so this is only the backlog)."""
    return [(k, t) for k, t in store.list_objects(INCOMING) if k != INCOMING]


def parse_events(payload: dict, bucket: str) -> list[tuple["str | None", str]]:
    """S3 notification JSON -> [(key, kind)]. Only an ObjectCreated under incoming/ of our bucket yields a key.
    kind is for metrics: created | removed | other."""
    out = []
    for rec in payload.get("Records", []):
        name = rec.get("eventName", "")
        s3 = rec.get("s3", {})
        if s3.get("bucket", {}).get("name") != bucket:
            out.append((None, "other"))
        elif name.startswith("s3:ObjectCreated:"):
            key = unquote_plus(s3.get("object", {}).get("key", ""))   # keys arrive URL-encoded
            out.append((key if key.startswith(INCOMING) else None, "created"))
        elif name.startswith("s3:ObjectRemoved:"):
            out.append((None, "removed"))                             # our own moves out of incoming/
        else:
            out.append((None, "other"))
    return out
