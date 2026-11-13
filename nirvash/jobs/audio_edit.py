"""Trimming, volume and re-cutting songs.

Every edit is made from a kept original, never from the last edited file, so changes don't stack up quality loss
and can always be undone:

- The first edit of a song copies its file into the Originals folder (Library\\Originals by default).
- The library file is then rebuilt from that original with the song's settings: where it starts and ends
  (trim_start / trim_end, seconds into the original) and its volume (gain_db).
- Trimming alone copies the audio as it is (no re-encoding). A volume change has to re-encode; it's done once,
  from the original, at a high bitrate in the same format, so the song sounds the same in any player.
- Re-cutting takes a new stretch of the song's source video as the new original (the old one goes to the
  Recycle Bin), the same lossless way a scan does.
- Tags, cover art and the file name of the library file are kept.

Loudness is measured as EBU R128 integrated loudness (LUFS) with the true peak, using ffmpeg's loudnorm filter.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from ..core import log as _log
from ..core.db import get_db
from ..core.settings import get_settings
from ..media import ff
from .context import JobContext
from .library_files import ext_for_codec, to_recycle_bin, unique_path

log = _log.get("audio_edit")

_JSON = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.S)


class EditError(RuntimeError):
    pass


# ---- loudness -----------------------------------------------------------------------------
@dataclass
class Loudness:
    lufs: float        # integrated loudness
    peak: float        # true peak, dBTP


def measure(path: str | Path, start: float | None = None, end: float | None = None) -> Loudness | None:
    """Loudness of the file (or the stretch from start to end). None when it can't be measured (silence)."""
    args = [ff.ffmpeg(), "-hide_banner", "-nostdin", "-v", "info"]
    if start:
        args += ["-ss", f"{start:.3f}"]
    args += ["-i", str(path)]
    if end is not None:
        args += ["-t", f"{max(0.1, end - (start or 0)):.3f}"]
    args += ["-map", "0:a:0", "-vn", "-af", "loudnorm=print_format=json", "-f", "null", "-"]
    p = ff.run(args, timeout=1800)
    m = _JSON.search(p.stderr.decode("utf-8", "replace"))
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
        lufs, peak = float(d["input_i"]), float(d["input_tp"])
    except (ValueError, KeyError):
        return None
    if lufs < -69:          # silence
        return None
    return Loudness(lufs, peak)


def level_gain(l: Loudness | None, target: float | None = None, max_gain: float | None = None) -> float:
    """The change that brings a song to the target loudness without its peaks going over -1 dBTP."""
    if l is None:
        return 0.0
    s = get_settings()
    target = float(s.get("level.target", -14.0)) if target is None else target
    max_gain = float(s.get("level.max_gain", 12.0)) if max_gain is None else max_gain
    g = target - l.lufs
    g = min(g, -1.0 - l.peak)                # keep headroom: raising a song never makes it clip
    g = max(-max_gain, min(max_gain, g))
    return round(g * 2) / 2                   # half-dB steps


# ---- originals --------------------------------------------------------------------------------
def originals_dir() -> Path:
    return get_settings().folder("originals")


def original_path(sid: int) -> Path | None:
    r = get_db().song(sid)
    if r is None or not r["orig_name"]:
        return None
    p = originals_dir() / r["orig_name"]
    return p if p.exists() else None


def source_for_editing(sid: int) -> Path:
    """The file edits start from: the kept original, or the library file if the song was never edited."""
    o = original_path(sid)
    if o is not None:
        return o
    r = get_db().song(sid)
    if r is None:
        raise EditError("That song isn't in the library any more.")
    p = Path(r["file_path"])
    if not p.exists():
        raise EditError("The song's file is missing.")
    return p


def keep_original(sid: int) -> Path:
    """Copy the library file into Originals the first time a song is edited."""
    o = original_path(sid)
    if o is not None:
        return o
    r = get_db().song(sid)
    src = Path(r["file_path"])
    if not src.exists():
        raise EditError("The song's file is missing.")
    dest = unique_path(originals_dir() / f"{sid}{src.suffix.lower()}")
    tmp = dest.with_name(dest.stem + ".part" + dest.suffix)
    shutil.copy2(src, tmp)
    tmp.replace(dest)
    get_db().update_song(sid, orig_name=dest.name)
    log.info("kept the original of song %d as %s", sid, dest.name)
    return dest


def is_edited(r) -> bool:
    return bool(r["orig_name"]) and (bool(r["trim_start"]) or r["trim_end"] is not None or abs(r["gain_db"] or 0) >= 0.05)


# ---- building the library file ------------------------------------------------------------------
def _encoder(codec: str, ext: str, bitrate: int) -> list[str]:
    """Same format, high quality: used only when the volume changes."""
    s = get_settings()
    kbps = max(192, min(320, int((bitrate or 0) / 1000 * 1.15) or 256))
    if ext == ".m4a" or codec == "aac":
        return ["-c:a", "aac", "-b:a", s.get("export.aac_bitrate", f"{kbps}k") or f"{kbps}k"]
    if ext == ".mp3" or codec == "mp3":
        return ["-c:a", "libmp3lame", "-b:a", s.get("export.mp3_bitrate", "320k")]
    if ext == ".opus" or codec == "opus":
        return ["-c:a", "libopus", "-b:a", s.get("export.opus_bitrate", "256k")]
    if ext == ".ogg" or codec == "vorbis":
        return ["-c:a", "libvorbis", "-q:a", "8"]
    if ext == ".flac" or codec == "flac":
        return ["-c:a", "flac"]
    if ext == ".wav":
        return ["-c:a", "pcm_s16le"]
    return ["-c:a", "flac"]            # anything else (in .mka) becomes lossless FLAC


def _muxer(ext: str, codec: str) -> list[str]:
    if ext == ".m4a":
        return ["-f", "ipod" if codec == "aac" else "mp4", "-movflags", "+faststart"]
    return {".mka": ["-f", "matroska"], ".opus": ["-f", "opus"], ".ogg": ["-f", "ogg"], ".wav": ["-f", "wav"],
            ".mp3": ["-f", "mp3"], ".flac": ["-f", "flac"]}.get(ext, ["-f", "matroska"])


def render(ctx: JobContext | None, sid: int, *, trim_start: float = 0.0, trim_end: float | None = None,
           gain_db: float = 0.0) -> dict:
    """Rebuild the library file from the original with these settings. Returns what changed."""
    ctx = ctx or JobContext()
    db = get_db()
    r = db.song(sid)
    if r is None:
        raise EditError("That song isn't in the library any more.")
    out = Path(r["file_path"])
    orig = keep_original(sid)
    pr = ff.probe(orig)
    a = pr.audio
    if a is None:
        raise EditError("The original has no audio.")
    total = pr.duration or (r["duration"] or 0)
    ts = max(0.0, float(trim_start or 0.0))
    te = None if trim_end is None or (total and trim_end >= total - 0.02) else float(trim_end)
    if te is not None and te - ts < 1.0:
        raise EditError("Keep at least a second of the song.")
    gain = round(float(gain_db or 0.0), 2)
    length = (te if te is not None else total) - ts

    ext = out.suffix.lower()
    tmp = out.with_name(out.stem + ".part" + out.suffix)
    keep_tags = out.exists()
    reencode = abs(gain) >= 0.05

    def build(with_cover: bool) -> list[str]:
        # input 0: the original (audio); input 1: the library file as it is now (its tags and cover art)
        args = [ff.ffmpeg(), "-v", "error", "-y"]
        if ts > 0:
            args += ["-ss", f"{ts:.3f}"]
        args += ["-i", str(orig)]
        if keep_tags:
            args += ["-i", str(out)]
        if te is not None:
            args += ["-t", f"{length:.3f}"]
        args += ["-map", "0:a:0"]
        if with_cover:
            args += ["-map", "1:v?", "-c:v", "copy", "-disposition:v", "attached_pic"]
        if keep_tags:
            args += ["-map_metadata", "1"]
        if reencode:
            args += ["-af", f"volume={gain:.2f}dB"] + _encoder(a.codec, ext, int(r["bitrate"] or 0))
        else:
            args += ["-c:a", "copy"]
        return args + _muxer(ext, a.codec) + [str(tmp)]

    how = "re-encoded once from the original" if reencode else "audio copied as it is"
    ctx.progress(0.05, "Building the song")
    cover = keep_tags and ext in (".m4a", ".mp3", ".flac")
    for attempt in ((True, False) if cover else (False,)):
        tmp.unlink(missing_ok=True)
        try:
            ff.run_with_progress(build(attempt), length,
                                 lambda f: ctx.progress(0.05 + 0.8 * f, "Building the song"), ctx.cancel)
            break
        except ff.FFError as e:
            tmp.unlink(missing_ok=True)
            if attempt:          # a cover the format won't take: try again without it
                log.warning("song %d: cover art not copied (%s)", sid, str(e)[-200:])
                continue
            raise EditError(f"ffmpeg couldn't build the song: {e}") from e
        except ff.Cancelled:
            tmp.unlink(missing_ok=True)
            raise InterruptedError("cancelled") from None
    if not tmp.exists() or tmp.stat().st_size < 500:
        tmp.unlink(missing_ok=True)
        raise EditError("ffmpeg made an empty file.")
    ctx.progress(0.9, "Swapping the file in")
    try:
        tmp.replace(out)
    except PermissionError as e:
        tmp.unlink(missing_ok=True)
        raise EditError("The song's file is in use (is it playing somewhere else?). Nothing was changed.") from e
    from .scan import describe_file
    info = describe_file(out)
    db.update_song(sid, trim_start=ts or None, trim_end=te, gain_db=gain, duration=info["duration"],
                   bitrate=info["bitrate"])
    log.info("song %d rebuilt: %.2f-%s s, %+.1f dB (%s)", sid, ts, f"{te:.2f}" if te else "end", gain, how)
    ctx.progress(1.0, "Done")
    return {"sid": sid, "duration": info["duration"], "gain": gain, "how": how}


def reset(ctx: JobContext | None, sid: int) -> dict:
    """Put the song back exactly as it was first added (or last re-cut)."""
    return render(ctx, sid)


# ---- re-cutting from the video ----------------------------------------------------------------------------
def recut(ctx: JobContext | None, sid: int, video: str, start: float, end: float, gain_db: float | None = None) -> dict:
    """Take start..end of the source video as the song's new original, then rebuild the library file."""
    ctx = ctx or JobContext()
    db = get_db()
    r = db.song(sid)
    if r is None:
        raise EditError("That song isn't in the library any more.")
    if not Path(video).exists():
        raise EditError("The video isn't there any more (moved, deleted, or its drive isn't connected).")
    if end - start < 1.0:
        raise EditError("Keep at least a second of the song.")
    pr = ff.probe(video)
    a = pr.audio
    if a is None:
        raise EditError("That video has no sound.")
    ctx.progress(0.02, "Cutting the song from the video")
    from .scan import Section, extract
    tmpdir = originals_dir() / "_recut"
    tmpdir.mkdir(parents=True, exist_ok=True)
    full = start <= 0.05 and pr.duration and end >= pr.duration - 0.05
    new = extract(video, a, Section(start, end, partial=not full), bool(full), tmpdir)
    old = original_path(sid)
    dest = unique_path(originals_dir() / f"{sid}-{int(time.time())}{new.suffix.lower()}")
    new.replace(dest)
    if old is not None:
        to_recycle_bin(old)        # the previous cut can still be got back from the Recycle Bin
    db.update_song(sid, orig_name=dest.name, trim_start=None, trim_end=None, loudness=None, true_peak=None)
    # the library file keeps its name, but follows the new original's format
    lib = Path(r["file_path"])
    if lib.suffix.lower() != dest.suffix.lower():
        newlib = unique_path(lib.with_suffix(dest.suffix.lower()))
        if lib.exists():
            shutil.copy2(dest, newlib)        # starts as the new audio; render() then adds tags back where it can
            to_recycle_bin(lib)
        db.update_song(sid, file_path=str(newlib), ext=dest.suffix.lower(), codec=a.codec)
    # the source link now says which part of the video the song is
    db.x("UPDATE song_sources SET start=?, end=? WHERE song_id=? AND video_path=?", (start, end, sid, video))
    l = measure(dest)
    if l is not None:
        db.update_song(sid, loudness=l.lufs, true_peak=l.peak)
    g = (r["gain_db"] or 0.0) if gain_db is None else gain_db
    res = render(ctx, sid, gain_db=g)
    db.update_song(sid, full_length=None if not full else res["duration"], partial=int(not full))
    try:
        from ..analysis.fingerprint import fingerprint
        from .scan import fp_index
        fp = fingerprint(str(Path(get_db().song(sid)["file_path"])))
        if fp is not None:
            db.update_song(sid, fingerprint=fp)
            fp_index().add(sid, fp)
    except Exception as e:  # noqa: BLE001 - only used to spot duplicates
        log.warning("couldn't refresh the fingerprint of song %d: %s", sid, e)
    return res


# ---- evening out loud and quiet songs ------------------------------------------------------------------------
# The song the library is playing (set by the library page): its file is open, so it's left for next time.
playing: dict[str, int | None] = {"sid": None}


def _is_playing(sid: int) -> bool:
    return playing["sid"] == sid

def songs_to_level() -> list[int]:
    return [r["id"] for r in get_db().q("SELECT id FROM songs WHERE level_done=0 ORDER BY id")]


def level_songs(ctx: JobContext, ids: list[int] | None = None, busy=_is_playing, force: bool = False) -> dict:
    """Measure songs and bring the ones well away from the target loudness towards it.

    Songs within the tolerance are only measured, never touched. Songs whose volume was set by hand are left
    alone unless force is set (Selected > Even out volume). `busy(sid)` says a song is playing: it's left for
    next time rather than swapped under the player."""
    s = get_settings()
    db = get_db()
    tol = float(s.get("level.tolerance", 2.0))
    ids = songs_to_level() if ids is None else ids
    changed, measured, left, failed = 0, 0, 0, 0
    t0 = time.time()
    for n, sid in enumerate(ids):
        ctx.check()
        r = db.song(sid)
        if r is None:
            continue
        name = r["title"] or Path(r["file_path"]).stem
        ctx.progress(n / max(1, len(ids)), f"Song {n + 1} of {len(ids)} · {name}")
        if r["level_done"] and not force:
            continue
        if busy(sid):
            left += 1
            continue
        if not Path(r["file_path"]).exists():       # missing file or drive: tried again next time
            left += 1
            continue
        try:
            src = source_for_editing(sid)
            l = measure(src, r["trim_start"], r["trim_end"])
            measured += 1
            if l is None:
                db.update_song(sid, level_done=1)
                continue
            db.update_song(sid, loudness=l.lufs, true_peak=l.peak)
            # the song as it plays now: the measured original plus any gain already applied
            now = l.lufs + (r["gain_db"] or 0.0)
            if abs(now - float(s.get("level.target", -14.0))) <= tol and not force:
                db.update_song(sid, level_done=1)
                continue
            g = level_gain(l)
            if abs(g - (r["gain_db"] or 0.0)) < 0.5:
                db.update_song(sid, level_done=1)
                continue
            render(None, sid, trim_start=r["trim_start"] or 0.0, trim_end=r["trim_end"], gain_db=g)
            db.update_song(sid, level_done=1)
            changed += 1
            ctx.event("song_changed", sid)
        except (EditError, ff.FFError, OSError) as e:
            failed += 1
            log.warning("couldn't even out song %d: %s", sid, e)
    ctx.progress(1.0, "")
    log.info("evened out %d of %d songs in %.0f s (%d left for later, %d failed)", changed, measured,
             time.time() - t0, left, failed)
    return {"changed": changed, "measured": measured, "left": left, "failed": failed}
