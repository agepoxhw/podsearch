"""The search API: /search, /audio/{name} (S3 range proxy), the static UI, /healthz /readyz /metrics.

Light on purpose (no torch): the query embedding comes from the separate embedder service, see
embed_client.py and docs/DECISIONS.md (Phase 5). Run:
    uvicorn --factory podsearch.api.app:create_app --host 0.0.0.0 --port 8000
"""
import mimetypes
import threading
import time
from contextlib import asynccontextmanager
from collections import OrderedDict
from pathlib import Path

from botocore.exceptions import ClientError
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from qdrant_client import QdrantClient
from starlette.concurrency import run_in_threadpool

from .. import vectors
from ..config import Settings, get_settings
from ..ingest.sources import is_audio
from ..logs import get_logger
from ..store import INCOMING, LIBRARY, Store
from .embed_client import EmbedClient, EmbedContractError, EmbedUnavailable

log = get_logger("api")

REQUESTS = Counter("search_requests_total", "GET /search by HTTP status", ["code"])
SECONDS = Histogram("search_seconds", "End-to-end /search latency", buckets=(.05, .1, .2, .3, .5, 1, 2, 5))
HITS = Histogram("search_hits", "Hits returned per query", buckets=(0, 1, 2, 5, 10, 20, 50))
ZERO_HITS = Counter("zero_hit_queries_total", "Queries that returned no hit above the threshold")
TOP_SCORE = Histogram("search_top_score", "Cosine score of the best hit", buckets=(.1, .2, .3, .4, .5, .6, .7, .8, .9, 1))
EMBED_ERRORS = Counter("embed_client_errors_total", "Failed calls to the embedder", ["kind"])
CACHE_HITS = Counter("query_cache_hits_total", "Query embeddings served from the LRU")
CACHE_MISSES = Counter("query_cache_misses_total", "Query embeddings that needed the embedder")
STATIC = Path(__file__).parent / "static"


class LRU:
    """Small query-embedding cache. Only touched from the event loop thread, so no lock."""

    def __init__(self, size: int):
        self.size, self.d = size, OrderedDict()

    def get(self, key):
        if key in self.d:
            self.d.move_to_end(key)
            return self.d[key]

    def put(self, key, value):
        self.d[key] = value
        self.d.move_to_end(key)
        while len(self.d) > self.size:
            self.d.popitem(last=False)


def _err(code: int, msg: str, retry_after: int = 0) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=code,
                        headers={"Retry-After": str(retry_after)} if retry_after else None)


def _init_collection(qdrant: QdrantClient, s: Settings) -> None:
    """Run once at startup, retrying: Qdrant may not be up yet. /readyz does not wait for it (see DECISIONS)."""
    delay = 1.0
    while True:
        try:
            vectors.ensure_collection(qdrant, s.collection, s.vector_size)
            log.info("collection ready", extra={"collection": s.collection})
            return
        except Exception as e:
            log.warning("qdrant not ready, retrying", extra={"error": repr(e)})
            time.sleep(delay)
            delay = min(delay * 2, 10)


def create_app(settings: "Settings | None" = None, qdrant: "QdrantClient | None" = None,
               store: "Store | None" = None, embed: "EmbedClient | None" = None,
               init_collection: bool = True) -> FastAPI:
    s = settings or get_settings()
    qdrant = qdrant or QdrantClient(url=s.qdrant_url)
    store = store or Store(s)
    embed = embed or EmbedClient(s.embedder_url, s.text_model, s.vector_size)
    cache = LRU(1024)

    @asynccontextmanager
    async def lifespan(_):
        yield
        await embed.close()

    app = FastAPI(title="podsearch", docs_url=None, redoc_url=None, lifespan=lifespan)

    if init_collection:
        threading.Thread(target=_init_collection, args=(qdrant, s), daemon=True, name="init-collection").start()

    # ---- probes and metrics: local checks only -------------------------------------------------
    @app.get("/healthz")
    def healthz():
        return Response("ok", media_type="text/plain")

    @app.get("/readyz")
    def readyz():
        return Response("ok", media_type="text/plain")

    @app.get("/metrics")
    def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    # ---- search ---------------------------------------------------------------------------------
    @app.get("/search")
    async def search(request: Request):
        t0 = time.time()
        resp = await _search(request.query_params)
        REQUESTS.labels(str(resp.status_code)).inc()
        SECONDS.observe(time.time() - t0)
        return resp

    async def _search(params) -> JSONResponse:
        q = (params.get("q") or "").strip()
        if not q or len(q) > s.max_query_chars:
            return _err(400, f"q must be 1..{s.max_query_chars} characters")
        try:
            k = int(params.get("k", 10))
        except ValueError:
            return _err(400, "k must be an integer")
        if not 1 <= k <= 50:
            return _err(400, "k must be between 1 and 50")
        below = params.get("below_threshold", "").lower() in ("1", "true", "yes")
        threshold = -1.0 if below else s.score_threshold      # debug view: show everything, cosine is >= -1

        key = (s.text_model, q)
        vec, cached = cache.get(key), True
        if vec is None:
            cached = False
            CACHE_MISSES.inc()
            try:
                vec = await embed.embed(q)
            except EmbedUnavailable as e:
                EMBED_ERRORS.labels(e.kind).inc()
                log.warning("embedder unavailable", extra={"kind": e.kind})
                return _err(503, f"embedder unavailable ({e.kind})", e.retry_after)
            except EmbedContractError as e:
                EMBED_ERRORS.labels("contract").inc()
                log.error("embedder contract mismatch", extra={"error": str(e)})
                return _err(502, "embedder does not match the index (model or vector size)")
            cache.put(key, vec)
        else:
            CACHE_HITS.inc()

        try:
            hub_cut = None if below else s.hub_z_max                 # the debug view shows everything, hubs included
            points = await run_in_threadpool(vectors.search, qdrant, s.collection, vec, k, threshold, hub_cut)
        except Exception as e:
            log.warning("qdrant query failed", extra={"error": repr(e)})
            return _err(503, "vector store unavailable", 5)

        hits = [{"score": round(p.score, 4), "name": p.payload["name"], "episode": p.payload["episode"],
                 "chunk_index": p.payload["chunk_index"], "start_s": p.payload["start_s"],
                 "end_s": p.payload["end_s"], "hub_z": p.payload.get("hub_z"), "audio_url": f"/audio/{p.payload['name']}"} for p in points]
        HITS.observe(len(hits))
        if hits:
            TOP_SCORE.observe(hits[0]["score"])
        elif not below:
            ZERO_HITS.inc()
        log.info("search", extra={"q": q, "k": k, "hits": len(hits), "cached": cached,
                                  "top": hits[0]["score"] if hits else None})
        return JSONResponse({"query": q, "k": k, "threshold": threshold, "model": s.text_model, "hits": hits})

    # ---- audio: S3 range proxy ------------------------------------------------------------------
    @app.get("/audio/{name}")
    def audio(name: str, request: Request):
        """Forward Range to S3 so <audio> can seek. library/ first, then incoming/ (the brief window between
        the vector upsert and the file move)."""
        if "/" in name or ".." in name or not is_audio(name):
            return _err(404, "not found")
        extra = {"Range": request.headers["range"]} if "range" in request.headers else {}
        for prefix in (LIBRARY, INCOMING):
            try:
                obj = store.s3.get_object(Bucket=store.bucket, Key=prefix + name, **extra)
                break
            except ClientError as e:
                code = e.response["Error"]["Code"]
                if code == "InvalidRange":
                    return _err(416, "range not satisfiable")
                if code not in ("NoSuchKey", "404", "NotFound"):
                    log.warning("s3 error", extra={"error": repr(e)})
                    return _err(503, "object store unavailable", 5)
        else:
            return _err(404, "not found")
        headers = {"Accept-Ranges": "bytes", "Content-Length": str(obj["ContentLength"])}
        if "ContentRange" in obj:
            headers["Content-Range"] = obj["ContentRange"]
        # Content type from the extension: objects uploaded with the CLI are stored as binary/octet-stream.
        return StreamingResponse(obj["Body"].iter_chunks(64 * 1024), status_code=206 if extra and "ContentRange" in obj else 200,
                                 media_type=mimetypes.guess_type(name)[0] or "audio/mpeg", headers=headers)

    return app
