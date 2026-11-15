"""The editor screen on generated videos: open, trim, crop, blur, overlay, colour, undo, save, export."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["NIRVASH_NO_PLAYBACK"] = "1"

from tests.test_editor import _video  # noqa: E402
from tests.test_pipeline import env, sh  # noqa: E402,F401
from tests.test_ui_flows import wait_for  # noqa: E402


def test_editor_screen(env, monkeypatch):
    import cv2
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from nirvash.ui import theme
    theme.apply(app)
    monkeypatch.setenv("NIRVASH_AUTO_DIALOGS", "accept")
    from nirvash.core.db import get_db
    from nirvash.media import ff
    from nirvash.ui.editor_page import EditorPage
    from nirvash.ui.jobqueue import get_queue
    from PySide6.QtCore import Qt
    a = _video(env / "first.mp4", 320, 240, 4.0, 25)
    b = _video(env / "second.mp4", 320, 240, 2.0, 25)
    page = EditorPage()
    page.resize(1400, 820)
    page.show()
    assert page.stack.currentIndex() == 0                     # empty: drop or open
    page.open_files([str(a)])
    assert page.stack.currentIndex() == 1 and page.p.duration == 4.0
    assert wait_for(app, lambda: page.canvas.image is not None, 10), "no picture"
    page.add_files([str(b)])
    assert len(page.p.clips) == 2 and abs(page.p.duration - 6.0) < 0.05
    # trim the first clip by typing
    page.clip_list.setCurrentRow(0)
    page.in_edit.setText("0:01.00")
    page._trim_typed()
    assert page.p.clips[0].start == 1.0 and abs(page.p.duration - 5.0) < 0.05
    page.undo()
    assert page.p.clips[0].start == 0.0
    page.redo()
    assert page.p.clips[0].start == 1.0
    # crop: starts as the whole video; a shape; moving it later adds a move there
    page.set_tool("crop")
    page.crop_on.setChecked(True)
    assert page.p.crop.enabled and page.p.crop.keys[0][1:5] == [0.0, 0.0, 1.0, 1.0]
    assert page.canvas.view == "whole"
    page.aspect.setCurrentIndex(page.aspect.findData("1:1"))
    page._aspect_changed()
    W, H, _ = page.p.canvas()
    _t, x, y, w, h = page.p.crop.keys[0][:5]
    assert abs(w * W - h * H) < 2 and page.p.output_size() == (240, 240)
    page.seek(3.0)
    page._box_dragged("crop", "crop", (0.0, 0.0, w / 2, h / 2), "video")
    page.canvas.boxCommitted.emit()
    assert len(page.p.crop.keys) == 2
    assert page.p.crop_box(1.0)[0] == x and page.p.crop_box(3.0) == (0.0, 0.0, w / 2, h / 2)
    # clicking the timeline doesn't leave the crop tool
    page.timeline.picked.emit("clip", "0")
    assert page.tool == "crop"
    # fixing a part: mark 1..2 s, keep the box there
    page.seek(1.5)
    page._set_mark((1.0, 2.0))
    page._box_dragged("crop", "crop", (0.5, 0.5, w / 2, h / 2), "video")
    page._keep_for_part("crop", "crop")
    assert page.p.crop_box(1.5)[:2] == (0.5, 0.5) and page.p.crop_box(3.0)[:2] == (0.0, 0.0)
    page._set_mark(None)
    # blur box drawn with the mouse on the finished frame: shown while drawing, then draggable
    from PySide6.QtCore import QPoint
    from PySide6.QtTest import QTest
    page.set_tool("blur")
    assert page.canvas.view == "result"
    page.seek(0.5)
    assert wait_for(app, lambda: page.canvas.image is not None, 10)
    c = page.canvas
    r = c.pic_rect()
    a_ = QPoint(int(r.x() + r.width() * 0.1), int(r.y() + r.height() * 0.6))
    b_ = QPoint(int(r.x() + r.width() * 0.4), int(r.y() + r.height() * 0.9))
    QTest.mousePress(c, Qt.LeftButton, Qt.NoModifier, a_)
    QTest.mouseMove(c, b_)
    assert c._temp_new is not None and c._temp_new[2] > 0.2
    QTest.mouseRelease(c, Qt.LeftButton, Qt.NoModifier, b_)
    assert len(page.p.blurs) == 1 and page.sel[0] == "blur" and page.p.blurs[0].space == "output"
    k0 = list(page.p.blurs[0].keys[0])
    m_ = QPoint(int(r.x() + r.width() * 0.25), int(r.y() + r.height() * 0.75))
    QTest.mousePress(c, Qt.LeftButton, Qt.NoModifier, m_)
    QTest.mouseMove(c, m_ + QPoint(30, -20))
    QTest.mouseRelease(c, Qt.LeftButton, Qt.NoModifier, m_ + QPoint(30, -20))
    assert page.p.blurs[0].keys[0][1] > k0[1] and page.p.blurs[0].keys[0][2] < k0[2]
    page.blur_kind.changed.emit("pixelate")
    assert page.p.blurs[0].kind == "pixelate"
    # overlay from the library
    from nirvash.edit import overlays as ovl
    png = env / "logo.png"
    img = np.zeros((40, 40, 4), np.uint8)
    img[..., 2] = 255
    img[..., 3] = 200
    cv2.imwrite(str(png), img)
    lib = ovl.import_files([str(png)], ovl.root())
    page._fill_overlay_library()
    assert page.ov_files.count() == 1
    page.add_overlay(str(lib[0]))
    assert len(page.p.overlays) == 1 and page.tools.currentWidget() is page.pages["overlay"]
    # colour
    page._colour_moved("saturation", 1.4)
    page.changed(frames=True)
    # sound: even out, louder part
    page.set_tool("sound")
    page.norm.setChecked(True)
    assert wait_for(app, lambda: page.p.sound.measured and page.p.sound.measured[0] == page.p.sound_signature(), 20)
    page._set_mark((2.0, 3.0))
    page._add_section(6.0)
    assert len(page.p.sound.sections) == 1 and page.sel[0] == "sound"
    page._set_mark(None)
    # saved as you go, and picked up again
    page._save()
    assert get_db().load_edit(str(a).casefold()) is not None
    page.close_project()
    page.open_files([str(a)])
    assert len(page.p.clips) == 2 and len(page.p.overlays) == 1 and page.p.colour.saturation == 1.4
    # the result view renders
    page.set_tool("overlay")
    page.seek(1.0)
    assert wait_for(app, lambda: page.canvas.image is not None, 10)
    assert page.canvas.image.width() < page.canvas.base.shape[1] + 1
    OW, OH = page.p.output_size()
    # export through the queue
    page.export()
    assert wait_for(app, lambda: not get_queue().busy(), 120)
    qj = get_queue().last
    assert qj.state == "done", qj.result_text
    out = Path(page.p.default_output_path())
    pr = ff.probe(out)
    assert (pr.width, pr.height) == (OW, OH) and abs(pr.duration - 5.0) < 0.3
    # live preview plays (pictures arrive on time) and stops
    page.seek(0.5)
    page.key_play_pause()
    assert wait_for(app, lambda: page.t > 1.2, 10), page.t
    page.key_play_pause()
    assert not page.preview.playing
    # following a subject with the blur box, through the queue
    page.seek(0.5)
    page.sel = ("blur", page.p.blurs[0].id)
    page.track("blur", page.p.blurs[0].id)
    assert wait_for(app, lambda: not get_queue().busy(), 60)
    assert get_queue().last.state == "done", get_queue().last.result_text
    assert page.p.blurs[0].space == "video" and len(page.p.blurs[0].keys) > 5
    page.undo()
    assert len(page.p.blurs[0].keys) == 1
    # start over: everything gone, and undoable
    page.start_over()
    assert len(page.p.clips) == 1 and not page.p.blurs and not page.p.crop.enabled
    page.undo()
    assert len(page.p.clips) == 2
    # keyboard: frame steps
    page.seek(1.0)
    page.key_next()
    assert abs(page.t - 1.04) < 0.01
    page.close()
