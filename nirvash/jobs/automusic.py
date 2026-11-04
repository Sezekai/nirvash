"""Auto-music: analyse silent videos, pick songs, build the music track, export losslessly."""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..analysis import audio_features as af
from ..analysis import labels
from ..analysis import video_features as vf
from ..analysis.encoders import ModelMissing, get_clip
from ..core import log as _log
from ..core.db import get_db
from ..core.settings import get_settings
from ..media import ff
from .context import JobContext
from .library_files import unique_path

log = _log.get("automusic")


# ---------------------------------------------------------------------------
@dataclass
class AutoItem:
    video: str
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    vcodec: str = ""
    has_sound: bool = False
    features: dict = field(default_factory=dict)
    song_id: int | None = None
    score: float = 0.0
    alts: list[list] = field(default_factory=list)      # [[song_id, score], ...]
    chain_ids: list[int] = field(default_factory=list)
    mode: str | None = None                             # None = default from Settings
    offset: float = 0.0
    volume: float | None = None
    fade_in: float | None = None
    fade_out: float | None = None
    ready: bool = False
    status: str = "new"                                 # new | matched | no_match | exported | error
    output: str = ""
    message: str = ""
    manual: bool = False

    def to_json(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_json(d: dict) -> "AutoItem":
        known = {k: v for k, v in d.items() if k in AutoItem.__dataclass_fields__}
        return AutoItem(**known)


def eff(item: AutoItem, key: str) -> Any:
    """An item's setting, or the default from Settings."""
    s = get_settings()
    v = getattr(item, key)
    if v is not None:
        return v
    return {
        "mode": s.get("automusic.short_mode", "chain"),
        "volume": float(s.get("automusic.volume", 1.0)),
        "fade_in": float(s.get("automusic.fade_in_s", 0.5)),
        "fade_out": float(s.get("automusic.fade_out_s", 2.0)),
    }[key]


# ---------------------------------------------------------------------------
# Video analysis
def analyse_video(item: AutoItem, ctx: JobContext | None = None) -> None:
    db = get_db()
    p = Path(item.video)
    st = p.stat()
    pr = ff.probe(p)
    item.duration = pr.duration
    item.width, item.height, item.fps = pr.width, pr.height, round(pr.fps, 3)
    item.vcodec = pr.video.codec if pr.video else ""
    item.has_sound = False
    if pr.audio is not None:
        try:
            y = ff.decode_audio(p, 8000, duration=min(pr.duration, 600))
            item.has_sound = bool(len(y)) and float(np.max(af.loudness_envelope(y, 8000, 0.5))) > \
                float(get_settings().get("scan.silence_db", -50))
        except Exception:
            item.has_sound = False
    cached = db.video_features(item.video, st.st_size, st.st_mtime)
    if cached and "pace" in cached and ("mood" in cached or cached.get("no_clip")):
        item.features = cached
        return
    feats = vf.motion_and_cuts(item.video, pr.duration)
    try:
        clip = get_clip()
        feats["mood"] = vf.mood(item.video, pr.duration, clip)
    except ModelMissing as e:
        feats["no_clip"] = True
        if ctx:
            ctx.event("warning", str(e))
    except Exception as e:
        log.warning("CLIP failed on %s: %s", p.name, e)
        feats["no_clip"] = True
    item.features = feats
    db.save_video_features(item.video, st.st_size, st.st_mtime, feats)


# ---------------------------------------------------------------------------
# Scoring
@dataclass
class SongInfo:
    id: int
    path: str
    duration: float
    pace: float | None
    mood: dict | None
    favourite: bool
    partial_unchecked: bool
    cats: set
    used: int


def load_songs() -> list[SongInfo]:
    db = get_db()
    cats = db.all_song_categories()
    usage = db.usage_counts()
    out = []
    for r in db.songs():
        if not r["duration"] or not Path(r["file_path"]).exists():
            continue
        mood = json.loads(r["mood"]) if r["mood"] else None
        out.append(SongInfo(r["id"], r["file_path"], float(r["duration"]), r["pace"], mood, bool(r["favourite"]),
                            bool(r["partial"]) and not bool(r["checked"]), cats.get(r["id"], set()), usage.get(r["id"], 0)))
    return out


def rule_category(video: str) -> int | None:
    rules = get_db().folder_rules()
    if not rules:
        return None
    p = Path(video).parent
    best = None
    for folder, cid in rules.items():
        try:
            fp = Path(folder)
            if p == fp or fp in p.parents:
                if best is None or len(str(fp)) > len(best[0]):
                    best = (str(fp), cid)
        except Exception:
            pass
    return best[1] if best else None


def score_song(item: AutoItem, song: SongInfo, mode: str, batch_uses: int, rule_cid: int | None) -> tuple[float, dict] | None:
    s = get_settings()
    w = s.get("automusic.weights") or {}
    V, L = max(item.duration, 0.1), song.duration
    if L >= V:
        s_len = 1.0 - 0.3 * min(1.0, (L - V) / max(30.0, V))
    else:
        ratio = L / V
        if mode == "long_only":
            return None
        s_len = {"loop": 0.35 + 0.4 * ratio, "chain": 0.4 + 0.4 * ratio, "trim_fade": 0.6 * ratio ** 2}.get(mode, 0.5 * ratio)
    vp = item.features.get("pace")
    s_pace = 0.5 if vp is None or song.pace is None else 1.0 - abs(float(vp) - float(song.pace))
    ms = labels.mood_similarity(item.features.get("mood"), song.mood)
    s_mood = 0.5 if ms is None else ms
    s_pref = 0.5 + (0.3 if song.favourite else 0.0) - (0.15 if song.partial_unchecked else 0.0)
    if rule_cid is not None:
        s_pref += 0.4 if rule_cid in song.cats else -0.2
    s_pref = float(np.clip(s_pref, 0, 1))
    total = (w.get("length", 0.4) * s_len + w.get("pace", 0.25) * s_pace + w.get("mood", 0.25) * s_mood
             + w.get("prefs", 0.1) * s_pref)
    total -= 0.12 * batch_uses + 0.02 * min(song.used, 5)
    return total, {"length": s_len, "pace": s_pace, "mood": s_mood, "prefs": s_pref}


def rank_songs(item: AutoItem, songs: list[SongInfo], batch_uses: dict[int, int]) -> list[tuple[float, SongInfo]]:
    mode = eff(item, "mode")
    rule = rule_category(item.video)
    ranked = []
    for sg in songs:
        r = score_song(item, sg, mode, batch_uses.get(sg.id, 0), rule)
        if r is not None:
            ranked.append((r[0], sg))
    ranked.sort(key=lambda x: -x[0])
    return ranked


def pick_chain(item: AutoItem, primary: SongInfo, songs: list[SongInfo], batch_uses: dict[int, int]) -> list[int]:
    """Further songs (most similar to the first) to cover a video the first song is too short for."""
    cf = float(get_settings().get("automusic.crossfade_s", 3.0))
    need = item.duration - (primary.duration - item.offset)
    chain: list[int] = []
    pool = [s for s in songs if s.id != primary.id]

    def sim(a: SongInfo) -> float:
        ms = labels.mood_similarity(primary.mood, a.mood)
        pace = 0.5 if a.pace is None or primary.pace is None else 1 - abs(a.pace - primary.pace)
        return (0.5 if ms is None else ms) * 0.6 + pace * 0.4 - 0.1 * batch_uses.get(a.id, 0)

    pool.sort(key=lambda a: -sim(a))
    for cand in pool:
        if need <= 0:
            break
        chain.append(cand.id)
        need -= cand.duration - cf
    return chain


_song_cache: dict[str, tuple[np.ndarray, np.ndarray]] = {}
_cache_lock = threading.Lock()


def song_audio(path: str) -> tuple[np.ndarray, np.ndarray]:
    with _cache_lock:
        if path in _song_cache:
            return _song_cache[path]
    y = ff.decode_audio(path, af.SR)
    _bpm, beats = af.tempo_and_beats(y)
    with _cache_lock:
        if len(_song_cache) > 64:
            _song_cache.clear()
        _song_cache[path] = (y, beats)
    return y, beats


def choose_offset(item: AutoItem, song: SongInfo) -> float:
    if not get_settings().get("automusic.smart_start", True):
        return 0.0
    try:
        y, beats = song_audio(song.path)
        return af.smart_start(y, af.SR, beats, min(item.duration, song.duration), song.duration)
    except Exception as e:
        log.info("smart start failed for %s: %s", song.path, e)
        return 0.0


def assign(item: AutoItem, song: SongInfo, songs: list[SongInfo], batch_uses: dict[int, int], score: float | None) -> None:
    item.song_id = song.id
    if score is not None:
        item.score = round(float(score), 3)
    item.offset = choose_offset(item, song)
    item.chain_ids = []
    if eff(item, "mode") == "chain" and song.duration - item.offset < item.duration:
        item.chain_ids = pick_chain(item, song, songs, batch_uses)
    item.status = "matched"


def match_items(items: list[AutoItem], ctx: JobContext, only_unmatched: bool = False) -> None:
    songs = load_songs()
    if not songs:
        raise RuntimeError("The song library is empty. Find music first.")
    batch_uses: dict[int, int] = {}
    for it in items:
        if it.song_id is not None and (it.manual or only_unmatched):
            batch_uses[it.song_id] = batch_uses.get(it.song_id, 0) + 1
    with_sound = bool(get_settings().get("automusic.include_with_sound", False))
    todo = [it for it in items if not it.manual and not (only_unmatched and it.song_id is not None)
            and it.status != "exported" and (with_sound or not it.has_sound)]
    for i, it in enumerate(todo):
        ctx.check()
        ctx.progress(i / max(1, len(todo)), f"Matching {Path(it.video).name}")
        ranked = rank_songs(it, songs, batch_uses)
        if not ranked:
            it.song_id = None
            it.status = "no_match"
            it.message = "No song is long enough (Settings > Auto-music > songs shorter than the video)"
            continue
        best_score, best = ranked[0]
        it.alts = [[sg.id, round(float(sc), 3)] for sc, sg in ranked[:6]]
        assign(it, best, songs, batch_uses, best_score)
        batch_uses[best.id] = batch_uses.get(best.id, 0) + 1
        ctx.event("item_changed", it)
    ctx.progress(1.0, "Matched")


def manual_pick(item: AutoItem, song_id: int) -> None:
    songs = load_songs()
    song = next((s for s in songs if s.id == song_id), None)
    if song is None:
        raise RuntimeError("That song's file is missing.")
    sc = score_song(item, song, eff(item, "mode") if eff(item, "mode") != "long_only" else "trim_fade", 0,
                    rule_category(item.video))
    assign(item, song, songs, {}, sc[0] if sc else None)
    item.manual = True


# ---------------------------------------------------------------------------
# Building the music track
@dataclass
class Piece:
    path: str
    start: float
    dur: float


def plan_pieces(item: AutoItem) -> tuple[list[Piece], float]:
    """Pieces of song(s) to play back to back, and the crossfade between them."""
    db = get_db()
    s = get_settings()
    cf = float(s.get("automusic.crossfade_s", 3.0))
    row = db.song(item.song_id) if item.song_id is not None else None
    if row is None:
        return [], cf
    V = item.duration
    L = float(row["duration"])
    off = min(max(0.0, item.offset), max(0.0, L - 1.0))
    avail = L - off
    if avail >= V:
        return [Piece(row["file_path"], off, V)], cf
    mode = eff(item, "mode")
    if mode in ("trim_fade", "long_only"):
        return [Piece(row["file_path"], off, avail)], cf
    pieces = [Piece(row["file_path"], off, avail)]
    covered = avail
    queue: list[tuple[str, float]] = []
    if mode == "chain":
        for cid in item.chain_ids:
            r = db.song(cid)
            if r is not None and Path(r["file_path"]).exists():
                queue.append((r["file_path"], float(r["duration"])))
    guard = 0
    while covered < V - 0.05 and guard < 200:
        guard += 1
        if queue:
            path, dur = queue.pop(0)
        else:
            path, dur = row["file_path"], L        # loop the first song
        if dur <= cf + 0.5:
            continue
        pieces.append(Piece(path, 0.0, dur))
        covered += dur - cf
    # Trim the last piece so the total equals the video length.
    excess = covered - V
    if excess > 0 and len(pieces) > 1:
        pieces[-1].dur = max(cf + 0.5, pieces[-1].dur - excess)
    return pieces, cf


def track_length(pieces: list[Piece], cf: float) -> float:
    if not pieces:
        return 0.0
    return sum(p.dur for p in pieces) - cf * (len(pieces) - 1)


def can_copy_audio(item: AutoItem, pieces: list[Piece]) -> bool:
    return (len(pieces) == 1 and eff(item, "fade_in") <= 0 and eff(item, "fade_out") <= 0
            and abs(eff(item, "volume") - 1.0) < 1e-6)


def audio_graph(item: AutoItem, pieces: list[Piece], cf: float, first_input: int) -> tuple[list[str], str]:
    """ffmpeg input args and filter_complex producing [aout]."""
    args: list[str] = []
    filters: list[str] = []
    for i, p in enumerate(pieces):
        args += ["-ss", f"{p.start:.3f}", "-t", f"{p.dur:.3f}", "-i", p.path]
        filters.append(f"[{first_input + i}:a:0]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                       f"asetpts=PTS-STARTPTS[p{i}]")
    cur = "p0"
    for i in range(1, len(pieces)):
        d = min(cf, pieces[i - 1].dur - 0.1, pieces[i].dur - 0.1)
        filters.append(f"[{cur}][p{i}]acrossfade=d={max(0.1, d):.3f}:c1=tri:c2=tri[x{i}]")
        cur = f"x{i}"
    total = track_length(pieces, cf)
    fi, fo, vol = eff(item, "fade_in"), eff(item, "fade_out"), eff(item, "volume")
    tail = []
    if fi > 0:
        tail.append(f"afade=t=in:st=0:d={min(fi, total / 2):.3f}")
    if fo > 0:
        d = min(fo, total / 2)
        tail.append(f"afade=t=out:st={max(0.0, total - d):.3f}:d={d:.3f}")
    if abs(vol - 1.0) > 1e-6:
        tail.append(f"volume={vol:.3f}")
    tail.append(f"atrim=end={min(total, item.duration):.3f}")
    filters.append(f"[{cur}]{','.join(tail)}[aout]")
    return args, ";".join(filters)


def preview_audio(item: AutoItem) -> Path | None:
    """Render the exact music track as a WAV for previewing (cached by its settings)."""
    pieces, cf = plan_pieces(item)
    if not pieces:
        return None
    key = json.dumps([[p.path, p.start, p.dur] for p in pieces] + [cf, eff(item, "fade_in"), eff(item, "fade_out"),
                      eff(item, "volume"), item.duration])
    h = hashlib.sha1(key.encode()).hexdigest()[:16]
    out = get_settings().cache("preview") / f"{h}.wav"
    if out.exists():
        return out
    in_args, graph = audio_graph(item, pieces, cf, 0)
    tmp = out.with_suffix(".part.wav")
    p = ff.run([ff.ffmpeg(), "-v", "error", "-nostdin", "-y"] + in_args +
               ["-filter_complex", graph, "-map", "[aout]", "-c:a", "pcm_s16le", "-f", "wav", str(tmp)], timeout=600)
    if p.returncode != 0:
        raise ff.FFError(p.stderr.decode("utf-8", "replace")[-500:])
    tmp.replace(out)
    return out


# ---------------------------------------------------------------------------
# Export
COPY_OK = {
    ".mp4": {"aac", "mp3", "opus", "alac", "ac3", "eac3", "flac"},
    ".m4v": {"aac", "mp3", "ac3", "alac"},
    ".mov": {"aac", "mp3", "alac", "ac3", "pcm_s16le", "pcm_s24le"},
    ".mkv": None,  # anything
    ".webm": {"opus", "vorbis"},
    ".flv": {"aac", "mp3"},
    ".avi": {"mp3", "ac3", "pcm_s16le", "mp2"},
    ".wmv": {"wmav2", "mp3"}, ".asf": {"wmav2", "mp3"},
    ".ts": {"aac", "mp3", "ac3", "mp2", "eac3"}, ".m2ts": {"aac", "ac3", "eac3", "mp2"}, ".mts": {"aac", "ac3", "mp2"},
    ".mpg": {"mp2", "ac3", "mp3"}, ".mpeg": {"mp2", "ac3", "mp3"},
    ".3gp": {"aac"}, ".ogv": {"vorbis", "opus"},
}


def encode_args(ext: str) -> list[str]:
    s = get_settings()
    aac = ["-c:a", "aac", "-b:a", s.get("export.aac_bitrate", "320k")]
    return {
        ".webm": ["-c:a", "libopus", "-b:a", s.get("export.opus_bitrate", "256k")],
        ".ogv": ["-c:a", "libvorbis", "-q:a", "8"],
        ".avi": ["-c:a", "libmp3lame", "-b:a", s.get("export.mp3_bitrate", "320k")],
        ".wmv": ["-c:a", "wmav2", "-b:a", "256k"], ".asf": ["-c:a", "wmav2", "-b:a", "256k"],
        ".mpg": ["-c:a", "mp2", "-b:a", "384k"], ".mpeg": ["-c:a", "mp2", "-b:a", "384k"],
    }.get(ext, aac)


def output_path(video: str) -> Path:
    p = Path(video)
    suffix = get_settings().get("export.suffix", "_edited") or "_edited"
    return unique_path(p.with_name(f"{p.stem}{suffix}{p.suffix}"))


def export_item(item: AutoItem, ctx: JobContext) -> Path:
    pieces, cf = plan_pieces(item)
    if not pieces:
        raise RuntimeError("No song picked")
    ext = Path(item.video).suffix.lower()
    out = output_path(item.video)
    tmp = out.with_name(out.stem + ".nirvash-part" + out.suffix)
    args = [ff.ffmpeg(), "-y", "-i", item.video]
    song_codec = ""
    if can_copy_audio(item, pieces):
        try:
            song_codec = ff.probe(pieces[0].path).audio.codec
        except Exception:
            song_codec = ""
    allowed = COPY_OK.get(ext, {"aac"})
    copy_audio = bool(song_codec) and (allowed is None or song_codec in allowed)
    if copy_audio:
        p = pieces[0]
        args += ["-ss", f"{p.start:.3f}", "-t", f"{p.dur:.3f}", "-i", p.path,
                 "-map", "0:v", "-map", "1:a:0", "-c:a", "copy"]
    else:
        in_args, graph = audio_graph(item, pieces, cf, 1)
        args += in_args + ["-filter_complex", graph, "-map", "0:v", "-map", "[aout]"] + encode_args(ext)
    args += ["-map", "0:s?", "-c:v", "copy", "-c:s", "copy", "-map_metadata", "0", "-map_chapters", "0"]
    if ext in (".mp4", ".m4v", ".mov", ".3gp"):
        args += ["-movflags", "+faststart"]
    fmt = {".mkv": "matroska", ".webm": "webm", ".mp4": "mp4", ".m4v": "mp4", ".mov": "mov", ".flv": "flv",
           ".avi": "avi", ".wmv": "asf", ".asf": "asf", ".ts": "mpegts", ".m2ts": "mpegts", ".mts": "mpegts",
           ".mpg": "mpeg", ".mpeg": "mpeg", ".3gp": "3gp", ".ogv": "ogg"}.get(ext)
    if fmt:
        args += ["-f", fmt]
    args.append(str(tmp))
    try:
        ff.run_with_progress(args, item.duration, lambda f: ctx.progress(f, Path(item.video).name), ctx.cancel)
    except ff.FFError as e:
        if tmp.exists():
            tmp.unlink()
        # Subtitles that the container can't take are the usual cause; try again without them.
        if "-map" in args and "0:s?" in args:
            k = args.index("0:s?")
            args2 = args[:k - 1] + args[k + 1:]
            if "-c:s" in args2:
                k = args2.index("-c:s")
                del args2[k:k + 2]
            ff.run_with_progress(args2, item.duration, lambda f: ctx.progress(f, Path(item.video).name), ctx.cancel)
        else:
            raise e
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        raise
    check = verify_output(item, tmp)
    if check:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(check)
    tmp.replace(out)
    item.output = str(out)
    item.status = "exported"
    item.message = "Video copied untouched" + (", audio copied" if copy_audio else "")
    db = get_db()
    for sid in [item.song_id] + list(item.chain_ids if eff(item, "mode") == "chain" else []):
        if sid is not None:
            db.record_usage(sid, item.video)
    return out


def verify_output(item: AutoItem, out: Path) -> str:
    try:
        pr = ff.probe(out)
    except Exception as e:
        return f"Export check failed: {e}"
    if pr.video is None or pr.audio is None:
        return "Export check failed: the new file is missing its video or audio"
    if (pr.width, pr.height) != (item.width, item.height) or (item.vcodec and pr.video.codec != item.vcodec):
        return "Export check failed: the video stream changed"
    if item.duration and abs(pr.duration - item.duration) > max(1.0, item.duration * 0.02):
        return f"Export check failed: length {pr.duration:.1f}s vs {item.duration:.1f}s"
    return ""


# ---------------------------------------------------------------------------
# Session (kept between runs)
def save_session(folder: str, items: list[AutoItem]) -> None:
    get_db().kv_set("automusic_session", {"folder": folder, "items": [i.to_json() for i in items]})


def load_session() -> tuple[str, list[AutoItem]]:
    d = get_db().kv_get("automusic_session") or {}
    return d.get("folder", ""), [AutoItem.from_json(x) for x in d.get("items", [])]
