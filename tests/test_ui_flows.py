"""Drive the real pages the way a person does (buttons -> background jobs -> results), offscreen.

Catches wiring mistakes between the UI and the jobs that unit tests of the jobs alone can't."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["NIRVASH_NO_PLAYBACK"] = "1"

from tests.test_pipeline import make_music_wav, sh  # noqa: E402


class AllMusicTagger:
    provider = "fake"

    def probs(self, chunks):
        return np.zeros((len(chunks), 527), np.float32)

    def classes(self, p):
        n = len(p)
        return {"music": np.ones(n), "speech": np.zeros(n), "singing": np.zeros(n)}

    def mood(self, p):
        return {"calm": 0.7, "happy": 0.3}


def wait_for(app, cond, timeout=240):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("NIRVASH_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("NIRVASH_ROOT", str(tmp_path / "root"))
    import nirvash.core.db as dbm
    import nirvash.core.settings as sm
    sm._settings = None
    dbm._db = None
    from nirvash.jobs import scan
    scan.reset_fp_index()
    monkeypatch.setattr(scan, "get_tagger", lambda: AllMusicTagger())
    from nirvash.analysis import models
    real_status = models.status
    monkeypatch.setattr(models, "status",
                        lambda spec: {"present": True, "dir": "x", "shared": False} if spec.key == "ast" else real_status(spec))
    from PySide6.QtWidgets import QApplication, QMessageBox
    a = QApplication.instance() or QApplication([])
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    from nirvash.ui.main_window import MainWindow
    monkeypatch.setattr(MainWindow, "_check_models", lambda self: None)
    monkeypatch.setenv("NIRVASH_AUTO_DIALOGS", "accept")
    yield a


def test_find_music_auto_music_and_library(app, tmp_path):
    from nirvash.core.db import get_db
    from nirvash.core.settings import get_settings
    from nirvash.ui.main_window import MainWindow

    # Videos with music
    src = tmp_path / "with music"
    src.mkdir()
    wav = tmp_path / "m.wav"
    make_music_wav(wav, 30)
    sh("ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25", "-i", str(wav), "-t", "30",
       "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(src / "song video.mp4"))
    # Silent videos
    silent = tmp_path / "silent"
    silent.mkdir()
    for i, d in enumerate((12, 20)):
        sh("ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc2=size=480x270:rate=30", "-t", str(d), "-c:v", "libx264",
           "-preset", "ultrafast", str(silent / f"clip {i}.mp4"))

    w = MainWindow()
    w.identify.pause(True)   # no network lookups in tests

    # --- Find music: same path as pressing the button
    get_settings().set("ui.last_scan_folders", [str(src)])
    page = w.scan_page
    for f in page._folders():
        page.remove_folder(f)
    page._folder_chip(str(src))
    page.start()
    assert wait_for(app, lambda: not w.queue.busy()), "scan didn't finish"
    last = w.queue.last
    assert last.state == "done", last.result_text
    assert page.counts["videos"] == 1 and page.counts["added"] == 1, (page.counts, last.result_text)
    assert last.result_text.startswith("Finished"), last.result_text
    songs = get_db().songs()
    assert len(songs) == 1 and Path(songs[0]["file_path"]).exists()
    sid = songs[0]["id"]

    # --- Library: favourite, category, refresh
    lib = w.library
    lib.reload()
    assert len(lib.model.rows) == 1
    lib.toggle_fav([sid])
    assert get_db().song(sid)["favourite"] == 1
    cid = get_db().add_category("Chill", "#2fe0c0")
    lib._set_cat([sid], cid, True)
    assert cid in get_db().song_category_ids(sid)
    lib.select_song(sid)
    wait_for(app, lambda: lib.detail.sid == sid, 5)

    # --- Auto-music: pick folder, find songs, export all
    auto = w.auto_page
    auto.folder = str(silent)
    auto.items = []
    auto.find_songs()
    assert wait_for(app, lambda: auto.job is None), "find songs didn't finish"
    assert len(auto.items) == 2 and all(i.song_id == sid for i in auto.items), [vars(i) for i in auto.items]
    auto.table.selectRow(1)
    wait_for(app, lambda: auto.cur is auto.items[1], 5)
    auto._set("volume", 0.8)            # an edit from the controls
    auto.export([i for i in auto.items if i.song_id and i.status == "matched"])
    assert wait_for(app, lambda: auto.job is None), "export didn't finish"
    for it in auto.items:
        assert it.status == "exported", (it.status, it.message)
        assert Path(it.output).exists()

    # --- Library: delete goes to the Recycle Bin stand-in
    import nirvash.ui.library_page as lp
    binned = []
    lp.to_recycle_bin = lambda p: (binned.append(p), Path(p).unlink(), True)[2]
    srcs = [x["video_path"] for x in get_db().sources(sid)]
    lib.ask_delete = lambda ids: (True, True)
    lib.delete([sid])
    assert wait_for(app, lambda: not w.queue.busy() and lib.batch is None), "delete didn't finish"
    assert get_db().song(sid) is None and binned
    assert {v.lower() for v in srcs} <= get_db().ignored_videos()
    assert get_db().ignore_counts()[1] == 1
    w.identify.stop()
    w.close()
