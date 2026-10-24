#!/usr/bin/env python3
"""Local labeling page: `make label` -> http://localhost:8099

Shows every hit from results/raw.json with an inline player and tick/cross buttons. Each click is saved at once to
results/labels.json and results/queries.md (via eval.py), so nothing is lost if you close the tab.
The labels are yours: this script never sets one. Audio is played from the search API on localhost:8080.
Standard library only; listens on 127.0.0.1 only.
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import eval as ev  # noqa: E402  (scripts/eval.py)

PORT, LOCK = 8099, threading.Lock()
PAGE = (Path(__file__).parent / "label.html").read_text()


def load() -> dict:
    raw = json.loads((ev.OUT / "raw.json").read_text())
    return {"api": ev.API, "queries": raw["results"], "labels": ev.load_labels(), "commit": raw["commit"]}


def save(key: str, label: str, reason: str) -> dict:
    with LOCK:
        raw = json.loads((ev.OUT / "raw.json").read_text())
        labels = ev.load_labels()
        if label or reason:
            labels[key] = {"label": label, "reason": reason}
        else:
            labels.pop(key, None)
        ev.save_labels(labels)
        (ev.OUT / "queries.md").write_text(ev.markdown(raw["results"], raw["commit"], labels))
        return labels


class H(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif self.path == "/data":
            self._send(200, json.dumps(load()).encode(), "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path != "/label":
            return self._send(404, b"not found", "text/plain")
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        if body.get("label", "") not in ("", "✓", "✗") or "key" not in body:
            return self._send(400, b"bad label", "text/plain")
        labels = save(body["key"], body.get("label", ""), body.get("reason", "")[:300])
        self._send(200, json.dumps({"saved": len(labels)}).encode(), "application/json")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if not (ev.OUT / "raw.json").exists():
        sys.exit("results/raw.json is missing: run `make eval` first")
    print(f"labeling page: http://localhost:{PORT}   (Ctrl+C to stop; labels are saved on every click)")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
