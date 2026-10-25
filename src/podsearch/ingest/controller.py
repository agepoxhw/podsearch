"""The ingest controller: turns "files appeared in incoming/" into Ray jobs.

Folder location is the state: a file in incoming/ is unfinished work. So the controller keeps no database,
only two in-memory sets (pending, in flight) to avoid double submission. If it dies, or a Ray job dies,
the files are still in incoming/ and the next reconcile enqueues them again.
"""
import hmac
import json
import shlex
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from ..config import Settings, get_settings
from ..logs import get_logger
from ..store import Store
from . import sources

log = get_logger("controller")

DISCOVERED = Counter("files_discovered_total", "Files queued for indexing", ["source"])
RECONCILE_FOUND = Counter("reconcile_found_total", "Files the reconciler found that no event had announced")
IGNORED = Counter("files_ignored_total", "Non-audio objects in incoming/")
BACKLOG = Gauge("ingest_backlog_files", "Objects currently in incoming/")
LAG = Gauge("ingest_lag_seconds", "Age of the oldest object in incoming/")
JOBS = Counter("ray_jobs_submitted_total", "Ray jobs submitted")
JOBS_BAD = Counter("ray_jobs_failed_total", "Ray jobs that ended FAILED/STOPPED or timed out", ["reason"])
JOB_SECONDS = Histogram("ray_job_duration_seconds", "Ray job wall time", buckets=(10, 30, 60, 120, 300, 600, 1800, 3600))
WEBHOOK_EVENTS = Counter("webhook_events_total", "S3 notification records received", ["kind"])
WEBHOOK_LAST = Gauge("webhook_last_event_timestamp_seconds", "Unix time of the last accepted webhook call")
RETRY_AFTER_ERROR_S = 5
MANAGED_BY = "podsearch-controller"
TERMINAL = ("SUCCEEDED", "FAILED", "STOPPED")


class RayJobs:
    """Thin client for the Ray Jobs REST API."""

    def __init__(self, address: str):
        self.http = httpx.Client(base_url=address, timeout=10)

    def submit(self, keys: list[str]) -> str:
        r = self.http.post("/api/jobs/", json={
            # Ray runs the entrypoint through a shell: quote every key, or a space or a quote in a file name
            # splits it into several arguments (or runs code).
            "entrypoint": "python -m podsearch.ingest.pipeline " + " ".join(shlex.quote(k) for k in keys),
            "metadata": {"managed_by": MANAGED_BY, "keys": json.dumps(keys)}})
        r.raise_for_status()
        return r.json()["submission_id"]

    def status(self, job_id: str) -> str:
        r = self.http.get(f"/api/jobs/{job_id}")
        r.raise_for_status()
        return r.json()["status"]

    def stop(self, job_id: str) -> None:
        self.http.post(f"/api/jobs/{job_id}/stop").raise_for_status()

    def active_managed(self) -> list[tuple[str, list[str]]]:
        """(job id, files) of jobs an earlier incarnation of this controller started that are still running."""
        r = self.http.get("/api/jobs/")
        r.raise_for_status()
        return [(j["submission_id"], json.loads(j["metadata"]["keys"])) for j in r.json()
                if (j.get("metadata") or {}).get("managed_by") == MANAGED_BY
                and j["status"] in ("PENDING", "RUNNING")]


class Controller:
    def __init__(self, settings: Settings, store: Store, ray: RayJobs, clock=time.monotonic):
        self.s, self.store, self.ray, self.clock = settings, store, ray, clock
        self.lock = threading.Lock()
        self.pending: dict[str, str] = {}      # key -> source it first arrived from
        self.first_pending_at = 0.0
        self.inflight: set[str] = set()
        self.job_id: "str | None" = None
        self.job_started = 0.0
        self.event_seen: set[str] = set()      # keys announced by events and still in incoming/
        self.next_submit_at = 0.0
        self.ignored_logged = False
        self.ready = False
        self.adopted = False                   # True once we know whether a job of ours is already running

    # ---- intake -------------------------------------------------------------------------------
    def enqueue(self, key: str, source: str) -> bool:
        """Both sources call this. Returns True if the key was newly queued."""
        if not sources.is_audio(key):
            IGNORED.inc()
            if not self.ignored_logged:
                self.ignored_logged = True
                log.info("ignoring non-audio objects in incoming/ (counted in files_ignored_total)", extra={"example": key})
            return False
        with self.lock:
            if source == "event":
                self.event_seen.add(key)
            if key in self.pending or key in self.inflight:
                return False                   # already on its way (maybe via the other source)
            if not self.pending:
                self.first_pending_at = self.clock()
            self.pending[key] = source
        DISCOVERED.labels(source).inc()
        if source == "reconcile" and key not in self.event_seen:
            RECONCILE_FOUND.inc()              # an event should have announced this one
        log.info("file queued", extra={"file": key, "source": source})
        return True

    def reconcile(self) -> int:
        objs = sources.list_incoming(self.store)
        audio = [(k, t) for k, t in objs if sources.is_audio(k)]
        BACKLOG.set(len(audio))
        LAG.set(max((time.time() - t.timestamp() for _, t in audio), default=0.0))
        with self.lock:
            self.event_seen &= {k for k, _ in objs}   # forget files that have left incoming/
        new = sum(self.enqueue(k, "reconcile") for k, _ in objs)
        self.ready = True
        log.info("reconcile done", extra={"listed": len(objs), "queued": new})
        return new

    # ---- jobs ---------------------------------------------------------------------------------
    def adopt_running_jobs(self) -> None:
        """After a controller restart: a job we started earlier may still be running. Don't start a second one."""
        for job_id, keys in self.ray.active_managed()[:1]:
            self.job_id, self.job_started = job_id, self.clock()
            self.inflight = set(keys)
            log.info("adopted running job", extra={"job_id": job_id, "files": len(keys)})
        self.adopted = True

    def tick(self) -> None:
        if not self.adopted:                    # Ray may be down or rolling at startup: retry, and submit nothing meanwhile
            try:
                self.adopt_running_jobs()
            except httpx.HTTPError as e:
                log.warning("could not check for running jobs, retrying", extra={"error": repr(e)})
                return
        if self.job_id:
            self._poll_job()
        elif self.pending:
            self._maybe_submit()

    def _poll_job(self) -> None:
        elapsed = self.clock() - self.job_started
        try:
            status = self.ray.status(self.job_id)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:   # the Ray head restarted and lost its job records: the job is gone
                log.warning("ray lost the job", extra={"job_id": self.job_id})
                JOBS_BAD.labels("lost").inc()
                self._release_job()
                return
            status = self._status_unknown(e)
        except httpx.HTTPError as e:
            status = self._status_unknown(e)
        if status is None:
            if elapsed > self.s.job_timeout_s:  # Ray stays unreachable: give up on the job instead of waiting forever
                log.warning("job timed out and Ray is unreachable, releasing it", extra={"job_id": self.job_id})
                JOBS_BAD.labels("timeout").inc()
                self._release_job()
            return
        if status in TERMINAL:
            log.info("job finished", extra={"job_id": self.job_id, "status": status, "seconds": round(elapsed)})
            JOB_SECONDS.observe(elapsed)
            if status != "SUCCEEDED":
                JOBS_BAD.labels(status.lower()).inc()
            self._release_job()
        elif elapsed > self.s.job_timeout_s:
            log.warning("job timed out, stopping it", extra={"job_id": self.job_id})
            JOBS_BAD.labels("timeout").inc()
            try:
                self.ray.stop(self.job_id)
            except httpx.HTTPError as e:
                log.warning("stop failed", extra={"error": repr(e)})

    @staticmethod
    def _status_unknown(e: Exception) -> None:
        log.warning("ray unreachable while polling", extra={"error": repr(e)})

    def _release_job(self) -> None:
        with self.lock:
            self.inflight, self.job_id = set(), None   # unfinished files are still in incoming/: reconcile retries

    def _maybe_submit(self) -> None:
        now = self.clock()
        if now < self.next_submit_at:
            return
        if len(self.pending) < self.s.debounce_files and now - self.first_pending_at < self.s.debounce_s:
            return                              # still collecting a burst
        with self.lock:
            batch = list(self.pending)[:self.s.batch_max]
            sources_used = sorted({self.pending[k] for k in batch})
        try:
            job_id = self.ray.submit(batch)
        except httpx.HTTPError as e:
            self.next_submit_at = now + 10      # files stay pending; try again shortly
            log.warning("submit failed", extra={"error": repr(e)})
            return
        with self.lock:
            for k in batch:
                self.pending.pop(k, None)
            self.inflight, self.job_id, self.job_started = set(batch), job_id, now
            self.first_pending_at = now
        JOBS.inc()
        log.info("job submitted", extra={"job_id": job_id, "files": len(batch), "source": ",".join(sources_used)})


# ---- process wiring ---------------------------------------------------------------------------
def _loop(fn, every: float, name: str) -> None:
    def run():
        while True:
            delay = every
            try:
                fn()
            except Exception:
                log.exception("loop error", extra={"loop": name})
                delay = min(every, RETRY_AFTER_ERROR_S)   # a failed reconcile at startup must not wait a full interval
            time.sleep(delay)
    threading.Thread(target=run, name=name, daemon=True).start()


def _http(ctrl: Controller, port: int) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def _reply(self, code: int, body: bytes = b"", ctype: str = "text/plain"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_HEAD(self):        # the object store health-checks the webhook origin with HEAD before delivering
            self._reply(200)

        def do_POST(self):
            if self.path != "/events":
                return self._reply(404)
            token = ctrl.s.webhook_token
            if token and not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {token}"):
                WEBHOOK_EVENTS.labels("unauthorized").inc()
                return self._reply(401)
            try:
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            except ValueError:
                return self._reply(400)
            WEBHOOK_LAST.set(time.time())
            for key, kind in sources.parse_events(payload, ctrl.s.s3_bucket):
                WEBHOOK_EVENTS.labels(kind).inc()
                if key and ctrl.s.event_source == "webhook":     # with EVENT_SOURCE=none events are accepted and ignored
                    ctrl.enqueue(key, "event")
            self._reply(200)

        def do_GET(self):
            if self.path == "/metrics":
                self._reply(200, generate_latest(), CONTENT_TYPE_LATEST)
            elif self.path == "/healthz":
                self._reply(200, b"ok")
            elif self.path == "/readyz":     # ready after the first reconcile has run
                self._reply(200, b"ready") if ctrl.ready else self._reply(503, b"starting")
            else:
                self._reply(404)

        def log_message(self, *a):          # silence per-request access logs
            pass
    return ThreadingHTTPServer(("0.0.0.0", port), H)


def main() -> None:
    s = get_settings()
    ctrl = Controller(s, Store(s), RayJobs(s.ray_address))
    _loop(ctrl.tick, 1.0, "tick")
    _loop(ctrl.reconcile, s.reconcile_interval, "reconcile")
    log.info("controller started", extra={"event_source": s.event_source, "reconcile_interval": s.reconcile_interval})
    _http(ctrl, s.http_port).serve_forever()


if __name__ == "__main__":
    main()
