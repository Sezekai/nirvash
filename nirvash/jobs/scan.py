"""Find music in videos and extract it into the song library without re-encoding."""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..analysis import audio_features as af
from ..analysis import labels
from ..analysis.encoders import get_clap
from ..analysis.fingerprint import FingerprintIndex, fingerprint
from ..core import log as _log
from ..core.db import get_db
from ..core.settings import get_settings
from ..media import ff
from .context import JobContext
from .library_files import ext_for_codec, safe_name, stamp, to_recycle_bin, unique_path

log = _log.get("scan")

WIN = 10.0
HOP = 5.0
MAX_DECODE_S = 3600.0


@dataclass
class Section:
    start: float
    end: float
    partial: bool

    @property
    def length(self) -> float:
        return self.end - self.start


@dataclass
class VideoResult:
    path: str
    status: str            # music | partial | speech | silent | no_audio | skipped | error
    detail: str = ""
    sections: list[Section] = field(default_factory=list)
    added: list[int] = field(default_factory=list)
    merged: list[int] = field(default_factory=list)


STATUS_TEXT = {
    "music": "All music",
    "partial": "Music sections",
    "speech": "No music (talking / other sound)",
    "silent": "Silent",
    "no_audio": "No audio track",
    "skipped": "Already scanned",
    "error": "Error",
}

_fp_index: FingerprintIndex | None = None
_fp_lock = threading.Lock()


def fp_index() -> FingerprintIndex:
    global _fp_index
    with _fp_lock:
        if _fp_index is None:
            idx = FingerprintIndex()
            for sid, fp, _dur in get_db().fingerprints():
                idx.add(sid, fp)
            _fp_index = idx
        return _fp_index


def forget_fingerprint(song_id: int) -> None:
    with _fp_lock:
        if _fp_index is not None:
            _fp_index.remove(song_id)


def reset_fp_index() -> None:
    global _fp_index
    with _fp_lock:
        _fp_index = None


# ---------------------------------------------------------------------------
def window_starts(duration: float) -> list[float]:
    if duration <= WIN:
        return [0.0]
    starts = list(np.arange(0.0, duration - WIN + 1e-6, HOP))
    if duration - (starts[-1] + WIN) > 1.0:
        starts.append(duration - WIN)
    return [float(s) for s in starts]


def classify(y: np.ndarray, sr: int, duration: float, silence_db: float, clap) -> tuple[list[str], np.ndarray, list[float]]:
    """Label each 5 s slot as music / speech / other / quiet. Returns (labels, window embeddings, window starts)."""
    starts = window_starts(duration)
    chunks = [y[int(s * sr): int((s + WIN) * sr)] for s in starts]
    loud = [float(np.max(af.loudness_envelope(c, sr, 0.5))) if len(c) else -90.0 for c in chunks]
    embs = clap.embed_audio(chunks)
    probs = labels.audio_class_probs(embs, clap)
    n_slots = max(1, int(np.ceil(duration / HOP)))
    acc = {k: np.zeros(n_slots) for k in ("music", "speech", "other")}
    cnt = np.zeros(n_slots)
    slot_loud = np.full(n_slots, -90.0)
    for wi, s in enumerate(starts):
        a = int(round(s / HOP))
        b = min(n_slots, int(np.ceil((s + WIN) / HOP - 1e-6)))
        for k in acc:
            acc[k][a:b] += probs[k][wi]
        cnt[a:b] += 1
        slot_loud[a:b] = np.maximum(slot_loud[a:b], loud[wi])
    cnt = np.maximum(cnt, 1)
    out = []
    for i in range(n_slots):
        m, sp, ot = acc["music"][i] / cnt[i], acc["speech"][i] / cnt[i], acc["other"][i] / cnt[i]
        if slot_loud[i] < silence_db + 6:
            out.append("quiet")
        elif m >= 0.45 and m > sp and m > ot:
            out.append("music")
        elif sp >= ot:
            out.append("speech")
        else:
            out.append("other")
    return out, embs, starts


def find_sections(slot_labels: list[str], duration: float, min_len: float, full_ratio: float) -> tuple[str, list[Section]]:
    n = len(slot_labels)
    music = [l == "music" for l in slot_labels]
    audible = [l != "quiet" for l in slot_labels]
    n_music = sum(music)
    n_aud = max(1, sum(audible))
    if n_music == 0:
        return ("silent" if not any(audible) else "speech"), []
    has_speech = any(l == "speech" for l in slot_labels)
    if not has_speech and n_music / n_aud >= full_ratio:
        return "music", [Section(0.0, duration, False)]
    secs: list[Section] = []
    i = 0
    while i < n:
        if music[i]:
            j = i
            while j < n and music[j]:
                j += 1
            a, b = i * HOP, min(duration, j * HOP)
            # Pull partial boundaries in a little so they don't catch the talking next door.
            if i > 0:
                a += 1.0
            if j < n:
                b -= 1.0
            if b - a >= min_len or (b - a >= 0.8 * duration and b - a > 3):
                secs.append(Section(round(a, 2), round(b, 2), True))
            i = j
        else:
            i += 1
    if not secs:
        return "speech", []
    return "partial", secs


# ---------------------------------------------------------------------------
def extract(video: str, astream: ff.Stream, sec: Section, full: bool, songs_dir: Path) -> Path:
    ext = ext_for_codec(astream.codec)
    stem = safe_name(Path(video).stem, 120)
    name = f"{stem}{ext}" if full else f"{stem} [{stamp(sec.start)}-{stamp(sec.end)}]{ext}"
    dest = unique_path(songs_dir / name)
    tmp = dest.with_name(dest.stem + ".part" + dest.suffix)
    args = [ff.ffmpeg(), "-v", "error", "-nostdin", "-y"]
    if not full:
        args += ["-ss", f"{sec.start:.3f}"]
    args += ["-i", video]
    if not full:
        args += ["-t", f"{sec.length:.3f}"]
    args += ["-map", f"0:{astream.index}", "-vn", "-sn", "-dn", "-c:a", "copy", "-map_metadata", "-1"]
    if ext == ".mka":
        args += ["-f", "matroska"]
    elif ext == ".m4a":
        args += ["-f", "ipod" if astream.codec == "aac" else "mp4", "-movflags", "+faststart"]
    elif ext == ".opus":
        args += ["-f", "opus"]
    elif ext == ".ogg":
        args += ["-f", "ogg"]
    elif ext == ".wav":
        args += ["-f", "wav"]
    args.append(str(tmp))
    p = ff.run(args, timeout=1800)
    if p.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 1000:
        # Some containers refuse a codec; Matroska takes anything.
        if tmp.exists():
            tmp.unlink()
        dest = unique_path(dest.with_suffix(".mka"))
        tmp = dest.with_name(dest.stem + ".part.mka")
        args2 = [a for a in args[:-1]]
        if "-f" in args2:
            k = args2.index("-f")
            del args2[k:k + 2]
        if "-movflags" in args2:
            k = args2.index("-movflags")
            del args2[k:k + 2]
        p = ff.run(args2 + ["-f", "matroska", str(tmp)], timeout=1800)
        if p.returncode != 0 or not tmp.exists():
            raise ff.FFError(p.stderr.decode("utf-8", "replace")[-400:])
    tmp.replace(dest)
    return dest


def describe_file(path: Path) -> dict:
    pr = ff.probe(path)
    a = pr.audio
    br = 0
    try:
        br = int(a.raw.get("bit_rate") or pr.raw.get("format", {}).get("bit_rate") or 0) if a else 0
    except Exception:
        pass
    return {"codec": a.codec if a else "", "ext": path.suffix.lower(),
            "sample_rate": int(a.raw.get("sample_rate") or 0) if a else 0,
            "channels": int(a.raw.get("channels") or 0) if a else 0,
            "bitrate": br, "duration": pr.duration}


def song_features(path: Path, emb: np.ndarray | None, clap) -> dict:
    y = ff.decode_audio(path, af.SR)
    bpm, _beats = af.tempo_and_beats(y)
    energy = af.energy_score(y)
    out = {"bpm": bpm, "energy": energy, "pace": af.song_pace(bpm, energy)}
    if emb is not None:
        out["clap_emb"] = emb
        out["mood"] = json.dumps(labels.song_mood(emb, clap))
    return out


def add_or_merge(video: str, sec: Section, file: Path, sec_embs: np.ndarray, clap, res: VideoResult,
                 ctx: JobContext) -> None:
    db = get_db()
    info = describe_file(file)
    fp = fingerprint(str(file))
    idx = fp_index()
    match = idx.match(fp) if fp is not None and len(fp) > 20 else None
    if match:
        sid, _off, sim = match
        old = db.song(sid)
        if old is not None:
            better = info["duration"] > (old["duration"] or 0) + 1.0 or (
                abs(info["duration"] - (old["duration"] or 0)) <= 1.0 and info["bitrate"] > (old["bitrate"] or 0) * 1.15)
            db.add_source(sid, video, sec.start, sec.end)
            if better:
                old_path = Path(old["file_path"])
                emb = sec_embs.mean(axis=0) if len(sec_embs) else None
                feats = song_features(file, emb / max(np.linalg.norm(emb), 1e-8) if emb is not None else None, clap)
                db.update_song(sid, file_path=str(file), partial=int(sec.partial and bool(old["partial"])),
                               fingerprint=fp, **info, **feats)
                idx.add(sid, fp)
                if old_path.exists() and old_path != file:
                    to_recycle_bin(old_path)
                log.info("duplicate of song %d (%.0f%% match); kept the new, longer copy", sid, sim * 100)
            else:
                file.unlink(missing_ok=True)
                log.info("duplicate of song %d (%.0f%% match); kept the existing copy", sid, sim * 100)
            res.merged.append(sid)
            ctx.event("song_changed", sid)
            return
    emb = sec_embs.mean(axis=0) if len(sec_embs) else None
    if emb is not None:
        emb = emb / max(np.linalg.norm(emb), 1e-8)
    feats = song_features(file, emb, clap)
    sid = db.add_song(file_path=str(file), partial=int(sec.partial), fingerprint=fp, id_status="pending",
                      **info, **feats)
    db.add_source(sid, video, sec.start, sec.end)
    if fp is not None:
        idx.add(sid, fp)
    res.added.append(sid)
    ctx.event("song_added", sid)


def scan_video(path: Path, ctx: JobContext) -> VideoResult:
    s = get_settings()
    db = get_db()
    vp = str(path)
    st = path.stat()
    prev = db.scanned(vp)
    if prev and not s.get("scan.rescan") and prev["size"] == st.st_size and abs((prev["mtime"] or 0) - st.st_mtime) < 1e-3:
        return VideoResult(vp, "skipped", STATUS_TEXT.get(prev["result"], prev["result"]))
    res = VideoResult(vp, "error")
    try:
        pr = ff.probe(path)
        a = pr.audio
        if a is None:
            res.status = "no_audio"
        else:
            dur = min(pr.duration or 0, MAX_DECODE_S)
            y = ff.decode_audio(path, 48000, duration=dur, stream=a.index)
            dur = len(y) / 48000
            silence_db = float(s.get("scan.silence_db", -50))
            if dur < 1.0 or float(np.max(af.loudness_envelope(y, 48000, 0.5))) < silence_db:
                res.status = "silent"
            else:
                clap = get_clap()
                ctx.check()
                slot_labels, embs, starts = classify(y, 48000, dur, silence_db, clap)
                status, secs = find_sections(slot_labels, dur, float(s.get("scan.min_section_s", 12)),
                                             float(s.get("scan.full_ratio", 0.9)))
                res.status = status
                res.sections = secs
                songs_dir = s.folder("songs")
                for sec in secs:
                    ctx.check()
                    full = not sec.partial
                    w_idx = [i for i, st0 in enumerate(starts) if st0 >= sec.start - 2 and st0 + WIN <= sec.end + 2]
                    if not w_idx:
                        w_idx = [i for i, st0 in enumerate(starts) if st0 < sec.end and st0 + WIN > sec.start]
                    f = extract(vp, a, sec, full, songs_dir)
                    add_or_merge(vp, sec, f, embs[w_idx] if w_idx else embs[:0], clap, res, ctx)
                res.detail = ", ".join(f"{ff.fmt_time(x.start)}-{ff.fmt_time(x.end)}" for x in secs)
    except InterruptedError:
        raise
    except Exception as e:
        log.exception("scan failed for %s", vp)
        res.status = "error"
        res.detail = str(e)[:300]
    if res.status != "error":
        db.mark_scanned(vp, st.st_size, st.st_mtime, res.status, res.detail)
    return res


def scan_folders(folders: list[str], ctx: JobContext) -> list[VideoResult]:
    s = get_settings()
    videos: list[Path] = []
    seen = set()
    for f in folders:
        for v in ff.list_videos(f, bool(s.get("scan.recursive", True))):
            k = str(v).lower()
            if k not in seen:
                seen.add(k)
                videos.append(v)
    # Never scan the library itself.
    songs_dir = str(s.folder("songs")).lower()
    videos = [v for v in videos if not str(v).lower().startswith(songs_dir)]
    ctx.event("total", len(videos))
    results = []
    for i, v in enumerate(videos):
        ctx.check()
        ctx.progress(i / max(1, len(videos)), v.name)
        r = scan_video(v, ctx)
        results.append(r)
        ctx.event("video_done", r)
    ctx.progress(1.0, "Done")
    return results
