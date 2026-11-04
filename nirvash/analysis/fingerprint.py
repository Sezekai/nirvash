"""Chromaprint fingerprints (fpcalc) for spotting the same song in different clips."""
from __future__ import annotations

import json
from collections import Counter, defaultdict

import numpy as np

from ..core.paths import bin_path
from ..media.ff import run

ITEM_S = 0.1238  # seconds per fingerprint item (Chromaprint default)
_POP = np.array([bin(i).count("1") for i in range(256)], np.uint8)


def fingerprint(path: str, max_seconds: float = 900) -> np.ndarray | None:
    p = run([bin_path("fpcalc"), "-raw", "-json", "-length", str(int(max_seconds)), str(path)], timeout=300)
    if p.returncode != 0 or not p.stdout:
        return None
    try:
        data = json.loads(p.stdout)
        return np.asarray(data["fingerprint"], dtype=np.int64).astype(np.uint32)
    except Exception:
        return None


def fingerprint_compressed(path: str, max_seconds: float = 120) -> tuple[str, int] | None:
    """Compressed fingerprint + duration for AcoustID lookups."""
    p = run([bin_path("fpcalc"), "-json", "-length", str(int(max_seconds)), str(path)], timeout=300)
    if p.returncode != 0 or not p.stdout:
        return None
    try:
        data = json.loads(p.stdout)
        return data["fingerprint"], int(round(float(data["duration"])))
    except Exception:
        return None


def bit_similarity(a: np.ndarray, b: np.ndarray, offset: int) -> tuple[float, int]:
    """Fraction of equal bits when b is shifted by `offset` items against a; also overlap length."""
    if offset >= 0:
        x, y = a[offset:], b
    else:
        x, y = a, b[-offset:]
    n = min(len(x), len(y))
    if n <= 0:
        return 0.0, 0
    xor = np.bitwise_xor(x[:n], y[:n]).view(np.uint8)
    bits = int(_POP[xor].sum())
    return 1.0 - bits / (32.0 * n), n


class FingerprintIndex:
    """Inverted index over the top bits of each fingerprint item, to find candidate matches fast."""

    SHIFT = 12

    def __init__(self):
        self.fps: dict[int, np.ndarray] = {}
        self.index: dict[int, list[tuple[int, int]]] = defaultdict(list)

    def add(self, song_id: int, fp: np.ndarray) -> None:
        self.remove(song_id)
        self.fps[song_id] = fp
        for pos, v in enumerate((fp >> self.SHIFT).tolist()):
            self.index[v].append((song_id, pos))

    def remove(self, song_id: int) -> None:
        if song_id not in self.fps:
            return
        fp = self.fps.pop(song_id)
        for v in set((fp >> self.SHIFT).tolist()):
            lst = self.index.get(v)
            if lst:
                self.index[v] = [e for e in lst if e[0] != song_id]

    def match(self, fp: np.ndarray, min_overlap_s: float = 8.0, threshold: float = 0.80) -> tuple[int, int, float] | None:
        """Best (song_id, offset, similarity) matching `fp`, or None.

        offset > 0 means the new clip starts `offset` items into the stored song."""
        votes: Counter = Counter()
        for pos, v in enumerate((fp >> self.SHIFT).tolist()):
            for sid, opos in self.index.get(v, ()):
                votes[(sid, opos - pos)] += 1
        best = None
        min_items = int(min_overlap_s / ITEM_S)
        for (sid, off), cnt in votes.most_common(12):
            if cnt < 4:
                break
            for o in (off - 1, off, off + 1):
                sim, n = bit_similarity(self.fps[sid], fp, o)
                if n >= min(min_items, len(fp) - 2) and sim >= threshold and (best is None or sim > best[2]):
                    best = (sid, o, sim)
        return best
