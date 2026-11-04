"""Tempo, beats, energy and loudness envelope, measured locally with NumPy."""
from __future__ import annotations

import numpy as np

SR = 11025
N_FFT = 1024
HOP = 256


def _onset_envelope(y: np.ndarray) -> np.ndarray:
    if len(y) < N_FFT * 2:
        return np.zeros(1)
    n = 1 + (len(y) - N_FFT) // HOP
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n)[:, None]
    win = np.hanning(N_FFT)
    mag = np.abs(np.fft.rfft(y[idx] * win, axis=1))
    logm = np.log1p(100 * mag)
    flux = np.maximum(0, np.diff(logm, axis=0)).sum(axis=1)
    flux = flux - np.convolve(flux, np.ones(16) / 16, mode="same")  # remove slow trend
    flux = np.maximum(flux, 0)
    return flux / (flux.max() + 1e-9)


def tempo_and_beats(y: np.ndarray, sr: int = SR) -> tuple[float | None, np.ndarray]:
    """Estimate BPM (60-200) and beat times in seconds."""
    if sr != SR:
        raise ValueError("expects 11025 Hz audio")
    env = _onset_envelope(y)
    if len(env) < 100 or env.sum() == 0:
        return None, np.zeros(0)
    fps = SR / HOP
    e = env - env.mean()
    ac = np.correlate(e, e, mode="full")[len(e) - 1:]
    lags = np.arange(len(ac))
    bpm_of = lambda lag: 60.0 * fps / np.maximum(lag, 1)
    lo, hi = int(60 * fps / 200), int(60 * fps / 60)
    hi = min(hi, len(ac) - 1)
    if hi <= lo:
        return None, np.zeros(0)
    cand = lags[lo:hi + 1]
    bpms = bpm_of(cand)
    prior = np.exp(-0.5 * (np.log2(bpms / 120.0) / 0.9) ** 2)
    score = ac[lo:hi + 1] * prior
    lag = int(cand[int(np.argmax(score))])
    # Refine with a parabolic fit
    if lo < lag < hi:
        a, b, c = ac[lag - 1], ac[lag], ac[lag + 1]
        denom = a - 2 * b + c
        frac_lag = lag + (0.5 * (a - c) / denom if denom else 0.0)
    else:
        frac_lag = float(lag)
    bpm = float(60.0 * fps / frac_lag)
    period = frac_lag
    best_phase, best = 0.0, -1.0
    for ph in np.linspace(0, period, num=max(2, int(period)), endpoint=False):
        pos = np.arange(ph, len(env) - 1, period).astype(int)
        s = env[pos].sum()
        if s > best:
            best, best_phase = s, ph
    beats = np.arange(best_phase, len(env) - 1, period) / fps
    return round(bpm, 1), beats


def loudness_envelope(y: np.ndarray, sr: int, step: float = 0.5) -> np.ndarray:
    """RMS level in dBFS every `step` seconds."""
    n = max(1, int(sr * step))
    k = len(y) // n
    if k == 0:
        return np.array([-90.0])
    frames = y[: k * n].reshape(k, n)
    rms = np.sqrt((frames.astype(np.float64) ** 2).mean(axis=1))
    return 20 * np.log10(np.maximum(rms, 1e-5))


def energy_score(y: np.ndarray, sr: int = SR) -> float:
    """0..1: how loud and busy the music is."""
    env = loudness_envelope(y, sr)
    loud = float(np.percentile(env, 75)) if len(env) else -60.0
    loud_n = np.clip((loud + 36) / 30, 0, 1)
    on = _onset_envelope(y) if sr == SR else np.zeros(1)
    density = float((on > 0.25).mean()) if len(on) > 1 else 0.0
    dens_n = np.clip(density / 0.25, 0, 1)
    return round(float(0.6 * loud_n + 0.4 * dens_n), 3)


def song_pace(bpm: float | None, energy: float | None) -> float:
    b = 0.5 if not bpm else float(np.clip((bpm - 60) / 120, 0, 1))
    e = 0.5 if energy is None else float(energy)
    return round(0.45 * b + 0.55 * e, 3)


def smart_start(y: np.ndarray, sr: int, beats: np.ndarray, need: float, song_len: float) -> float:
    """Pick where to start a song for a video `need` seconds long.

    Skips a quiet intro; if the song is much longer than the video, starts at its strongest
    stretch. Starts are snapped to beats."""
    env = loudness_envelope(y, sr, step=0.5)
    if len(env) < 2:
        return 0.0
    peak = np.percentile(env, 90)
    # First point where the song is within 12 dB of its loud parts.
    loud_idx = int(np.argmax(env > peak - 12))
    intro_skip = loud_idx * 0.5 if loud_idx * 0.5 < min(20.0, song_len * 0.25) else 0.0
    spare = song_len - need
    if spare <= 1.0:
        start = min(intro_skip, max(0.0, spare))
    else:
        win = max(1, int(need / 0.5))
        if win >= len(env):
            start = min(intro_skip, spare)
        else:
            c = np.convolve(env, np.ones(win) / win, mode="valid")  # mean loudness of each window
            starts = np.arange(len(c)) * 0.5
            ok = starts <= spare
            # Prefer early starts unless a later stretch is clearly stronger.
            score = c - 0.02 * starts
            score[~ok] = -1e9
            best = float(starts[int(np.argmax(score))])
            start = best if c[int(best / 0.5)] > c[int(min(intro_skip, spare) / 0.5)] + 1.5 else min(intro_skip, spare)
    if len(beats):
        near = beats[np.argmin(np.abs(beats - start))]
        if abs(near - start) < 1.0 and near <= max(0.0, spare):
            start = float(near)
    return round(max(0.0, start), 2)
