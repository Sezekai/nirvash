"""Text prompts used with CLAP and CLIP."""
from __future__ import annotations

import numpy as np

# Audio classes for "is this music?". Speech over music counts as speech.
AUDIO_CLASSES: dict[str, list[str]] = {
    "music": [
        "music",
        "a song with singing",
        "instrumental music",
        "background music playing",
        "electronic dance music",
        "piano music",
    ],
    "speech": [
        "a person talking",
        "people having a conversation",
        "a man speaking",
        "a woman speaking",
        "someone talking over background music",
        "a child speaking",
        "laughing and talking",
    ],
    "other": [
        "silence",
        "background noise",
        "sound effects",
        "wind and outdoor ambient noise",
        "footsteps and room noise",
        "applause and crowd noise",
        "video game sound effects",
        "engine and traffic noise",
    ],
}

# Shared mood words: (CLAP prompt for songs, CLIP prompt for video frames)
MOODS: dict[str, tuple[str, str]] = {
    "calm": ("calm, peaceful music", "a calm, peaceful scene"),
    "energetic": ("energetic, upbeat music", "an energetic, fast-moving scene"),
    "happy": ("happy, cheerful music", "a happy, cheerful scene"),
    "sad": ("sad, melancholic music", "a sad, melancholic scene"),
    "romantic": ("romantic, tender music", "a romantic, tender scene"),
    "dramatic": ("dramatic, intense music", "a dramatic, intense scene"),
    "dreamy": ("dreamy, ethereal music", "a dreamy, ethereal scene"),
    "dark": ("dark, ominous music", "a dark, ominous scene"),
    "epic": ("epic, cinematic orchestral music", "an epic, cinematic scene"),
    "playful": ("playful, quirky music", "a playful, funny scene"),
    "cool": ("cool, stylish music", "a cool, stylish scene"),
    "aggressive": ("aggressive, heavy music", "an intense action fight scene"),
}
MOOD_KEYS = list(MOODS)


def softmax(x: np.ndarray, scale: float, axis: int = -1) -> np.ndarray:
    z = x * scale
    z = z - z.max(axis=axis, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=axis, keepdims=True)


def audio_class_probs(audio_embs: np.ndarray, clap) -> dict[str, np.ndarray]:
    """(n,512) window embeddings -> {class: (n,) probability}."""
    prompts, owners = [], []
    for cls, ps in AUDIO_CLASSES.items():
        for p in ps:
            prompts.append(p)
            owners.append(cls)
    t = clap.embed_text(prompts)
    p = softmax(audio_embs @ t.T, scale=50.0)
    out = {}
    for cls in AUDIO_CLASSES:
        idx = [i for i, o in enumerate(owners) if o == cls]
        out[cls] = p[:, idx].sum(axis=1)
    return out


def song_mood(embedding: np.ndarray, clap) -> dict[str, float]:
    t = clap.embed_text([MOODS[k][0] for k in MOOD_KEYS])
    p = softmax(embedding[None] @ t.T, scale=50.0)[0]
    return {k: round(float(v), 4) for k, v in zip(MOOD_KEYS, p)}


def video_mood(image_embs: np.ndarray, clip) -> dict[str, float]:
    t = clip.embed_text([MOODS[k][1] for k in MOOD_KEYS])
    p = softmax(image_embs @ t.T, scale=100.0).mean(axis=0)
    return {k: round(float(v), 4) for k, v in zip(MOOD_KEYS, p)}


def top_moods(mood: dict[str, float] | None, n: int = 2) -> list[str]:
    if not mood:
        return []
    return [k for k, _ in sorted(mood.items(), key=lambda kv: -kv[1])[:n]]


def mood_similarity(a: dict[str, float] | None, b: dict[str, float] | None) -> float | None:
    if not a or not b:
        return None
    va = np.array([a.get(k, 0) for k in MOOD_KEYS])
    vb = np.array([b.get(k, 0) for k in MOOD_KEYS])
    na, nb = np.linalg.norm(va), np.linalg.norm(vb)
    if na == 0 or nb == 0:
        return None
    return float(va @ vb / (na * nb))
