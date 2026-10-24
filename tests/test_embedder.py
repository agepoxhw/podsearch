import json
import threading
import time
import urllib.error
import urllib.request

import numpy as np
import pytest

from podsearch.config import Settings
from podsearch.embed import server as E


def call(port, path="/embed", body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=None if body is None else json.dumps(body).encode(),
                                 method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def start(encode, **kw):
    emb = E.Embedder(Settings(embed_concurrency=1, embed_queue=1, **kw), encode=encode)
    srv = E.make_server(emb, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return emb, srv, srv.server_address[1]


def test_not_ready_until_loaded_but_alive():
    emb, srv, port = start(lambda t: np.ones(4))
    assert call(port, "/healthz")[0] == 200
    code, headers, _ = call(port, "/readyz")
    assert code == 503
    assert call(port, "/embed", {"text": "x"})[0] == 503 and headers is not None
    emb.load()
    assert call(port, "/readyz")[0] == 200
    srv.shutdown()


def test_embed_returns_normalized_vector_with_model_and_dim():
    emb, srv, port = start(lambda t: np.array([3.0, 4.0]))
    emb.load()
    code, _, body = call(port, "/embed", {"text": " hello "})
    out = json.loads(body)
    assert code == 200 and out["dim"] == 2 and out["model"] == "text_sonar_basic_encoder"
    assert out["vector"] == pytest.approx([0.6, 0.8])
    srv.shutdown()


@pytest.mark.parametrize("body", [{"text": ""}, {"text": "   "}, {"text": "x" * 501}, {"nope": 1}, {"text": 5}])
def test_bad_input_is_400(body):
    emb, srv, port = start(lambda t: np.ones(2))
    emb.load()
    assert call(port, "/embed", body)[0] == 400
    srv.shutdown()


def test_overload_sheds_with_503_and_retry_after():
    gate = threading.Event()
    emb, srv, port = start(lambda t: (gate.wait(3), np.ones(2))[1])   # 1 running + 1 queued = 2 admitted
    emb.ready = True
    results = []
    ts = [threading.Thread(target=lambda: results.append(call(port, "/embed", {"text": "q"}))) for _ in range(2)]
    [t.start() for t in ts]
    time.sleep(0.3)                                                    # both admitted, model busy
    code, headers, _ = call(port, "/embed", {"text": "third"})
    assert code == 503 and headers["Retry-After"] == "1"
    gate.set()
    [t.join() for t in ts]
    assert [r[0] for r in results] == [200, 200]
    srv.shutdown()
