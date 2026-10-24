"""Structured JSON logs on stdout (one object per line), so `kubectl logs` output is greppable and
a log shipper could parse it. Extra fields: log.info("indexed", extra={"file": name, "chunks": 12})."""
import json
import logging
import sys

_STD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class _Json(logging.Formatter):
    def format(self, r: logging.LogRecord) -> str:
        out = {"ts": self.formatTime(r, "%Y-%m-%dT%H:%M:%S"), "level": r.levelname, "logger": r.name, "msg": r.getMessage()}
        out.update({k: v for k, v in r.__dict__.items() if k not in _STD})
        if r.exc_info:
            out["exc"] = self.formatException(r.exc_info)
        return json.dumps(out, default=str)


def get_logger(name: str) -> logging.Logger:
    log = logging.getLogger(name)
    if not log.handlers:                      # idempotent: Ray may import this module many times
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(_Json())
        log.addHandler(h)
        log.setLevel(logging.INFO)
        log.propagate = False
    return log
