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


@lru_cache
def get_settings() -> Settings:
    return Settings()
