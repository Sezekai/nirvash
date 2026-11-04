"""Mood words shared by songs (AudioSet music moods) and videos (CLIP prompts)."""
from __future__ import annotations

import numpy as np

# Same keys as ast.MOOD_LABELS; these are the CLIP prompts for video frames.
MOODS: dict[str, str] = {
    "happy": "a happy, cheerful scene",
    "playful": "a playful, funny scene",
    "sad": "a sad, melancholic scene",
    "calm": "a calm, gentle, peaceful scene",
    "energetic": "an exciting, energetic, fast-moving scene",
    "aggressive": "an intense, aggressive action scene",
    "dark": "a dark, scary, ominous scene",
}
MOOD_KEYS = list(MOODS)


def softmax(x: np.ndarray, scale: float, axis: int = -1) -> np.ndarray:
    z = x * scale
    z = z - z.max(axis=axis, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=axis, keepdims=True)


def video_mood(image_embs: np.ndarray, clip) -> dict[str, float]:
    t = clip.embed_text([MOODS[k] for k in MOOD_KEYS])
    p = softmax(image_embs @ t.T, scale=100.0).mean(axis=0)
    return {k: round(float(v), 4) for k, v in zip(MOOD_KEYS, p)}


def top_moods(mood: dict[str, float] | None, n: int = 2) -> list[str]:
    if not mood:
        return []
    return [k for k, _ in sorted(mood.items(), key=lambda kv: -kv[1])[:n] if k in MOODS]


def mood_similarity(a: dict[str, float] | None, b: dict[str, float] | None) -> float | None:
    if not a or not b:
        return None
    va = np.array([a.get(k, 0) for k in MOOD_KEYS])
    vb = np.array([b.get(k, 0) for k in MOOD_KEYS])
    na, nb = np.linalg.norm(va), np.linalg.norm(vb)
    if na == 0 or nb == 0:
        return None
    return float(va @ vb / (na * nb))
