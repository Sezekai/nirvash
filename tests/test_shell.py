"""The redesigned shell: themes, the job queue, the move log, Storage, safe mode, one window, the window frame."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["NIRVASH_NO_PLAYBACK"] = "1"

from tests.test_ui_flows import app, wait_for  # noqa: E402,F401
from tests.test_pipeline import env, sh  # noqa: E402,F401


# ---- appearance (no Qt) ---------------------------------------------------------------
def test_presets_and_theme_files(tmp_path):
    from nirvash.core import appearance as ap
    assert ap.presets_check() == []                      # every preset keeps text at 4.5:1 or more
    t = ap.normalize(None)
    assert t["scene"] == {"image": "starfield", "opacity": 66}
    assert ap.describe(t)["background"] == "starglass" and ap.describe(t)["accent"] == "tealViolet"
    # Junk never breaks it
    bad = ap.normalize({"background": {"ground": "red", "panel": 5}, "accent": {"stops": ["#zzz"], "angle": 999},
                        "font": {"size": 99}, "density": {"control": 3}, "buttons": {"style": "neon"}})
    assert bad["background"]["ground"] == t["background"]["ground"] and bad["accent"]["angle"] == 360
    assert bad["font"]["size"] == 16 and bad["density"]["control"] == 24 and bad["buttons"]["style"] == "flat"
    # A theme from another app (no scene) is shown plain; ours keeps its scene through a file round trip
    other = ap.normalize({"name": "From Waifu2x", "background": ap.BACKGROUNDS["obsidian"]})
    assert other["scene"]["image"] == "none"
    a = ap.Appearance(tmp_path / "appearance.json")
    a.set_current(ap.with_background(t, "garnet"))
    name = a.save_as("Red night")
    assert a.active == name and not a.edited()
    a.set_current(ap.with_background(a.current, "olive"), active=name)
    assert a.edited()
    f = tmp_path / "x.theme.json"
    a.export_file(a.theme(name), f)
    j = json.loads(f.read_text())
    assert j["version"] == 1 and j["background"]["ground"] == ap.BACKGROUNDS["garnet"]["ground"]
    n2 = a.import_file(f)
    assert n2 != name and a.theme(n2)["background"] == a.theme(name)["background"]
    a.rename(n2, "Copy")
    a.duplicate("Copy")
    a.delete("Copy")
    b = ap.Appearance(tmp_path / "appearance.json")      # survives a restart
    assert [x["name"] for x in b.saved] == [name, "Copy copy"]


def test_font_guess():
    from nirvash.core import appearance as ap
    fam, info = ap.guess_font([Path("Satoshi-Regular.otf"), Path("Satoshi-Bold.otf"), Path("Satoshi-MediumItalic.otf")])
    assert fam == "Satoshi"
    assert [(i["weight"], i["style"]) for i in info] == [(400, "normal"), (700, "normal"), (500, "italic")]


def test_every_preset_applies(app):
    from PySide6.QtWidgets import QApplication
    from nirvash.core import appearance as ap
    from nirvash.ui import theme
    qa = QApplication.instance()
    for k in ap.BACKGROUNDS:
        theme.apply(qa, ap.with_background(None, k))
        assert theme.C["ground"] == ap.BACKGROUNDS[k]["ground"]
    for k, acc in ap.ACCENTS.items():
        t = ap.normalize({"accent": {"stops": acc["stops"], "angle": 45}})
        theme.apply(qa, t)
        assert "qlineargradient" in theme.stylesheet() or len(acc["stops"]) == 1
    theme.apply(qa, ap.normalize(None))
    pm = theme.scene_pixmap(800, 500)
    assert pm is not None and not pm.isNull()


# ---- the queue ----------------------------------------------------------------------------
def test_queue_runs_in_order_and_stops(app):
    from nirvash.ui.jobqueue import JobQueue
    q = JobQueue()
    order, done = [], []

    def work(ctx, name, steps):
        for i in range(steps):
            ctx.check()
            ctx.progress((i + 1) / steps, f"{name} {i}")
            time.sleep(0.02)
        order.append(name)
        return name

    a = q.add("A", work, "a", 5, on_done=done.append, done_text=lambda r: f"{r} ok")
    b = q.add("B", work, "b", 5, on_done=done.append)
    assert q.current is a and q.waiting == [b]
    assert wait_for(app, lambda: not q.busy(), 10)
    assert order == ["a", "b"] and done == ["a", "b"]
    assert a.result_text.startswith("Finished: a ok · took")
    assert a.frac == 1.0
    # progress is capped below 100% until the job has really finished
    seen = []
    c = q.add("C", work, "c", 3, on_progress=lambda f, m: seen.append(f))
    assert wait_for(app, lambda: not q.busy(), 10)
    assert seen and max(seen) <= 0.99 and c.frac == 1.0
    # Stop: the running job stops, the queue empties, finished work stays
    stopped = []
    d = q.add("D", work, "d", 200, on_stopped=lambda: stopped.append("d"))
    e = q.add("E", work, "e", 5, on_stopped=lambda: stopped.append("e"))
    wait_for(app, lambda: d.frac > 0.02, 5)
    q.stop_all()
    assert wait_for(app, lambda: not q.busy(), 10)
    assert d.state == "stopped" and e.state == "stopped" and set(stopped) == {"d", "e"}
    assert "e" not in order


def test_queue_pause(app):
    from nirvash.ui.jobqueue import JobQueue
    q = JobQueue()

    def work(ctx):
        for i in range(60):
            ctx.check()
            ctx.progress(i / 60, "")
            time.sleep(0.01)

    j = q.add("P", work, pausable=True)
    wait_for(app, lambda: j.frac > 0.1, 5)
    q.pause(True)
    f0 = j.frac
    t0 = time.time()
    while time.time() - t0 < 0.5:
        app.processEvents()
    assert j.paused and j.frac - f0 < 0.05
    q.pause(False)
    assert wait_for(app, lambda: not q.busy(), 10) and j.state == "done"
    assert j.paused_for >= 0.4


# ---- move log and undo -------------------------------------------------------------------------
def test_move_log_undo(env):
    from nirvash.core.db import get_db
    from nirvash.core.settings import get_settings
    from nirvash.jobs import file_ops
    db = get_db()
    p = get_settings().folder("songs") / "Some video.m4a"
    sh("ffmpeg", "-y", "-f", "lavfi", "-i", "sine=d=3", "-c:a", "aac", "-f", "ipod", str(p))
    sid = db.add_song(file_path=str(p), codec="aac", ext=".m4a", duration=3, title="Song", artist="A")
    new = file_ops.rename_and_tag(sid)
    m = db.last_move()
    assert m["src"] == str(p) and m["dst"] == str(new) and m["song_id"] == sid
    msg = file_ops.undo_move(m["id"])
    assert msg.startswith("Put ") and p.exists() and not new.exists()
    assert db.song(sid)["file_path"] == str(p)
    assert "already" in file_ops.undo_move(m["id"])
    # can't undo when the file was moved away outside Nirvash
    new = file_ops.rename_and_tag(sid)
    m = db.last_move()
    new.rename(new.with_name("moved by hand.m4a"))
    assert "outside Nirvash" in file_ops.undo_move(m["id"])


# ---- storage --------------------------------------------------------------------------------------
def test_storage_move_and_clear(env):
    from nirvash.core.db import get_db
    from nirvash.core.settings import get_settings
    from nirvash.jobs import storage
    from nirvash.jobs.context import JobContext
    s = get_settings()
    songs = s.folder("songs")
    (songs / "sub").mkdir()
    for i in range(3):
        (songs / "sub" / f"s{i}.m4a").write_bytes(os.urandom(200_000))
    sid = get_db().add_song(file_path=str(songs / "sub" / "s1.m4a"), duration=3)
    msgs = []
    ctx = JobContext(lambda f, m: msgs.append((f, m)))
    dest = env / "elsewhere"
    dest.mkdir()
    res = storage.move_place(ctx, "songs", str(dest))
    new = Path(res["to"])
    assert new == dest / songs.name and (new / "sub" / "s2.m4a").stat().st_size == 200_000
    assert not songs.exists()                                    # old copy removed only after the switch
    assert get_db().song(sid)["file_path"] == str(new / "sub" / "s1.m4a")
    assert s.folder("songs") == new
    assert any("MB" in m or "KB" in m for _f, m in msgs) and all(f <= 1 for f, _m in msgs)
    # clearing the cache frees its space
    s.set("folders.cache", str(env / "cachex"))
    (env / "cachex").mkdir()
    (env / "cachex" / "a.bin").write_bytes(b"1" * 5000)
    assert storage.clear_cache() == 5000 and not list((env / "cachex").iterdir())


def test_missing_drive_isnt_created(env, monkeypatch):
    from nirvash.core import paths
    monkeypatch.setattr(paths, "drive_ok", lambda p: False)
    target = env / "never" / "made"
    paths.ensure(target)
    assert not target.exists()


# ---- safe mode ------------------------------------------------------------------------------------
def test_safe_mode_after_two_crashes(env, monkeypatch):
    from nirvash.core import safemode
    monkeypatch.delenv("NIRVASH_NO_SAFE_MODE", raising=False)
    assert safemode.start() is False             # first run
    assert safemode.start() is False             # previous run didn't close cleanly (1)
    assert safemode.start() is True              # second unclean exit in a row
    safemode.turn_off()
    assert safemode.start() is False             # "Turn them back on"
    safemode.clean_exit()
    assert safemode.start() is False


# ---- one window ----------------------------------------------------------------------------------
def test_second_copy_brings_the_first_forward(app):
    from nirvash import app as appmod

    class W:
        shown = 0

        def isMinimized(self):
            return False

        def show(self):
            W.shown += 1

        def raise_(self):
            pass

        def activateWindow(self):
            pass

    server = appmod._listen(W())
    assert appmod._already_running() is True
    assert wait_for(app, lambda: W.shown >= 1, 5)
    server.close()


# ---- shell keys and panels ------------------------------------------------------------------------
def test_shell_keys(app):
    from PySide6.QtCore import Qt
    from nirvash.ui.main_window import MainWindow
    w = MainWindow()
    w.identify.stop()
    w.show()
    w.toggle_log(True)
    assert w.drawer.isVisible()
    w._escape()
    assert not w.drawer.isVisible()
    w.toggle_panels()
    assert not w.library.toolbar.isVisible() and not w.rail.isVisible()
    w._escape()
    assert w.library.toolbar.isVisible()
    w.toggle_rail()
    assert not w.rail.isVisible()
    w.toggle_rail()
    # K opens the shortcuts over the current screen; K, Esc or a click outside closes it
    w.go("scan")
    w.toggle_keys()
    assert w.keys_popup.isVisible() and w.current_page() is w.scan_page
    w._escape()
    assert not w.keys_popup.isVisible()
    w.toggle_keys()
    from PySide6.QtCore import QPoint
    from PySide6.QtTest import QTest
    QTest.mouseClick(w.keys_popup, Qt.LeftButton, pos=QPoint(3, 3))
    assert not w.keys_popup.isVisible()
    for size, cols in (((1920, 1040), 1), ((1100, 700), 2)):
        w.resize(*size)
        w.toggle_keys()
        app.processEvents()
        card = w.keys_popup.card.geometry()
        assert w.keys_popup.rect().contains(card), (size, card)
        assert w.keys_popup.columns >= cols, (size, w.keys_popup.columns)
        w.toggle_keys()
    for k in ("look", "settings", "scan", "auto", "editor", "library"):
        w.go(k)
        app.processEvents()
    # an error opens the log by itself
    from nirvash.core import log as _log
    _log.setup().error("test error")
    assert wait_for(app, lambda: w.drawer.isVisible(), 3)
    w.close()


@pytest.mark.skipif(sys.platform != "win32" or os.environ.get("QT_QPA_PLATFORM") == "offscreen",
                    reason="Windows window frame only")
def test_windows_frame(app):  # pragma: no cover - Windows only
    import ctypes
    from PySide6.QtWidgets import QApplication
    from nirvash.ui import frameless
    from nirvash.ui.main_window import MainWindow
    w = MainWindow()
    w.identify.stop()
    w.resize(1200, 760)
    w.show()
    for _ in range(20):
        QApplication.processEvents()
    hwnd = int(w.winId())
    u = ctypes.windll.user32
    style = u.GetWindowLongW(hwnd, -16)
    assert style & frameless.WS_THICKFRAME and style & frameless.WS_CAPTION

    def hit(lx, ly):
        class RECT(ctypes.Structure):
            _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long), ("r", ctypes.c_long), ("b", ctypes.c_long)]
        r = RECT()
        u.GetWindowRect(hwnd, ctypes.byref(r))
        dpr = w.devicePixelRatioF()
        x, y = r.l + int(lx * dpr), r.t + int(ly * dpr)
        return u.SendMessageW(hwnd, frameless.WM_NCHITTEST, 0, (y << 16) | (x & 0xFFFF))

    assert hit(400, 16) == frameless.HTCAPTION
    assert hit(1, 300) == frameless.HTLEFT
    assert hit(400, 300) == frameless.HTCLIENT
    bx = w.title_bar.btns[2].mapTo(w, w.title_bar.btns[2].rect().center())
    assert hit(bx.x(), bx.y()) == frameless.HTCLIENT          # the close button stays clickable
    w.showMaximized()
    for _ in range(20):
        QApplication.processEvents()
    avail = w.screen().availableGeometry()
    g = w.geometry()
    assert abs(g.width() - avail.width()) <= 2 and g.top() >= avail.top() - 1, (g, avail)
    w.close()


def test_row_click_volume_keys_delete_default(app, monkeypatch):
    from PySide6.QtCore import QModelIndex
    from nirvash.ui.library_page import COL, DeleteDialog, LibraryPage
    page = LibraryPage()
    # the "don't add the same music again" box starts off; skipping the videos stays on
    d = DeleteDialog("x", 2, page)
    assert not d.skip_songs.isChecked() and d.skip_videos.isChecked()
    d.done(0)
    # a plain click on the playing song's row pauses it; on its already-chosen row again carries on
    calls = []
    state = {"on": True}
    monkeypatch.setattr(page.player, "is_playing", lambda: state["on"])
    monkeypatch.setattr(page.player, "pause", lambda: (calls.append("pause"), state.update(on=False)))
    monkeypatch.setattr(page.player, "play", lambda: (calls.append("play"), state.update(on=True)))
    page.playing_sid = 7
    monkeypatch.setattr(page, "_sid", lambda idx: 7)
    idx = QModelIndex()
    monkeypatch.setattr(idx.__class__, "column", lambda self: COL["title"])
    page._row_was_current = False
    page._clicked(idx)
    assert calls == ["pause"]
    page._clicked(idx)                   # first click only chose the row: no resume
    assert calls == ["pause"]
    page._row_was_current = True
    page._clicked(idx)
    assert calls == ["pause", "play"]
    monkeypatch.undo()
    # - and = change the volume by 5, 0 mutes
    bar = page.bar
    bar.vol.setValue(50)
    page.key_volume_up()
    assert bar.vol.value() == 55
    page.key_volume_down()
    page.key_volume_down()
    assert bar.vol.value() == 45
    page.key_mute()
    assert bar.muted and bar.vol.value() == 45
    page.key_volume_up()                 # changing the volume unmutes
    assert not bar.muted and bar.vol.value() == 50
    page.close()
