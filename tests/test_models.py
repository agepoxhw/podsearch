import pytest

from podsearch import models


def test_missing_cache_dir_env_fails_loudly(monkeypatch):
    monkeypatch.delenv("FAIRSEQ2_CACHE_DIR", raising=False)
    with pytest.raises(models.ModelCacheError, match="FAIRSEQ2_CACHE_DIR"):
        models.load_speech()


@pytest.mark.parametrize("loader", ["load_speech", "load_text"])
def test_refuses_to_load_when_weights_are_not_baked(monkeypatch, tmp_path, loader):
    monkeypatch.setenv("FAIRSEQ2_CACHE_DIR", str(tmp_path))   # empty cache: fairseq2 would download
    with pytest.raises(models.ModelCacheError, match="Refusing to download"):
        getattr(models, loader)()


def test_marker_is_per_model(monkeypatch, tmp_path):
    monkeypatch.setenv("FAIRSEQ2_CACHE_DIR", str(tmp_path))
    models.marker("speech").write_text("ok")
    models._require_baked("speech")                           # passes
    with pytest.raises(models.ModelCacheError):
        models._require_baked("text")                         # the text image must not run on the speech cache
