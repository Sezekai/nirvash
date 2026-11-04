"""File helpers for the song library."""
from __future__ import annotations

import re
from pathlib import Path

from ..core import log as _log

log = _log.get("files")

# Audio codec -> file extension that holds it without re-encoding.
CODEC_EXT = {
    "aac": ".m4a", "alac": ".m4a",
    "opus": ".opus",
    "vorbis": ".ogg",
    "mp3": ".mp3", "mp2": ".mp2",
    "flac": ".flac",
    "ac3": ".ac3", "eac3": ".eac3",
    "pcm_s16le": ".wav", "pcm_s24le": ".wav", "pcm_f32le": ".wav", "pcm_u8": ".wav", "pcm_s16be": ".wav",
}


def ext_for_codec(codec: str) -> str:
    return CODEC_EXT.get(codec, ".mka")


_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(s: str, limit: int = 150) -> str:
    s = _BAD.sub("_", s).strip().rstrip(".")
    return (s[:limit].rstrip() or "song")


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    i = 2
    while True:
        cand = p.with_name(f"{p.stem} ({i}){p.suffix}")
        if not cand.exists():
            return cand
        i += 1


def stamp(t: float) -> str:
    m, s = divmod(int(round(t)), 60)
    return f"{m}.{s:02d}"


def to_recycle_bin(p: Path) -> bool:
    try:
        from send2trash import send2trash
        send2trash(str(p))
        return True
    except Exception as e:
        log.warning("could not send %s to the Recycle Bin: %s", p, e)
        return False
