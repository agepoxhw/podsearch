"""All configuration comes from environment variables (set by the Helm chart). No config files."""
from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    s3_endpoint: str = "http://seaweedfs-s3:8333"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket: str = "podcasts"

    qdrant_url: str = "http://qdrant:6333"
    collection: str = "chunks_sonar_v1"      # versioned: a new model means a new collection
    vector_size: int = 1024

    score_threshold: float = 0.20            # true cosine, see CLAUDE.md
    max_attempts: int = 3                    # failures before a file moves to rejected/

    embed_actors_max: int = 1                # Ray Data actor pool upper bound (each actor = one loaded model)
    embed_batch_size: int = 8                # chunks per model call


@lru_cache
def get_settings() -> Settings:
    return Settings()
