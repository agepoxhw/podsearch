"""Decode audio to 16 kHz mono and cut it into fixed 10 s chunks.

Pure functions: no S3, no models. Chunk policy (PLAN section 2):
  * non-overlapping windows of CHUNK_S seconds
  * a tail shorter than MIN_TAIL_S is dropped
  * a tail of MIN_TAIL_S..CHUNK_S is zero-padded to a full window
"""
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16_000   # what the SONAR speech encoder expects
CHUNK_S = 10.0
MIN_TAIL_S = 2.0
_RMS_FLOOR_DB = -120.0  # digital silence would otherwise be -inf


class AudioError(Exception):
    """Base class: the file cannot be indexed. The pipeline moves it to rejected/."""


class DecodeError(AudioError):
    """ffmpeg could not read the input (corrupt, not audio, truncated to nothing)."""


class AudioTooShortError(AudioError):
    """Decoded fine, but shorter than MIN_TAIL_S, so it would produce zero chunks."""


@dataclass(frozen=True)
class Chunk:
    index: int
    start_s: float
    end_s: float            # end of the real audio (not of the zero padding)
    samples: np.ndarray     # float32, always exactly CHUNK_S * SAMPLE_RATE long
    rms_db: float           # loudness of the real audio, to spot near-silent chunks

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


def decode(src: "str | Path | bytes") -> np.ndarray:
    """Any audio/video container -> float32 mono at 16 kHz, via an ffmpeg subprocess.

    ffmpeg does the resampling and the stereo downmix, so 44.1 kHz stereo WAV works the same
    as an MP3. `src` is a path, or raw bytes (piped through stdin).
    """
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", "pipe:0" if isinstance(src, bytes) else str(src),
           "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "pipe:1"]
    proc = subprocess.run(cmd, input=src if isinstance(src, bytes) else None,
                          capture_output=True)
    if proc.returncode != 0:
        raise DecodeError(proc.stderr.decode(errors="replace").strip() or f"ffmpeg exit {proc.returncode}")
    signal = np.frombuffer(proc.stdout, dtype=np.float32)
    if signal.size == 0:
        raise DecodeError("ffmpeg produced no audio samples")
    return signal


def rms_db(samples: np.ndarray) -> float:
    rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
    return max(20.0 * np.log10(rms), _RMS_FLOOR_DB) if rms > 0 else _RMS_FLOOR_DB


def chunk(signal: np.ndarray, sr: int = SAMPLE_RATE, chunk_s: float = CHUNK_S,
          min_tail_s: float = MIN_TAIL_S) -> list[Chunk]:
    size = int(round(chunk_s * sr))
    n_full, tail = divmod(len(signal), size)
    chunks = []
    for i in range(n_full):
        w = signal[i * size:(i + 1) * size]
        chunks.append(Chunk(i, i * chunk_s, (i + 1) * chunk_s, w, rms_db(w)))
    if tail >= min_tail_s * sr:
        w = signal[n_full * size:]
        padded = np.zeros(size, dtype=np.float32)
        padded[:tail] = w
        chunks.append(Chunk(n_full, n_full * chunk_s, len(signal) / sr, padded, rms_db(w)))
    if not chunks:
        raise AudioTooShortError(f"{len(signal) / sr:.2f}s of audio; need at least {min_tail_s}s")
    return chunks
