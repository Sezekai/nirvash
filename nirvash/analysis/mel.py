"""CLAP audio features (log-mel spectrogram), a NumPy port of HF's ClapFeatureExtractor
in "rand_trunc"/"repeatpad" mode, as used by laion/clap-htsat-unfused."""
from __future__ import annotations

import numpy as np

SR = 48000
N_FFT = 1024
HOP = 480
N_MELS = 64
FMIN, FMAX = 50.0, 14000.0
MAX_LEN = SR * 10  # 10 s windows


def _hz_to_mel_slaney(f):
    f = np.asarray(f, dtype=np.float64)
    f_sp = 200.0 / 3
    mels = f / f_sp
    min_log_hz, min_log_mel = 1000.0, 15.0
    logstep = 27.0 / np.log(6.4)
    return np.where(f >= min_log_hz, min_log_mel + np.log(np.maximum(f, 1e-10) / min_log_hz) * logstep, mels)


def _mel_to_hz_slaney(m):
    m = np.asarray(m, dtype=np.float64)
    f_sp = 200.0 / 3
    freqs = f_sp * m
    min_log_hz, min_log_mel = 1000.0, 15.0
    logstep = np.log(6.4) / 27.0
    return np.where(m >= min_log_mel, min_log_hz * np.exp(logstep * (m - min_log_mel)), freqs)


def mel_filters() -> np.ndarray:
    n_bins = N_FFT // 2 + 1
    mel_freqs = np.linspace(_hz_to_mel_slaney(FMIN), _hz_to_mel_slaney(FMAX), N_MELS + 2)
    filter_freqs = _mel_to_hz_slaney(mel_freqs)
    fft_freqs = np.linspace(0, SR // 2, n_bins)
    diff = np.diff(filter_freqs)
    slopes = filter_freqs[None, :] - fft_freqs[:, None]
    down = -slopes[:, :-2] / diff[:-1]
    up = slopes[:, 2:] / diff[1:]
    fb = np.maximum(0.0, np.minimum(down, up))
    enorm = 2.0 / (filter_freqs[2:N_MELS + 2] - filter_freqs[:N_MELS])
    return fb * enorm[None, :]  # (513, 64)


_FB = mel_filters()
_WIN = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(N_FFT) / N_FFT))  # periodic Hann


def log_mel(wave: np.ndarray) -> np.ndarray:
    """(n,) float waveform -> (frames, 64) float32 dB mel spectrogram."""
    x = np.asarray(wave, dtype=np.float64)
    x = np.pad(x, (N_FFT // 2, N_FFT // 2), mode="reflect")
    n_frames = 1 + (len(x) - N_FFT) // HOP
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n_frames)[:, None]
    frames = x[idx] * _WIN[None, :]
    spec = np.abs(np.fft.rfft(frames, n=N_FFT, axis=1)) ** 2  # (frames, 513)
    mel = spec @ _FB  # (frames, 64)
    return (10.0 * np.log10(np.maximum(mel, 1e-10))).astype(np.float32)


def prepare_window(wave: np.ndarray) -> np.ndarray:
    """Pad (repeat then zero) or cut a mono 48 kHz chunk to 10 s."""
    w = np.asarray(wave, dtype=np.float32)
    if len(w) == 0:
        w = np.zeros(1, np.float32)
    if len(w) > MAX_LEN:
        return w[:MAX_LEN]
    if len(w) < MAX_LEN:
        n = int(MAX_LEN / len(w))
        w = np.tile(w, n)
        w = np.pad(w, (0, MAX_LEN - len(w)))
    return w


def features(waves: list[np.ndarray]) -> np.ndarray:
    """List of mono 48 kHz chunks -> (batch, 1, 1001, 64) float32 input_features."""
    return np.stack([log_mel(prepare_window(w))[None] for w in waves]).astype(np.float32)
