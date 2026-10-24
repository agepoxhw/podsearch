"""Load SONAR models from the weights baked into the image. Never from the network.

fairseq2 downloads a model on first use if it is not in FAIRSEQ2_CACHE_DIR. In a pod that would mean
gigabytes of silent download on every start, so `load_*` first checks for the marker file that
`podsearch.prefetch` writes at image build time, and fails loudly if it is missing.
"""
import os
from pathlib import Path

SPEECH_ENCODER = "sonar_speech_encoder_eng"
TEXT_ENCODER = "text_sonar_basic_encoder"


class ModelCacheError(RuntimeError):
    pass


def cache_dir() -> Path:
    d = os.environ.get("FAIRSEQ2_CACHE_DIR")
    if not d:
        raise ModelCacheError("FAIRSEQ2_CACHE_DIR is not set")
    return Path(d)


def marker(kind: str) -> Path:
    return cache_dir() / f".baked-{kind}"


def _require_baked(kind: str) -> None:
    if not marker(kind).exists():
        raise ModelCacheError(
            f"{kind} model is not baked into this image ({marker(kind)} missing). "
            f"Refusing to download weights at runtime; rebuild the image.")


def build_speech():
    import torch
    from sonar.inference_pipelines.speech import SpeechToEmbeddingModelPipeline
    return SpeechToEmbeddingModelPipeline(encoder=SPEECH_ENCODER, device=torch.device("cpu"))


def build_text():
    import torch
    from sonar.inference_pipelines.text import TextToEmbeddingModelPipeline
    return TextToEmbeddingModelPipeline(encoder=TEXT_ENCODER, tokenizer=TEXT_ENCODER,
                                        device=torch.device("cpu"))


def load_speech():
    _require_baked("speech")
    return build_speech()


def load_text():
    _require_baked("text")
    return build_text()
