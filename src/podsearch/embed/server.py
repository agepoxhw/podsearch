"""The query embedder: POST /embed {"text": ...} -> {"vector": [...1024 floats...], "model": ..., "dim": 1024}.

A separate service from the search API so the API stays light and keeps serving the UI and /audio while the
model loads, restarts or is overloaded. Overload policy (see docs/DECISIONS.md): at most `embed_concurrency`
encodes run at once, `embed_queue` more may wait, anything beyond that gets an immediate 503 + Retry-After.
Failing fast is better than queueing: the caller would time out anyway and the encode would be wasted work.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from ..config import Settings, get_settings
from ..logs import get_logger
from ..vectors import normalize

log = get_logger("embedder")

REQUESTS = Counter("embed_requests_total", "POST /embed calls by HTTP status", ["code"])
SHED = Counter("embed_shed_total", "Requests rejected because the queue was full")
INFLIGHT = Gauge("embed_inflight", "Requests running or waiting for the model")
SECONDS = Histogram("embed_seconds", "Time in the model per request",
                    buckets=(.05, .1, .2, .3, .5, 1, 2, 5))
LOAD_SECONDS = Gauge("model_load_seconds", "Seconds the model took to load and warm up")
NOT_READY_RETRY_S, SHED_RETRY_S = 30, 1


class Embedder:
    """Owns the model, the load thread and the admission control. `encode` is injectable for tests."""

    def __init__(self, settings: Settings, encode=None):
        self.s = settings
        self.encode = encode                      # fn(text) -> 1-D vector; set by the load thread in production
        self.ready = False
        self.admit = threading.BoundedSemaphore(settings.embed_concurrency + settings.embed_queue)
        self.run = threading.Semaphore(settings.embed_concurrency)

    def load(self) -> None:
        """Run in a background thread so /healthz answers while the model loads (~47 s)."""
        t0 = time.time()
        if self.encode is None:
            import torch
            torch.set_num_threads(self.s.embed_threads)
            from .. import models
            pipe = models.load_text()
            self.encode = lambda text: pipe.predict([text], source_lang="eng_Latn")[0].numpy()
        self.encode("warm up")                    # first call pays one-time costs; do it before taking traffic
        LOAD_SECONDS.set(time.time() - t0)
        self.ready = True
        log.info("model ready", extra={"seconds": round(time.time() - t0, 1), "model": self.s.text_model})

    def embed(self, text: str) -> "np.ndarray | None":
        """Returns the normalized vector, or None when the queue is full (caller answers 503)."""
        if not self.admit.acquire(blocking=False):
            return None
        INFLIGHT.inc()
        try:
            with self.run:
                t0 = time.time()
                vec = normalize(np.asarray(self.encode(text), dtype=np.float32))
                SECONDS.observe(time.time() - t0)
                return vec
        finally:
            INFLIGHT.dec()
            self.admit.release()


def make_server(emb: Embedder, port: int) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def _reply(self, code: int, body: bytes = b"", ctype: str = "application/json", retry_after: int = 0):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if retry_after:
                self.send_header("Retry-After", str(retry_after))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: dict, retry_after: int = 0):
            if self.path == "/embed":
                REQUESTS.labels(str(code)).inc()
            self._reply(code, json.dumps(obj).encode(), retry_after=retry_after)

        def do_GET(self):
            if self.path == "/healthz":
                self._reply(200, b"ok", "text/plain")
            elif self.path == "/readyz":
                self._reply(200 if emb.ready else 503, b"ok" if emb.ready else b"loading", "text/plain")
            elif self.path == "/metrics":
                self._reply(200, generate_latest(), CONTENT_TYPE_LATEST)
            else:
                self._reply(404)

        def do_POST(self):
            if self.path != "/embed":
                return self._reply(404)
            try:
                text = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)))).get("text")
            except (ValueError, AttributeError):
                return self._json(400, {"error": "body must be JSON {\"text\": ...}"})
            if not isinstance(text, str) or not text.strip() or len(text) > emb.s.max_query_chars:
                return self._json(400, {"error": f"text must be 1..{emb.s.max_query_chars} characters"})
            if not emb.ready:
                return self._json(503, {"error": "model loading"}, NOT_READY_RETRY_S)
            vec = emb.embed(text.strip())
            if vec is None:
                SHED.inc()
                return self._json(503, {"error": "overloaded"}, SHED_RETRY_S)
            self._json(200, {"vector": vec.tolist(), "model": emb.s.text_model, "dim": int(vec.shape[0])})

        def log_message(self, *a):
            pass
    return ThreadingHTTPServer(("0.0.0.0", port), H)


def main() -> None:
    s = get_settings()
    emb = Embedder(s)
    threading.Thread(target=emb.load, daemon=True, name="model-load").start()
    log.info("embedder started", extra={"port": s.http_port})
    make_server(emb, s.http_port).serve_forever()


if __name__ == "__main__":
    main()
