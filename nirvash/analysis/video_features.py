"""Video pace (motion + cuts) and mood, for matching songs."""
from __future__ import annotations

import numpy as np

from ..core import log as _log
from ..media import ff
from . import labels

log = _log.get("video")


def motion_and_cuts(path: str, duration: float) -> dict:
    fps = 4.0 if duration <= 75 else max(0.5, 300.0 / duration)
    frames = ff.gray_frames(path, fps, 64, 36, max_frames=320).astype(np.float32) / 255.0
    if len(frames) < 3:
        return {"motion": 0.0, "cuts_per_min": 0.0, "pace": 0.3}
    d = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2))
    med = float(np.median(d))
    cuts = int(((d > 0.18) & (d > 4 * max(med, 0.01))).sum())
    span_min = max(len(frames) / fps / 60.0, 1 / 60)
    cpm = cuts / span_min
    motion = float(np.mean(np.minimum(d, 0.15)))  # cap cuts so they don't dominate motion
    # Scale: sampled-frame differences grow when frames are further apart.
    motion_n = float(np.clip(motion / (0.06 if fps >= 3 else 0.10), 0, 1))
    cuts_n = float(np.clip(cpm / 30.0, 0, 1))
    return {"motion": round(motion, 4), "cuts_per_min": round(cpm, 2),
            "pace": round(0.65 * motion_n + 0.35 * cuts_n, 3)}


def mood(path: str, duration: float, clip) -> dict[str, float] | None:
    n = 6 if duration > 6 else 3
    times = [duration * (i + 0.5) / n for i in range(n)]
    imgs = ff.rgb_frames_at(path, times, size=clip.SIZE)
    if not imgs:
        return None
    return labels.video_mood(clip.embed_images(imgs), clip)
