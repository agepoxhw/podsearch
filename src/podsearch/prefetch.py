"""Image build step: load one model so fairseq2 downloads it into FAIRSEQ2_CACHE_DIR, then write the
marker that `models.load_*` requires.   python -m podsearch.prefetch speech|text
"""
import sys

from . import models


def main(kind: str) -> None:
    {"speech": models.build_speech, "text": models.build_text}[kind]()   # downloads on first use
    models.marker(kind).write_text("ok\n")
    print(f"baked {kind} model into {models.cache_dir()}")


if __name__ == "__main__":
    main(sys.argv[1])
