import wave

import numpy as np
import pytest

from podsearch.audio import (CHUNK_S, SAMPLE_RATE, AudioTooShortError, DecodeError, chunk, decode,
                             rms_db)

SIZE = int(CHUNK_S * SAMPLE_RATE)


def sig(seconds: float) -> np.ndarray:
    return (0.1 * np.ones(int(seconds * SAMPLE_RATE))).astype(np.float32)


def write_wav(path, seconds, rate, channels):
    t = np.arange(int(seconds * rate)) / rate
    mono = (0.5 * np.sin(2 * np.pi * 440 * t) * 32767).astype("<i2")
    data = np.repeat(mono[:, None], channels, axis=1)   # same tone on every channel
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data.tobytes())


# ---- chunking ----
def test_exact_multiple_has_no_tail_chunk():
    cs = chunk(sig(30))
    assert [c.index for c in cs] == [0, 1, 2]
    assert [(c.start_s, c.end_s) for c in cs] == [(0, 10), (10, 20), (20, 30)]


def test_short_tail_is_dropped():
    assert len(chunk(sig(20.5))) == 2


def test_tail_of_seven_seconds_is_zero_padded():
    cs = chunk(sig(27))
    last = cs[-1]
    assert len(cs) == 3 and last.samples.shape == (SIZE,)
    assert last.start_s == 20 and last.end_s == 27 and last.duration_s == 7
    assert np.all(last.samples[7 * SAMPLE_RATE:] == 0)       # padded part
    assert np.all(last.samples[:7 * SAMPLE_RATE] == np.float32(0.1))


def test_tail_boundary_is_two_seconds():
    assert len(chunk(sig(12.0))) == 2        # exactly 2 s is kept
    assert len(chunk(sig(11.99))) == 1       # just under is dropped


def test_every_chunk_is_full_length():
    assert all(c.samples.shape == (SIZE,) and c.samples.dtype == np.float32 for c in chunk(sig(33)))


def test_too_short_raises_instead_of_indexing_nothing():
    with pytest.raises(AudioTooShortError):
        chunk(sig(1.0))


def test_rms_ignores_padding():
    assert chunk(sig(13))[-1].rms_db == pytest.approx(20 * np.log10(0.1), abs=0.01)


def test_rms_db_extremes():
    assert rms_db(np.zeros(100, dtype=np.float32)) == -120.0
    full_scale_sine = np.sin(np.linspace(0, 2 * np.pi * 100, SAMPLE_RATE)).astype(np.float32)
    assert rms_db(full_scale_sine) == pytest.approx(-3.01, abs=0.05)


# ---- decoding (uses the real ffmpeg) ----
def test_decode_44k_stereo_wav_becomes_16k_mono(tmp_path):
    p = tmp_path / "a.wav"
    write_wav(p, seconds=3, rate=44100, channels=2)
    s = decode(p)
    assert s.dtype == np.float32 and s.ndim == 1
    assert abs(len(s) - 3 * SAMPLE_RATE) < 400           # resampler edge effects only
    # ffmpeg's downmix is not a plain average: it sums the channels at ~0.707 each, so an identical
    # L/R tone of amplitude 0.5 comes out at ~0.707 (+3 dB). Not clipped; harmless for embeddings.
    assert np.abs(s).max() == pytest.approx(0.707, abs=0.03)


def test_decode_accepts_bytes(tmp_path):
    p = tmp_path / "a.wav"
    write_wav(p, seconds=1, rate=16000, channels=1)
    assert len(decode(p.read_bytes())) == pytest.approx(SAMPLE_RATE, abs=10)


@pytest.mark.parametrize("bad", [b"", b"this is not audio at all" * 100])
def test_decode_rejects_corrupt_bytes(bad):
    with pytest.raises(DecodeError):
        decode(bad)


def test_decode_rejects_missing_file(tmp_path):
    with pytest.raises(DecodeError):
        decode(tmp_path / "nope.mp3")
