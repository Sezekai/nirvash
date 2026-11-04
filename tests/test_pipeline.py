"""End-to-end test of scanning and auto-music on synthetic media, with stand-ins for the AI models."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def sh(*args):
    subprocess.run(args, check=True, capture_output=True)


def make_music_wav(path: Path, seconds: float, seed: int = 1):
    """Chord stabs on a 120 BPM beat - enough structure for fingerprints and tempo."""
    sr = 44100
    t = np.arange(int(seconds * sr)) / sr
    rng = np.random.default_rng(seed)
    y = np.zeros_like(t)
    chords = [[220, 277, 330], [196, 247, 294], [175, 220, 262], [165, 208, 247]]
    beat = 0.5
    for i in range(int(seconds / beat)):
        c = chords[(i // 4) % 4]
        s0 = int(i * beat * sr)
        seg = t[s0:s0 + int(beat * sr)] - t[s0]
        env = np.exp(-seg * 6)
        tone = sum(np.sin(2 * np.pi * f * seg + rng.uniform(0, 1)) for f in c) / 3
        y[s0:s0 + len(seg)] += 0.5 * env * tone
    import wave
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(y, -1, 1) * 32767).astype("<i2").tobytes())


class FakeTagger:
    """Stands in for the AudioSet model: labels windows from a timeline."""
    provider = "fake"
    timeline = None
    starts: list = []

    def probs(self, chunks):
        return np.zeros((len(chunks), 527), np.float32)

    def classes(self, p):
        lab = [self.timeline(s + 5) for s in self.starts]
        return {"music": np.array([1.0 if l == "music" else 0.0 for l in lab]),
                "speech": np.array([1.0 if l == "speech" else 0.0 for l in lab]),
                "singing": np.zeros(len(lab))}

    def mood(self, p):
        return {"calm": 0.6, "happy": 0.4}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NIRVASH_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("NIRVASH_ROOT", str(tmp_path / "root"))
    import importlib
    import nirvash.core.db as dbm
    import nirvash.core.settings as sm
    importlib.reload(sm)
    importlib.reload(dbm)
    sm._settings = None
    dbm._db = None
    from nirvash.jobs import scan, automusic
    importlib.reload(scan)
    importlib.reload(automusic)
    scan.reset_fp_index()
    return tmp_path


def test_scan_and_automusic(env, monkeypatch):
    from nirvash.jobs import scan, automusic
    from nirvash.jobs.context import JobContext
    from nirvash.core.db import get_db
    from nirvash.media import ff

    src = env / "src"
    src.mkdir()
    music = env / "music.wav"
    make_music_wav(music, 40)
    # A: 20 s of music, B: same song 0-25 s then talking-like noise.
    sh("ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25", "-i", str(music), "-t", "20",
       "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-b:a", "192k", "-shortest", str(src / "A music.mp4"))
    sh("ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25", "-i", str(music),
       "-f", "lavfi", "-i", "anoisesrc=d=8:a=0.2", "-filter_complex", "[1:a]atrim=0:32[m];[2:a]atrim=0:8[n];[m][n]concat=n=2:v=0:a=1[a]",
       "-map", "0:v", "-map", "[a]", "-t", "40", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
       str(src / "B partial.mp4"))
    # Silent videos for auto-music
    silent = env / "silent"
    silent.mkdir()
    sh("ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-t", "15", "-c:v", "libx264",
       "-preset", "ultrafast", "-g", "60", str(silent / "C no audio.mp4"))
    sh("ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=480x480:rate=24", "-f", "lavfi", "-i",
       "anullsrc=r=48000:cl=stereo", "-t", "50", "-c:v", "libvpx-vp9", "-deadline", "realtime", "-b:v", "300k",
       "-c:a", "libopus", str(silent / "D silent track.webm"))

    # Stand-in tagger: music where the timeline says so.
    tagger = FakeTagger()
    monkeypatch.setattr(scan, "get_tagger", lambda: tagger)
    timeline = {"A music.mp4": lambda t: "music", "B partial.mp4": lambda t: "music" if t < 32 else "speech"}
    current = {}
    orig_classify = scan.classify

    def classify(y, sr, duration, silence_db, tg):
        tg.starts = scan.window_starts(duration)
        tg.timeline = timeline[current["name"]]
        return orig_classify(y, sr, duration, silence_db, tg)

    monkeypatch.setattr(scan, "classify", classify)

    ctx = JobContext()
    results = []
    for v in sorted(src.iterdir()):
        current["name"] = v.name
        results.append(scan.scan_video(v, ctx))
    by = {Path(r.path).name: r for r in results}
    assert by["A music.mp4"].status == "music", by["A music.mp4"]
    assert by["B partial.mp4"].status == "partial", by["B partial.mp4"]
    db = get_db()
    songs = db.songs()
    # B's longer copy of the same song replaces A's.
    assert len(songs) == 1, [dict(s) for s in songs]
    s = songs[0]
    assert s["codec"] == "aac" and s["file_path"].endswith(".m4a")
    assert s["duration"] > 20, s["duration"]
    assert len(db.sources(s["id"])) == 2
    assert s["bpm"] and 100 < s["bpm"] < 140, s["bpm"]

    # Rescan skips
    current["name"] = "A music.mp4"
    assert scan.scan_video(src / "A music.mp4", ctx).status == "skipped"

    # Auto-music
    monkeypatch.setattr(automusic, "get_clip", lambda: (_ for _ in ()).throw(automusic.ModelMissing("no clip")))
    items = [automusic.AutoItem(str(p)) for p in sorted(silent.iterdir())]
    for it in items:
        automusic.analyse_video(it, ctx)
        assert not it.has_sound, it.video
        assert "pace" in it.features
    automusic.match_items(items, ctx)
    for it in items:
        assert it.song_id == s["id"], it
    # D (50 s) is longer than the song: default "chain" mode, with one song it loops.
    pieces, cf = automusic.plan_pieces(items[1])
    assert len(pieces) >= 2 and abs(automusic.track_length(pieces, cf) - items[1].duration) < 0.6
    for it in items:
        wav = automusic.preview_audio(it)
        assert wav and wav.exists()
        out = automusic.export_item(it, ctx)
        assert out.exists() and out.name.endswith("_edited" + Path(it.video).suffix)
        pr = ff.probe(out)
        assert pr.audio is not None
        # Video stream must be bit-identical.
        def vmd5(p):
            return subprocess.run(["ffmpeg", "-v", "error", "-i", str(p), "-map", "0:v:0", "-c", "copy", "-f", "md5", "-"],
                                  capture_output=True, text=True).stdout.strip()
        assert vmd5(out) == vmd5(it.video), it.video
        assert abs(pr.duration - it.duration) < 1.0
    assert ff.probe(items[1].output).audio.codec == "opus"
