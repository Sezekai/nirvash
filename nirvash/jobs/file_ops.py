"""Rename song files after the song, write tags Explorer shows, and remove links to the source videos.

Everything here copies the audio stream untouched (-c copy): only the file name and the tags change."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Callable, Iterable

from ..core import log as _log
from ..core.db import get_db
from ..core.settings import get_settings
from ..media import ff
from .context import JobContext
from .library_files import safe_name, unique_path

log = _log.get("files")

# Containers ffmpeg can write tags into (and which ones also take cover art Explorer shows).
MUXER = {".m4a": "ipod", ".mp3": "mp3", ".flac": "flac", ".opus": "opus", ".ogg": "ogg", ".wav": "wav",
         ".mka": "matroska"}
COVER_OK = {".m4a", ".mp3", ".flac"}


class FileBusy(Exception):
    """The file is open in another program (or the player) and can't be replaced."""


# ---------------------------------------------------------------------------
# Names
def fmt_len(sec: float) -> str:
    m, s = divmod(int(round(sec or 0)), 60)
    return f"{m}m {s:02d}s"


def base_name(r) -> str:
    """'Artist - Title' for identified songs, 'Unidentified song 412 (2m 35s)' otherwise."""
    if r["title"]:
        return f"{r['artist']} - {r['title']}" if r["artist"] else r["title"]
    return f"Unidentified song {r['id']} ({fmt_len(r['duration'])})"


# What can tell two copies of the same song apart, in the order it's added to the name.
_DIFFS: list[Callable] = [
    lambda r: fmt_len(r["duration"]),
    lambda r: f"{r['bpm']:.0f} BPM" if r["bpm"] else "",
    lambda r: f"{(r['bitrate'] or 0) // 1000} kbps" if r["bitrate"] else "",
    lambda r: (r["ext"] or Path(r["file_path"]).suffix).lstrip(".").upper(),
]


def song_names(rows: Iterable) -> dict[int, str]:
    """File name (without extension) for each song. Copies of the same song get what differs between them
    (length, then BPM, bitrate, format) added, e.g. 'Artist - Title (3m 25s)' and 'Artist - Title (4m 02s)'."""
    rows = list(rows)
    groups: dict[str, list] = {}
    for r in rows:
        if r["title"]:
            groups.setdefault(base_name(r).casefold(), []).append(r)
    out: dict[int, str] = {}
    for r in rows:
        if not r["title"]:
            out[r["id"]] = base_name(r)
    for grp in groups.values():
        if len(grp) == 1:
            out[grp[0]["id"]] = base_name(grp[0])
            continue
        chosen: list[Callable] = []

        def labels():
            return [", ".join(x for x in (f(r) for f in chosen) if x) for r in grp]

        for f in _DIFFS:
            ls = labels()
            if chosen and len(set(ls)) == len(ls):
                break
            if len({f(r) for r in grp}) > 1:
                chosen.append(f)
        for r, lab in zip(grp, labels()):
            out[r["id"]] = f"{base_name(r)} ({lab})" if lab else base_name(r)
    return out


def library_names(ids: Iterable[int] | None = None) -> dict[int, str]:
    """Names worked out against the whole library, so versions are told apart even when only one is renamed."""
    names = song_names(get_db().songs())
    return names if ids is None else {i: names[i] for i in ids if i in names}


def target_path(src: Path, stem: str) -> Path:
    stem = safe_name(stem, 150)
    # Already named this (maybe with a ' (2)' that kept it apart from another file): leave it.
    if re.fullmatch(re.escape(stem) + r"( \(\d+\))?", src.stem):
        return src
    want = src.with_name(stem + src.suffix)
    if str(want).casefold() == str(src).casefold():
        return want                       # only the letter case changes
    return unique_path(want)


# ---------------------------------------------------------------------------
# Writing
def _tag_args(r, ext: str) -> list[str]:
    args = []
    for key, val in (("title", r["title"]), ("artist", r["artist"]), ("album_artist", r["artist"]),
                     ("album", r["album"]), ("genre", r["genre"]), ("date", r["year"])):
        if val:
            args += ["-metadata", f"{key}={val}"]
    return args


def _replace(src: Path, dst: Path, tries: int = 12) -> None:
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise FileBusy(dst.name)
            time.sleep(0.25)


def _rename(src: Path, dst: Path, tries: int = 12) -> None:
    if src == dst:
        return
    for i in range(tries):
        try:
            os.rename(src, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise FileBusy(src.name)
            time.sleep(0.25)


def rewrite_tags(path: Path, r, with_info: bool) -> bool:
    """Replace the file's tags in place (audio copied untouched). with_info=False strips every tag.
    Returns False when the format can't hold tags (the file is left alone)."""
    ext = path.suffix.lower()
    fmt = MUXER.get(ext)
    if fmt is None:
        return False
    if ext == ".m4a" and (r["codec"] or "aac") != "aac":
        fmt = "mp4"
    before = ff.probe(path)
    cover = get_settings().cache("covers") / f"{r['id']}.jpg"
    use_cover = with_info and ext in COVER_OK and cover.exists() and cover.stat().st_size > 500
    tmp = path.with_name(f".nirvash-tag-{r['id']}{ext}")
    for attempt_cover in ((True, False) if use_cover else (False,)):
        args = [ff.ffmpeg(), "-v", "error", "-nostdin", "-y", "-i", str(path)]
        if attempt_cover:
            args += ["-i", str(cover)]
        args += ["-map", "0:a"]
        if attempt_cover:
            args += ["-map", "1:v", "-disposition:v:0", "attached_pic"]
        args += ["-c", "copy", "-map_metadata", "-1", "-map_chapters", "-1"]
        if with_info:
            args += _tag_args(r, ext)
        if ext == ".mp3":
            args += ["-id3v2_version", "3", "-write_id3v1", "1"]
        if fmt in ("ipod", "mp4"):
            args += ["-movflags", "+faststart"]
        args += ["-f", fmt, str(tmp)]
        p = ff.run(args, timeout=600)
        if p.returncode == 0 and tmp.exists() and _same_audio(before, tmp):
            break
        log.warning("tagging %s%s failed: %s", path.name, " with cover" if attempt_cover else "",
                    p.stderr.decode("utf-8", "replace")[-300:])
        tmp.unlink(missing_ok=True)
    else:
        return False
    try:
        _replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return True


def _same_audio(before: ff.Probe, after_path: Path) -> bool:
    try:
        after = ff.probe(after_path)
    except Exception:
        return False
    a, b = before.audio, after.audio
    if a is None or b is None or a.codec != b.codec:
        return False
    return abs((before.duration or 0) - (after.duration or 0)) <= 0.25


def rename_and_tag(sid: int, stem: str | None = None, with_info: bool = True) -> Path:
    """Rename the song's file to `stem` (default: its song name) and write its tags. Updates the library."""
    db = get_db()
    r = db.song(sid)
    if r is None:
        raise KeyError(sid)
    src = Path(r["file_path"])
    if not src.exists():
        raise FileNotFoundError(src.name)
    stem = stem or library_names([sid]).get(sid) or base_name(r)
    tagged = rewrite_tags(src, r, with_info and bool(r["title"]))
    dst = target_path(src, stem)
    _rename(src, dst)
    db.update_song(sid, file_path=str(dst), tagged=int(tagged and bool(r["title"]) and with_info))
    return dst


def unlink_videos(sid: int, stem: str | None = None) -> Path:
    """Forget which videos the song came from: drop the links, rename the file after the song (or
    'Unidentified song N') and strip anything left in its tags. Later scans never link it again."""
    db = get_db()
    db.remove_sources(sid)
    db.update_song(sid, unlinked=1)
    return rename_and_tag(sid, stem)


# ---------------------------------------------------------------------------
# Batch jobs (run on a worker thread; progress drives the TaskProgress bar)
def _batch(ctx: JobContext, ids: list[int], fn: Callable[[int, str | None], Path], verb: str) -> dict:
    names = library_names()
    done, failed = [], []
    for i, sid in enumerate(ids):
        ctx.check()
        ctx.progress(i / max(1, len(ids)), f"{verb} song {i + 1} of {len(ids)}")
        try:
            fn(sid, names.get(sid))
            done.append(sid)
        except FileBusy as e:
            failed.append((sid, f"{e} is open in another program"))
        except Exception as e:  # noqa: BLE001
            log.warning("%s song %d failed: %s", verb.lower(), sid, e)
            failed.append((sid, str(e) or e.__class__.__name__))
        ctx.event("song_changed", sid)
    ctx.progress(1.0, "Done")
    return {"done": done, "failed": failed}


def rename_batch(ctx: JobContext, ids: list[int]) -> dict:
    """Identified songs only: the rest have no title to name them after."""
    db = get_db()
    named = [i for i in ids if (r := db.song(i)) is not None and r["title"]]
    res = _batch(ctx, named, lambda sid, stem: rename_and_tag(sid, stem), "Renaming")
    res["skipped"] = len(ids) - len(named)
    return res


def unlink_batch(ctx: JobContext, ids: list[int]) -> dict:
    return _batch(ctx, ids, unlink_videos, "Unlinking")


def delete_songs(ctx: JobContext, ids: list[int], skip_videos: bool, skip_songs: bool,
                 binner: Callable[[Path], bool]) -> dict:
    """Send the songs' files to the Recycle Bin and forget them. skip_videos: later scans skip the videos they
    came from. skip_songs: the same music is never added again, from any video."""
    from . import scan as scanjob
    db = get_db()
    done, failed = [], []
    covers = get_settings().cache("covers")
    for i, sid in enumerate(ids):
        ctx.check()
        ctx.progress(i / max(1, len(ids)), f"Deleting song {i + 1} of {len(ids)}")
        r = db.song(sid)
        if r is None:
            continue
        p = Path(r["file_path"])
        if p.exists() and not binner(p):
            failed.append((sid, f"couldn't move {p.name} to the Recycle Bin"))
            continue
        if skip_videos:
            for s in db.sources(sid):
                db.ignore_video(s["video_path"])
        if skip_songs and r["fingerprint"]:
            db.ignore_song(r["fingerprint"], r["duration"] or 0)
        scanjob.forget_fingerprint(sid)
        db.delete_song(sid)
        (covers / f"{sid}.jpg").unlink(missing_ok=True)
        done.append(sid)
        ctx.event("song_changed", sid)
    scanjob.reset_ignored_index()
    ctx.progress(1.0, "Done")
    return {"done": done, "failed": failed}
