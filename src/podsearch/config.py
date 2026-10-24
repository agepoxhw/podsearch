"""All configuration comes from environment variables (set by the Helm chart). No config files."""
from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    s3_endpoint: str = "http://rustfs:9000"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket: str = "podcasts"

    qdrant_url: str = "http://qdrant:6333"
    collection: str = "chunks_sonar_v1"      # versioned: a new model means a new collection
    vector_size: int = 1024

    score_threshold: float = 0.20            # true cosine, see CLAUDE.md
    hub_z_max: float = 3.0                   # chunks scored above this by `make hubness` are hidden from search
    max_attempts: int = 3                    # failures before a file moves to rejected/

    ray_address: str = "http://ray-head-svc:8265"   # Ray Jobs REST API
    reconcile_interval: int = 300            # seconds between full listings of incoming/
    debounce_s: float = 5.0                  # wait this long (or for debounce_files) to batch a burst into one job
    debounce_files: int = 10
    batch_max: int = 50                      # files per Ray job
    job_timeout_s: int = 3600                # a job running longer than this is stopped; its files get retried
    event_source: str = "webhook"            # webhook (S3 bucket notifications) | none
    webhook_token: str = ""                  # bearer token the object store sends with each event
    http_port: int = 8000                    # /healthz /readyz /metrics

    text_model: str = "text_sonar_basic_encoder"   # the embedder's model; the API refuses a mismatch (see DECISIONS)
    max_query_chars: int = 500
    embedder_url: str = "http://embedder:8000"     # search-api -> embedder (synchronous)
    embed_concurrency: int = 1               # encodes running at once in the embedder (torch already uses all threads)
    embed_queue: int = 3                     # requests allowed to wait; beyond that the embedder answers 503
    embed_threads: int = 2                   # torch threads in the embedder = its CPU limit

    embed_actors_max: int = 1                # Ray Data actor pool upper bound (each actor = one loaded model)
    embed_batch_size: int = 8                # chunks per model call


@lru_cache
def get_settings() -> Settings:
    return Settings()
