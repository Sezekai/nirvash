"""Library file operations: renaming after the song, tags Explorer reads, removing video links, ignore lists."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_pipeline import FakeTagger, env, make_music_wav, sh  # noqa: E402,F401


def audio_hash(p: Path) -> str:
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(p), "-map", "0:a", "-c", "copy", "-f", "md5", "-"],
                         capture_output=True, check=True)
    return out.stdout.decode().strip()


def tags(p: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags:stream=codec_name,codec_type",
                          "-of", "json", str(p)], capture_output=True, check=True)
    j = json.loads(out.stdout)
    t = {k.lower(): v for k, v in (j.get("format", {}).get("tags") or {}).items()}
    t["_streams"] = [s["codec_type"] for s in j["streams"]]
    return t


def make_song(db, folder: Path, name: str, ext: str, seconds: float, **info) -> int:
    codec = {".m4a": ("aac", ["-f", "ipod"]), ".mp3": ("libmp3lame", []), ".opus": ("libopus", []),
             ".flac": ("flac", [])}[ext]
    p = folder / f"{name}{ext}"
    sh("ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=f=440:d={seconds}", "-c:a", codec[0], *codec[1],
       "-metadata", "title=from the video", str(p))
    sid = db.add_song(file_path=str(p), codec=codec[0].replace("lib", "").replace("mp3lame", "mp3"), ext=ext,
                      duration=seconds, bitrate=128000, id_status="found" if info.get("title") else "not_found",
                      **info)
    db.add_source(sid, str(folder / f"{name} video.mp4"), 0, seconds)
    return sid


def test_migration_adds_columns(tmp_path):
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE songs (id INTEGER PRIMARY KEY, file_path TEXT NOT NULL UNIQUE, duration REAL NOT NULL "
              "DEFAULT 0, title TEXT, added_at REAL NOT NULL)")
    c.execute("INSERT INTO songs(file_path, duration, added_at) VALUES('a.m4a', 3, 0)")
    c.commit()
    c.close()
    from nirvash.core.db import DB
    db = DB(p)
    r = db.song(1)
    assert r["played"] == 0 and r["listen_count"] == 0 and r["approved"] == 0 and r["unlinked"] == 0
    db.add_listen(1)
    db.add_listen(1)
    db.mark_played(1)
    r = db.song(1)
    assert r["played"] == 1 and r["listen_count"] == 2 and r["last_played"]


def test_names_tell_versions_apart():
    from nirvash.jobs.file_ops import song_names

    def row(i, title, artist="A", dur=200.0, bpm=120.0, br=128000, ext=".m4a"):
        return {"id": i, "title": title, "artist": artist, "duration": dur, "bpm": bpm, "bitrate": br, "ext": ext,
                "file_path": f"x{ext}"}

    names = song_names([row(1, "Song"), row(2, "Song", dur=245), row(3, "Other"), row(4, None, dur=95),
                        row(5, "Same", bpm=100), row(6, "same", bpm=128), row(7, "Twin"), row(8, "Twin")])
    assert names[1] == "A - Song (3m 20s)" and names[2] == "A - Song (4m 05s)"
    assert names[3] == "A - Other"
    assert names[4] == "Unidentified song 4 (1m 35s)"
    assert names[5] == "A - Same (100 BPM)" and names[6] == "A - same (128 BPM)"
    # Nothing tells these apart: same name, and the file step adds " (2)" instead of overwriting.
    assert names[7] == names[8] == "A - Twin"


def test_rename_tag_and_unlink(env):
    from nirvash.core.db import get_db
    from nirvash.core.settings import get_settings
    from nirvash.jobs import file_ops
    from nirvash.jobs.context import JobContext

    db = get_db()
    lib = get_settings().folder("songs")
    a = make_song(db, lib, "Some video [0.10-3.20]", ".m4a", 6, title="Héllo ☆", artist="Ärtist", album="Alb",
                  genre="Pop", year="2019")
    b = make_song(db, lib, "Another video", ".m4a", 9, title="Héllo ☆", artist="Ärtist")
    c = make_song(db, lib, "Third video", ".mp3", 5, title="Twin", artist="X")
    d = make_song(db, lib, "Fourth video", ".mp3", 5, title="Twin", artist="X")
    e = make_song(db, lib, "Talky video", ".opus", 4)
    # A file already sits at the name song c wants: it must never be overwritten.
    blocker = lib / "X - Twin.mp3"
    blocker.write_bytes(b"not a song")
    # Cover art for song a
    cover = get_settings().cache("covers") / f"{a}.jpg"
    sh("ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=200x200:d=1", "-frames:v", "1", str(cover))
    before = {i: audio_hash(Path(db.song(i)["file_path"])) for i in (a, b, c, d, e)}

    res = file_ops.rename_batch(JobContext(), [a, b, c, d, e])
    assert res["skipped"] == 1 and not res["failed"], res
    pa, pb, pc, pd = (Path(db.song(i)["file_path"]) for i in (a, b, c, d))
    assert pa.name == "Ärtist - Héllo ☆ (0m 06s).m4a", pa.name
    assert pb.name == "Ärtist - Héllo ☆ (0m 09s).m4a", pb.name
    assert {pc.name, pd.name} == {"X - Twin (2).mp3", "X - Twin (3).mp3"}, (pc.name, pd.name)
    assert blocker.read_bytes() == b"not a song"
    ta = tags(pa)
    assert ta["title"] == "Héllo ☆" and ta["artist"] == "Ärtist" and ta["album"] == "Alb" and ta["genre"] == "Pop"
    assert ta["date"] == "2019" and "video" in ta["_streams"], ta      # cover art attached
    tc = tags(pc)
    assert tc["title"] == "Twin" and tc["artist"] == "X"
    assert db.song(a)["tagged"] == 1
    # Audio untouched, no leftovers.
    for i in (a, b, c, d, e):
        assert audio_hash(Path(db.song(i)["file_path"])) == before[i], i
    assert not list(lib.glob(".nirvash-tag-*"))
    # Renaming again leaves names alone.
    file_ops.rename_batch(JobContext(), [a, c, d])
    assert Path(db.song(a)["file_path"]) == pa and {Path(db.song(i)["file_path"]).name for i in (c, d)} == {pc.name, pd.name}

    # Remove video links: sources gone, file named after the song, no trace of the video in tags.
    res = file_ops.unlink_batch(JobContext(), [a, e])
    assert not res["failed"], res
    for i in (a, e):
        r = db.song(i)
        assert r["unlinked"] == 1 and not db.sources(i)
    pe = Path(db.song(e)["file_path"])
    assert pe.name == f"Unidentified song {e} (0m 04s).opus", pe.name
    te = tags(pe)
    assert "title" not in te, te
    assert audio_hash(pe) == before[e]
    assert Path(db.song(a)["file_path"]) == pa


def test_scan_skips_deleted_and_never_relinks(env, monkeypatch):
    from nirvash.core.db import get_db
    from nirvash.jobs import file_ops, scan
    from nirvash.jobs.context import JobContext

    src = env / "src"
    src.mkdir()
    m1, m2 = env / "m1.wav", env / "m2.wav"
    make_music_wav(m1, 25, seed=1)
    make_music_wav(m2, 25, seed=7)
    sh("ffmpeg", "-y", "-i", str(m2), "-af", "asetrate=44100*1.5,aresample=44100", str(env / "m2b.wav"))
    for name, wav in (("V1.mp4", m1), ("V2.mp4", env / "m2b.wav"), ("V3.mp4", m1), ("V4.mp4", env / "m2b.wav")):
        sh("ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10", "-i", str(wav), "-t", "20",
           "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(src / name))
    tagger = FakeTagger()
    monkeypatch.setattr(scan, "get_tagger", lambda: tagger)
    orig = scan.classify

    def classify(y, sr, duration, silence_db, tg):
        tg.starts = scan.window_starts(duration)
        tg.timeline = lambda t: "music"
        return orig(y, sr, duration, silence_db, tg)

    monkeypatch.setattr(scan, "classify", classify)
    db = get_db()
    ctx = JobContext()
    for v in ("V1.mp4", "V2.mp4"):
        assert scan.scan_video(src / v, ctx, db.skip_lists()).status == "music"
    s1 = next(r for r in db.songs() if "V1" in r["file_path"])
    s2 = next(r for r in db.songs() if "V2" in r["file_path"])

    # A video whose song is in the library is never processed again, even with "re-check" on.
    from nirvash.core.settings import get_settings
    get_settings().set("scan.rescan", True)
    assert scan.scan_video(src / "V1.mp4", ctx, db.skip_lists()).status == "in_library"
    get_settings().set("scan.rescan", False)

    # Song 1: unlink. Its duplicate in V3 must not link it again.
    file_ops.unlink_videos(s1["id"])
    r3 = scan.scan_video(src / "V3.mp4", ctx, db.skip_lists())
    assert r3.merged == [s1["id"]] and not db.sources(s1["id"]), (r3, list(map(dict, db.sources(s1["id"]))))

    # Song 2: delete, skipping its video and the song itself.
    db.ignore_video(str(src / "V2.mp4"))
    db.ignore_song(s2["fingerprint"], s2["duration"])
    scan.forget_fingerprint(s2["id"])
    Path(s2["file_path"]).unlink()
    db.delete_song(s2["id"])
    scan.reset_ignored_index()
    get_db().x("DELETE FROM scanned_videos")
    assert scan.scan_video(src / "V2.mp4", ctx, db.skip_lists()).status == "ignored"
    # ...also after the folder is moved (matched by file name and size).
    import shutil
    moved = env / "moved"
    moved.mkdir()
    shutil.copy2(src / "V2.mp4", moved / "V2.mp4")
    assert scan.scan_video(moved / "V2.mp4", ctx, db.skip_lists()).status == "ignored"
    r4 = scan.scan_video(src / "V4.mp4", ctx, db.skip_lists())
    assert r4.dropped == 1 and not r4.added, r4
    assert len(db.songs()) == 1


def test_identify_methods(env, monkeypatch):
    """One-off service choices, trying harder with speed changes, AudD as a backup."""
    import numpy as np

    from nirvash.core.db import get_db
    from nirvash.core.settings import get_settings
    from nirvash.jobs import identify
    from nirvash.lookup import services

    db = get_db()
    lib = get_settings().folder("songs")
    p = lib / "clip.wav"
    sh("ffmpeg", "-y", "-f", "lavfi", "-i", "sine=f=330:d=40", str(p))
    sid = db.add_song(file_path=str(p), duration=40, id_status="pending")
    calls = []

    def fake_shazam(y, sr, cancel=None):
        calls.append(("shazam", round(len(y) / sr, 1)))
        # Only the version slowed back down to 0.8x speed (a 1.25x sped-up edit) matches.
        return {"title": "Song", "artist": "A"} if abs(len(y) / sr - 12.0) < 0.2 and len(calls) > 5 else None

    monkeypatch.setattr(services, "shazam", fake_shazam)
    monkeypatch.setattr(services, "itunes", lambda *a: None)
    monkeypatch.setattr(services, "musicbrainz", lambda *a: None)
    monkeypatch.setattr(services, "lrclib", lambda *a: None)
    monkeypatch.setattr(services, "audd", lambda *a: calls.append(("audd",)) or None)
    monkeypatch.setattr(services, "acoustid", lambda *a: calls.append(("acoustid",)) or None)
    s = get_settings()
    s.set("identify.acoustid_key", "k")
    s.set("identify.audd", True)
    s.set("identify.audd_token", "t")

    # AcoustID only: Shazam and AudD aren't touched, and the one-off choice is cleared afterwards.
    db.update_song(sid, id_methods="acoustid")
    identify.identify_song(sid)
    assert [c[0] for c in calls] == ["acoustid"] and db.song(sid)["id_methods"] is None
    assert db.song(sid)["id_status"] == "not_found"

    # Usual order: Shazam (3 parts), AcoustID, then AudD (2 parts).
    calls.clear()
    identify.identify_song(sid)
    assert [c[0] for c in calls] == ["shazam"] * 3 + ["acoustid", "audd", "audd"], calls

    # Trying harder: 5 parts, then speed changes, until one matches.
    calls.clear()
    db.update_song(sid, id_methods="deep")
    up = identify.identify_song(sid)
    r = db.song(sid)
    assert r["id_status"] == "found" and r["title"] == "Song", calls
    assert "speed" in r["id_source"] and r["id_methods"] is None
    assert len(calls) == 6, calls

    # respeed keeps 12 s of output for any speed
    y = np.zeros(16000 * 40, np.float32)
    for sp in identify.SPEEDS:
        assert abs(len(identify.respeed(y, 16000, 5, sp)) / 16000 - 12) < 0.01
