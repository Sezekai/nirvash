"""Trimming, volume, re-cutting from the video and evening out loudness (generated audio only)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_pipeline import env, make_music_wav, sh  # noqa: E402,F401


def _song(env, name: str, seconds: float, volume_db: float, title: str = "Tagged title"):
    """A music-like m4a at a set level, tagged, added to the library."""
    from nirvash.core.db import get_db
    from nirvash.core.settings import get_settings
    wav = env / f"{name}.wav"
    make_music_wav(wav, seconds)
    out = get_settings().folder("songs") / f"{name}.m4a"
    sh("ffmpeg", "-v", "error", "-y", "-i", str(wav), "-af", f"volume={volume_db}dB", "-c:a", "aac", "-b:a", "192k",
       "-metadata", f"title={title}", "-f", "ipod", str(out))
    from nirvash.jobs.scan import describe_file
    info = describe_file(out)
    return get_db().add_song(file_path=str(out), title=title, **info), out


def _tags(path: Path) -> dict:
    from nirvash.media import ff
    return {k.lower(): v for k, v in ff.probe(path).raw.get("format", {}).get("tags", {}).items()}


def test_trim_volume_reset(env):
    from nirvash.core.db import get_db
    from nirvash.jobs import audio_edit as ae
    sid, f = _song(env, "song", 20, -6)
    before = ae.measure(f)
    # trimming only: the audio is copied, the original kept, the tags stay
    res = ae.render(None, sid, trim_start=2.0, trim_end=12.0)
    assert abs(res["duration"] - 10) < 0.2 and res["how"].startswith("audio copied")
    orig = ae.original_path(sid)
    assert orig is not None and orig.exists() and abs(get_db().song(sid)["duration"] - 10) < 0.2
    assert _tags(f).get("title") == "Tagged title"
    # louder by 6 dB, built again from the original (still trimmed)
    res = ae.render(None, sid, trim_start=2.0, trim_end=12.0, gain_db=6.0)
    assert res["how"].startswith("re-encoded")
    after = ae.measure(f)
    assert 4.5 < after.lufs - before.lufs < 7.5, (before, after)
    assert abs(get_db().song(sid)["duration"] - 10) < 0.3 and get_db().song(sid)["gain_db"] == 6.0
    assert _tags(f).get("title") == "Tagged title"
    # back to the original
    ae.reset(None, sid)
    r = get_db().song(sid)
    assert abs(r["duration"] - 20) < 0.2 and r["trim_start"] is None and r["trim_end"] is None and r["gain_db"] == 0
    assert abs(ae.measure(f).lufs - before.lufs) < 0.5
    with pytest.raises(ae.EditError):
        ae.render(None, sid, trim_start=5.0, trim_end=5.5)      # less than a second


def test_level_songs(env):
    from nirvash.core.db import get_db
    from nirvash.jobs import audio_edit as ae
    from nirvash.jobs.context import JobContext
    quiet, qf = _song(env, "quiet", 12, -24)
    loud, lf = _song(env, "loud", 12, 0)
    from nirvash.core.settings import get_settings
    # a third song already at the target is only measured
    mid, mf = _song(env, "mid", 12, -12)
    get_settings().set("level.target", ae.measure(mf).lufs)
    m0 = mf.stat().st_mtime
    res = ae.level_songs(JobContext())
    assert res["measured"] == 3 and res["changed"] == 2, res
    assert mf.stat().st_mtime == m0 and get_db().song(mid)["orig_name"] is None    # untouched, nothing kept
    q = get_db().song(quiet)
    assert q["gain_db"] > 2 and q["level_done"] == 1 and ae.measure(qf).lufs > ae.measure(ae.original_path(quiet)).lufs + 2
    # hand-set volumes are left alone afterwards
    ae.render(None, quiet, gain_db=1.0)
    assert ae.level_songs(JobContext())["changed"] == 0
    assert get_db().song(quiet)["gain_db"] == 1.0
    assert get_db().song(loud)["gain_db"] < -6


def test_level_gain_keeps_headroom():
    from nirvash.jobs.audio_edit import Loudness, level_gain
    assert level_gain(Loudness(-20, -10), target=-14, max_gain=12) == 6.0
    assert level_gain(Loudness(-20, -3), target=-14, max_gain=12) == 2.0     # peaks would clip: only +2
    assert level_gain(Loudness(-40, -30), target=-14, max_gain=12) == 12.0
    assert level_gain(Loudness(-8, -0.1), target=-14, max_gain=12) == -6.0
    assert level_gain(None) == 0.0


def test_recut_from_video(env):
    from nirvash.core.db import get_db
    from nirvash.jobs import audio_edit as ae
    wav = env / "v.wav"
    make_music_wav(wav, 30)
    video = env / "clip.mp4"
    sh("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=160x90:r=10:d=30", "-i", str(wav),
       "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(video))
    sid, f = _song(env, "cut", 10, 0)
    get_db().add_source(sid, str(video), 10.0, 20.0)
    ae.render(None, sid, trim_start=1, trim_end=9, gain_db=0)
    first_orig = ae.original_path(sid)
    res = ae.recut(None, sid, str(video), 4.0, 26.0)
    assert abs(res["duration"] - 22) < 0.3
    r = get_db().song(sid)
    assert r["trim_start"] is None and r["trim_end"] is None and abs(r["duration"] - 22) < 0.3
    src = get_db().sources(sid)[0]
    assert (src["start"], src["end"]) == (4.0, 26.0)
    assert ae.original_path(sid) != first_orig and not first_orig.exists()     # old cut went to the Recycle Bin
    assert _tags(Path(r["file_path"])).get("title") == "Tagged title"
    assert r["loudness"] is not None
    with pytest.raises(ae.EditError):
        ae.recut(None, sid, str(env / "gone.mp4"), 0, 5)


def test_originals_place_can_move(env):
    from nirvash.jobs import audio_edit as ae, storage
    from nirvash.jobs.context import JobContext
    sid, _f = _song(env, "m", 8, 0)
    ae.render(None, sid, trim_start=1.0)
    dest = env / "elsewhere"
    dest.mkdir()
    storage.move_place(JobContext(), "originals", str(dest))
    assert ae.original_path(sid) is not None and str(ae.original_path(sid)).startswith(str(dest))
    _ = os


def test_editor_dialog_and_library(env, monkeypatch):
    """The editor opens on a real song, shows the original with its handles, and saving runs the job."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ["NIRVASH_NO_PLAYBACK"] = "1"
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from nirvash.core.db import get_db
    from nirvash.jobs import audio_edit as ae
    from nirvash.ui import theme
    theme.apply(app)
    from nirvash.ui.audio_editor import AudioEditDialog, parse_time
    from nirvash.ui.jobqueue import get_queue
    from nirvash.ui.library_page import LibraryPage
    from tests.test_ui_flows import wait_for
    assert parse_time("1:02.5") == 62.5 and parse_time("7,25") == 7.25 and parse_time("x") is None
    sid, f = _song(env, "ui", 16, -6)
    wav = env / "v.wav"
    make_music_wav(wav, 24)
    video = env / "clip.mp4"
    sh("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=160x90:r=10:d=24", "-i", str(wav),
       "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(video))
    get_db().add_source(sid, str(video), 4.0, 20.0)
    page = LibraryPage()
    page.resize(1200, 700)
    page.show()
    d = AudioEditDialog(page, sid)
    assert abs(d.wave.duration - 16) < 0.3 and (d.wave.a, round(d.wave.b)) == (0.0, 16)
    d.wave.set_range(3.0, 11.0, emit=True)
    d.gain.setValue(4)                         # +2 dB
    assert wait_for(app, lambda: d.loud is not None, 20)
    assert "LUFS" in d.loud_lbl.text()
    d._save()
    assert d.request.mode == "song" and d.request.trim_start == 3.0 and d.request.trim_end == 11.0
    assert d.request.gain_db == 2.0
    d.done(0)
    # video mode starts at where the song is in the video, with the old cut shown
    d = AudioEditDialog(page, sid, "video")
    assert d.mode.value == "video" and d.wave.old == (4.0, 20.0) and abs(d.wave.duration - 24) < 0.3
    d.done(0)
    # through the library: E opens it, the auto answer saves what's set (the whole song, louder)
    monkeypatch.setenv("NIRVASH_AUTO_DIALOGS", "accept")
    page.edit_audio(sid)
    assert wait_for(app, lambda: not get_queue().busy(), 30)
    assert get_queue().last.state == "done", get_queue().last.result_text
    r = get_db().song(sid)
    assert r["orig_name"] and r["level_done"] == 1
    page.edit_audio(sid, "video")
    assert wait_for(app, lambda: not get_queue().busy(), 30)
    assert get_queue().last.state == "done", get_queue().last.result_text
    assert abs(get_db().song(sid)["duration"] - 16) < 0.4          # the video part 4..20 s
    page.reset_audio(sid)
    assert wait_for(app, lambda: not get_queue().busy(), 30)
    assert ae.original_path(sid) is not None
    page.close()
