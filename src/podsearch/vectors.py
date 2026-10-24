"""Vector helpers: L2 normalization and the Qdrant collection (create, upsert, delete, search)."""
import numpy as np
from qdrant_client import QdrantClient, models

from .ids import point_id


def normalize(v: np.ndarray) -> np.ndarray:
    """L2-normalize rows. Qdrant's Cosine also normalizes on insert, but we normalize the query
    ourselves so the score we report is unambiguously a cosine similarity."""
    v = np.asarray(v, dtype=np.float32)
    norm = np.linalg.norm(v, axis=-1, keepdims=True)
    return v / np.maximum(norm, 1e-12)


def ensure_collection(client: QdrantClient, name: str, size: int = 1024) -> None:
    """Idempotent: safe to call on every API start."""
    if not client.collection_exists(name):
        client.create_collection(
            name, vectors_config=models.VectorParams(size=size, distance=models.Distance.COSINE))
    for field in ("file_id", "name", "episode"):   # filter fields: delete-by-file, search-in-episode
        client.create_payload_index(name, field, models.PayloadSchemaType.KEYWORD)


def upsert_chunks(client: QdrantClient, name: str, file_id: str, vectors: np.ndarray,
                  payloads: list[dict]) -> None:
    """Point id = uuid5(file_id, chunk_index), so re-running a file overwrites its own points."""
    points = [models.PointStruct(id=point_id(file_id, p["chunk_index"]), vector=v.tolist(),
                                 payload={**p, "file_id": file_id})
              for v, p in zip(normalize(vectors), payloads)]
    client.upsert(name, points=points, wait=True)   # wait=True: durable before we move the file


def delete_file(client: QdrantClient, name: str, file_id: str) -> None:
    client.delete(name, points_selector=models.FilterSelector(filter=models.Filter(
        must=[models.FieldCondition(key="file_id", match=models.MatchValue(value=file_id))])), wait=True)


def search(client: QdrantClient, name: str, query_vec: np.ndarray, k: int, threshold: float):
    return client.query_points(name, query=normalize(query_vec).tolist(), limit=k,
                               score_threshold=threshold, with_payload=True).points
