from datetime import datetime, timezone

import httpx
import pytest

from podsearch.config import Settings
from podsearch.ingest import controller as C


class FakeStore:
    def __init__(self, keys=()):
        self.keys = list(keys)

    def list_objects(self, prefix):
        return [(k, datetime.now(timezone.utc)) for k in self.keys]


class FakeRay:
    def __init__(self):
        self.submitted, self.state, self.stopped, self.active = [], "RUNNING", [], []

    def submit(self, keys):
        self.submitted.append(list(keys))
        return f"job{len(self.submitted)}"

    def status(self, job_id):
        if isinstance(self.state, Exception):
            raise self.state
        return self.state

    def stop(self, job_id):
        self.stopped.append(job_id)

    def active_managed(self):
        return self.active


class Clock:
    t = 100.0

    def __call__(self):
        return self.t


@pytest.fixture
def env():
    clock, ray, store = Clock(), FakeRay(), FakeStore()
    ctrl = C.Controller(Settings(debounce_s=5, debounce_files=3, job_timeout_s=60), store, ray, clock)
    return ctrl, ray, store, clock


def test_same_key_from_both_sources_is_queued_once(env):
    ctrl, *_ = env
    assert ctrl.enqueue("incoming/a.mp3", "event") is True
    assert ctrl.enqueue("incoming/a.mp3", "reconcile") is False


def test_non_audio_is_ignored(env):
    ctrl, ray, _, clock = env
    assert ctrl.enqueue("incoming/readme.txt", "event") is False
    assert ctrl.pending == {}


def test_debounce_waits_then_batches_a_burst(env):
    ctrl, ray, _, clock = env
    ctrl.enqueue("incoming/a.mp3", "event"); ctrl.enqueue("incoming/b.mp3", "event")
    ctrl.tick()
    assert ray.submitted == []                      # under 3 files and under 5 s: keep collecting
    clock.t += 5
    ctrl.tick()
    assert ray.submitted == [["incoming/a.mp3", "incoming/b.mp3"]]


def test_enough_files_submit_immediately(env):
    ctrl, ray, *_ = env
    for n in "abc":
        ctrl.enqueue(f"incoming/{n}.mp3", "event")
    ctrl.tick()
    assert len(ray.submitted) == 1 and len(ray.submitted[0]) == 3


def test_one_job_at_a_time_and_inflight_keys_not_requeued(env):
    ctrl, ray, _, clock = env
    for n in "abc":
        ctrl.enqueue(f"incoming/{n}.mp3", "event")
    ctrl.tick()
    assert ctrl.enqueue("incoming/a.mp3", "reconcile") is False     # in flight
    ctrl.enqueue("incoming/d.mp3", "event"); clock.t += 10
    ctrl.tick()
    assert len(ray.submitted) == 1                                   # d waits for the running job
    ray.state = "SUCCEEDED"; ctrl.tick()
    ctrl.tick()
    assert ray.submitted[1] == ["incoming/d.mp3"]


def test_failed_job_releases_files_for_the_next_reconcile(env):
    ctrl, ray, store, clock = env
    store.keys = ["incoming/a.mp3"]
    ctrl.reconcile(); clock.t += 5; ctrl.tick()
    ray.state = "FAILED"; ctrl.tick()
    assert ctrl.inflight == set() and ctrl.job_id is None
    assert ctrl.reconcile() == 1                                     # still in incoming/, so queued again


def test_stuck_job_is_stopped_after_timeout(env):
    ctrl, ray, _, clock = env
    for n in "abc":
        ctrl.enqueue(f"incoming/{n}.mp3", "event")
    ctrl.tick(); clock.t += 61; ctrl.tick()
    assert ray.stopped == ["job1"]


def test_reconcile_found_counts_only_files_events_missed(env):
    ctrl, ray, store, _ = env
    before = C.RECONCILE_FOUND._value.get()
    store.keys = ["incoming/a.mp3", "incoming/b.mp3"]
    ctrl.enqueue("incoming/a.mp3", "event")          # announced by an event
    ctrl.reconcile()                                 # a is already queued; b was missed
    assert C.RECONCILE_FOUND._value.get() - before == 1


def test_restart_adopts_a_running_job(env):
    ctrl, ray, *_ = env
    ray.active = [("jobX", ["incoming/a.mp3"])]
    ctrl.adopt_running_jobs()
    assert ctrl.job_id == "jobX" and ctrl.enqueue("incoming/a.mp3", "reconcile") is False


def http_error(code):
    req = httpx.Request("GET", "http://ray/api/jobs/x")
    return httpx.HTTPStatusError("boom", request=req, response=httpx.Response(code, request=req))


def test_job_lost_after_ray_head_restart_is_released(env):
    ctrl, ray, _, clock = env
    for n in "abc":
        ctrl.enqueue(f"incoming/{n}.mp3", "event")
    ctrl.tick()
    ray.state = http_error(404)                       # a restarted head no longer knows the job
    ctrl.tick()
    assert ctrl.job_id is None and ctrl.inflight == set()


def test_unreachable_ray_is_waited_for_but_not_forever(env):
    ctrl, ray, _, clock = env
    for n in "abc":
        ctrl.enqueue(f"incoming/{n}.mp3", "event")
    ctrl.tick()
    ray.state = httpx.ConnectError("ray is down")
    clock.t += 30; ctrl.tick()
    assert ctrl.job_id == "job1"                      # a short outage: keep the job
    clock.t += 61; ctrl.tick()
    assert ctrl.job_id is None                        # past job_timeout_s: release it


def test_submit_quotes_keys_for_the_shell():
    sent = {}

    class Http:
        def post(self, path, json):
            sent.update(json)
            return type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: {"submission_id": "j"}})()

    ray = C.RayJobs.__new__(C.RayJobs)
    ray.http = Http()
    ray.submit(["incoming/my episode.mp3", "incoming/Lex's $(x).mp3"])
    import shlex
    argv = shlex.split(sent["entrypoint"])
    assert argv[3:] == ["incoming/my episode.mp3", "incoming/Lex's $(x).mp3"]    # exactly what a shell would pass


def test_no_submit_until_the_startup_job_check_succeeds(env):
    ctrl, ray, _, clock = env
    ray.active_managed = lambda: (_ for _ in ()).throw(httpx.ConnectError("ray is rolling"))
    ctrl.enqueue("incoming/a.mp3", "event"); clock.t += 5
    ctrl.tick()
    assert ray.submitted == [] and not ctrl.adopted      # Ray unreachable: must not risk a duplicate job
    ray.active_managed = lambda: [("jobX", ["incoming/a.mp3"])]
    ctrl.tick()
    assert ctrl.job_id == "jobX" and ray.submitted == []  # adopted the running job instead of submitting


# ---- webhook parsing and handler -------------------------------------------------------------
import json
import threading
import urllib.error
import urllib.request

from podsearch.ingest import sources


def rec(name, key, bucket="podcasts"):
    return {"eventName": name, "s3": {"bucket": {"name": bucket}, "object": {"key": key}}}


def test_created_in_incoming_yields_decoded_key():
    ev = {"Records": [rec("s3:ObjectCreated:Put", "incoming%2Fmy+show%2520.mp3")]}
    assert sources.parse_events(ev, "podcasts") == [("incoming/my show%20.mp3", "created")]


def test_multipart_completion_counts_as_created():
    ev = {"Records": [rec("s3:ObjectCreated:CompleteMultipartUpload", "incoming%2Fbig.mp3")]}
    assert sources.parse_events(ev, "podcasts")[0] == ("incoming/big.mp3", "created")


def test_removed_other_prefix_and_other_bucket_are_not_work():
    ev = {"Records": [rec("s3:ObjectRemoved:Delete", "incoming%2Fa.mp3"),
                      rec("s3:ObjectCreated:Put", "library%2Fa.mp3"),
                      rec("s3:ObjectCreated:Put", "incoming%2Fa.mp3", bucket="other")]}
    assert [k for k, _ in sources.parse_events(ev, "podcasts")] == [None, None, None]


@pytest.fixture
def server(env):
    ctrl = env[0]
    ctrl.s = Settings(webhook_token="t0ken", event_source="webhook")
    srv = C._http(ctrl, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield ctrl, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def post(url, body, token="t0ken"):
    req = urllib.request.Request(url + "/events", data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        return urllib.request.urlopen(req).status
    except urllib.error.HTTPError as e:
        return e.code


def test_webhook_enqueues_with_valid_token_and_rejects_bad_token(server):
    ctrl, url = server
    ev = {"Records": [rec("s3:ObjectCreated:Put", "incoming%2Fa.mp3")]}
    assert post(url, ev, token="wrong") == 401 and ctrl.pending == {}
    assert post(url, ev) == 200 and ctrl.pending == {"incoming/a.mp3": "event"}


def test_head_on_the_origin_is_ok(server):
    _, url = server
    assert urllib.request.urlopen(urllib.request.Request(url + "/", method="HEAD")).status == 200
