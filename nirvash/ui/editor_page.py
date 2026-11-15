"""The Editor: trims, joins, crop and moving crop / zoom (by hand or by following a subject), turning and
straightening, blur and pixelate boxes, overlays from the overlay library, colour, and export.

Edits are saved as you go and come back when the same video is opened again. Nothing is changed in the original
files: exports go next to the first video as "<name>_edited.mp4" and never overwrite anything."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
                               QLineEdit, QListWidget, QListWidgetItem, QMenu, QScrollArea, QSlider, QSplitter,
                               QStackedWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from ..core import log as _log
from ..core.db import get_db
from ..edit import overlays as ovl
from ..edit import render as R
from ..edit.project import ASPECTS, Blur, Overlay, Project, Section, apply_section, clip_from_file, interp, set_key
from . import dialogs, theme, workers
from .audio_editor import RangeWave, fmt_precise, parse_time
from .editor_canvas import FrameCanvas, PreviewPlayer, Timeline
from .hub import hub
from .jobqueue import get_queue
from .player import Player
from .theme import C, icon
from .waveform import compute_peaks
from .widgets import Segmented, button, fmt_time, label, page_header, panel

log = _log.get("editor")

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".wmv", ".flv", ".ts", ".mts", ".m2ts", ".3gp", ".gif"}
TOOLS = [("clip", "Clips"), ("crop", "Crop"), ("blur", "Blur"), ("overlay", "Overlays"), ("colour", "Colour"),
         ("sound", "Sound"), ("export", "Export")]


def _slider(lo: int, hi: int, val: int) -> QSlider:
    s = QSlider(Qt.Horizontal)
    s.setRange(lo, hi)
    s.setValue(val)
    return s


class EditorPage(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("page")
        self.setAcceptDrops(True)
        self.p: Project | None = None
        self.key: str | None = None
        self.t = 0.0
        self.undo_stack: list[str] = []
        self.redo_stack: list[str] = []
        self._frame_cache: dict = {}
        self._grab_token = 0
        self._last_saved = ""
        self.sel: tuple[str, str] | None = None
        self.mark: tuple[float, float] | None = None    # a marked part of the timeline
        self.tool = "clip"
        self.queue = get_queue()

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 12)
        root.setSpacing(8)
        head, self.desc = page_header("Editor", "Trim, join, crop and zoom, follow a subject, blur, overlays, colour. "
                                                "The originals are never changed")
        root.addLayout(head)
        bar = QHBoxLayout()
        bar.setSpacing(6)
        ob = button("Open video…", "folder")
        ob.clicked.connect(self.open_dialog)
        bar.addWidget(ob)
        ab = button("Add video to join…", "plus")
        ab.clicked.connect(self.add_dialog)
        bar.addWidget(ab)
        self.add_btn = ab
        self.recent_btn = button("Recent ▾", None, "menuBtn")
        self.recent_btn.clicked.connect(self._recent_menu)
        bar.addWidget(self.recent_btn)
        self.name_lbl = label("", "muted")
        bar.addWidget(self.name_lbl, 1)
        self.reset_btn = button("Start over…", "trash", "ghost")
        self.reset_btn.setToolTip("Throw away every edit of this video and start again from the original")
        self.reset_btn.clicked.connect(self.start_over)
        bar.addWidget(self.reset_btn)
        self.undo_btn = button("Undo", "undo", "ghost")
        self.undo_btn.setToolTip("Undo (Ctrl+Z)")
        self.undo_btn.clicked.connect(self.undo)
        bar.addWidget(self.undo_btn)
        self.redo_btn = button("Redo", "refresh", "ghost")
        self.redo_btn.setToolTip("Redo (Ctrl+Y)")
        self.redo_btn.clicked.connect(self.redo)
        bar.addWidget(self.redo_btn)
        self.export_btn = button("Export", "export", "primary")
        self.export_btn.clicked.connect(self.export)
        bar.addWidget(self.export_btn)
        root.addLayout(bar)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)
        self.stack.addWidget(self._empty_view())
        work = QSplitter(Qt.Horizontal)
        work.setChildrenCollapsible(False)
        self.left_w = self._left_panel()
        self.right_w = self._right_panel()
        work.addWidget(self.left_w)
        work.addWidget(self._centre())
        work.addWidget(self.right_w)
        work.setStretchFactor(1, 1)
        work.setSizes([230, 900, 320])
        self.stack.addWidget(work)

        self.audio = Player(self, video=False)
        self.preview = PreviewPlayer(self.canvas, self.audio, self._play_time, self._play_end)
        self._save_timer = QTimer(self, singleShot=True, interval=800, timeout=self._save)
        self._grab_timer = QTimer(self, singleShot=True, interval=40, timeout=self._grab)
        self._ops_timer = QTimer(self, singleShot=True, interval=30, timeout=self._ops_changed)
        from PySide6.QtGui import QKeySequence, QShortcut
        for seq, fn in (("I", self.key_in), ("O", self.key_out), ("Delete", self.key_delete), ("Ctrl+Y", self.redo),
                        ("Ctrl+Shift+Z", self.redo), ("E", lambda: self.set_tool("export"))):
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(fn)
        self._sync_all()

    # =========================================================================================================
    # layout
    # =========================================================================================================
    def _empty_view(self) -> QWidget:
        w = panel()
        l = QVBoxLayout(w)
        l.setAlignment(Qt.AlignCenter)
        ic = label("")
        ic.setPixmap(theme.pixmap("cut", 34, C["faint"]))
        ic.setAlignment(Qt.AlignCenter)
        l.addWidget(ic)
        t = label("Drop a video here, or open one", "h2")
        t.setAlignment(Qt.AlignCenter)
        l.addWidget(t)
        s = label("Add more videos to join them. Your edits are saved as you go and come back when you open the "
                  "same video again.", "muted", wrap=True)
        s.setAlignment(Qt.AlignCenter)
        s.setMaximumWidth(460)
        l.addWidget(s, 0, Qt.AlignHCenter)
        b = button("Open video…", "folder", "primary")
        b.clicked.connect(self.open_dialog)
        l.addWidget(b, 0, Qt.AlignHCenter)
        self.empty_recent = QVBoxLayout()
        l.addSpacing(10)
        l.addLayout(self.empty_recent)
        return w

    def _left_panel(self) -> QWidget:
        w = panel()
        w.setMinimumWidth(200)
        l = QVBoxLayout(w)
        l.setContentsMargins(8, 8, 8, 8)
        l.setSpacing(6)
        self.left_seg = Segmented([("clips", "Clips"), ("library", "Overlay library")])
        self.left_seg.changed.connect(lambda v: self.left_stack.setCurrentIndex(0 if v == "clips" else 1))
        l.addWidget(self.left_seg)
        self.left_stack = QStackedWidget()
        l.addWidget(self.left_stack, 1)
        # clips
        cw = QWidget()
        cw.setObjectName("transparent")
        cl = QVBoxLayout(cw)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(6)
        self.clip_list = QListWidget()
        self.clip_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.clip_list.model().rowsMoved.connect(self._clips_reordered)
        self.clip_list.currentRowChanged.connect(self._clip_selected)
        cl.addWidget(self.clip_list, 1)
        cl.addWidget(label("Drag to change the order. The edit plays them one after another.", "faint", wrap=True))
        self.left_stack.addWidget(cw)
        # overlay library
        lw = QWidget()
        lw.setObjectName("transparent")
        ll = QVBoxLayout(lw)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(6)
        self.ov_tree = QTreeWidget()
        self.ov_tree.setHeaderHidden(True)
        self.ov_tree.setMaximumHeight(140)
        self.ov_tree.currentItemChanged.connect(lambda *_: self._fill_overlay_files())
        self.ov_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.ov_tree.customContextMenuRequested.connect(self._ov_folder_menu)
        ll.addWidget(self.ov_tree)
        self.ov_files = QListWidget()
        self.ov_files.setViewMode(QListWidget.IconMode)
        self.ov_files.setIconSize(QSize(72, 72))
        self.ov_files.setGridSize(QSize(88, 100))
        self.ov_files.setResizeMode(QListWidget.Adjust)
        self.ov_files.setWordWrap(True)
        self.ov_files.itemDoubleClicked.connect(lambda it: self.add_overlay(it.data(Qt.UserRole)))
        self.ov_files.setContextMenuPolicy(Qt.CustomContextMenu)
        self.ov_files.customContextMenuRequested.connect(self._ov_file_menu)
        ll.addWidget(self.ov_files, 1)
        r = QHBoxLayout()
        ib = button("Add files…", "plus")
        ib.clicked.connect(self.import_overlays)
        r.addWidget(ib)
        nb = button("New folder", "folder", "ghost")
        nb.clicked.connect(self.new_overlay_folder)
        r.addWidget(nb)
        ll.addLayout(r)
        ll.addWidget(label("Double-click one to put it on the video.", "faint", wrap=True))
        self.left_stack.addWidget(lw)
        return w

    def _centre(self) -> QWidget:
        w = QWidget()
        w.setObjectName("transparent")
        l = QVBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(6)
        self.canvas = FrameCanvas()
        self.canvas.setObjectName("stage")
        self.canvas.boxChanged.connect(self._box_dragged)
        self.canvas.boxCommitted.connect(lambda: self.changed())
        self.canvas.boxPicked.connect(self._picked)
        self.canvas.boxDrawn.connect(self._blur_drawn)
        l.addWidget(self.canvas, 1)
        tr = QHBoxLayout()
        tr.setSpacing(6)
        self.play_btn = button("", "play", "primary")
        self.play_btn.setToolTip("Play / pause the edit (Space)")
        self.play_btn.setFixedWidth(40)
        self.play_btn.clicked.connect(self.key_play_pause)
        tr.addWidget(self.play_btn)
        self.time_lbl = label("0:00.00 / 0:00", "mono")
        tr.addWidget(self.time_lbl)
        tr.addStretch(1)
        self.whole_box = QCheckBox("Whole picture")
        self.whole_box.setToolTip("Show the whole video instead of the finished (cropped) frame. Cropping always "
                                  "shows the whole picture.")
        self.whole_box.toggled.connect(lambda _v: self._update_view())
        tr.addWidget(self.whole_box)
        l.addLayout(tr)
        tr = QHBoxLayout()
        tr.setSpacing(6)
        tr.addWidget(label("Part", "cap"))
        ms = button("Starts here", "tri_down", "ghost")
        ms.setToolTip("Mark a part of the video, from the playhead (or drag along the times above the clips)")
        ms.clicked.connect(lambda: self._mark_at("a"))
        tr.addWidget(ms)
        me = button("Ends here", "tri_down", "ghost")
        me.clicked.connect(lambda: self._mark_at("b"))
        tr.addWidget(me)
        self.mark_lbl = label("none marked", "faint")
        tr.addWidget(self.mark_lbl)
        self.mark_clear = button("", "x", "ghost")
        self.mark_clear.setToolTip("Unmark")
        self.mark_clear.setFixedWidth(30)
        self.mark_clear.clicked.connect(lambda: self._set_mark(None))
        tr.addWidget(self.mark_clear)
        tr.addStretch(1)
        l.addLayout(tr)
        self.timeline = Timeline()
        self.timeline.seek.connect(self.seek)
        self.timeline.picked.connect(self._picked)
        self.timeline.rangeChanged.connect(self._range_dragged)
        self.timeline.committed.connect(lambda: self.changed())
        self.timeline.marked.connect(self._set_mark)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.NoFrame)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sc.setWidget(self.timeline)
        sc.setMaximumHeight(170)
        sc.setMinimumHeight(70)
        self.timeline_scroll = sc
        l.addWidget(sc)
        return w

    def _right_panel(self) -> QWidget:
        outer = panel()
        outer.setMinimumWidth(300)
        outer.setMaximumWidth(380)
        ol = QVBoxLayout(outer)
        ol.setContentsMargins(8, 8, 8, 8)
        ol.setSpacing(8)
        self.tool_seg = Segmented(TOOLS[:4])
        self.tool_seg2 = Segmented(TOOLS[4:])
        self.tool_seg.changed.connect(self.set_tool)
        self.tool_seg2.changed.connect(self.set_tool)
        ol.addWidget(self.tool_seg)
        ol.addWidget(self.tool_seg2)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.NoFrame)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.tools = QStackedWidget()
        self.tools.setObjectName("transparent")
        sc.setWidget(self.tools)
        ol.addWidget(sc, 1)
        self.pages = {}
        for key, builder in (("clip", self._clip_tool), ("crop", self._crop_tool), ("blur", self._blur_tool),
                             ("overlay", self._overlay_tool), ("colour", self._colour_tool),
                             ("sound", self._sound_tool), ("export", self._export_tool)):
            w = QWidget()
            w.setObjectName("transparent")
            lay = QVBoxLayout(w)
            lay.setContentsMargins(2, 2, 2, 2)
            lay.setSpacing(8)
            builder(lay)
            lay.addStretch(1)
            self.tools.addWidget(w)
            self.pages[key] = w
        return outer

    # ---- tool pages ------------------------------------------------------------------------------------------
    def _clip_tool(self, l: QVBoxLayout) -> None:
        self.clip_name = label("", "h2", wrap=True)
        l.addWidget(self.clip_name)
        l.addWidget(label("Trim: drag the handles (its sound is shown). I and O set them at the playhead.", "muted",
                          wrap=True))
        self.trim = RangeWave()
        self.trim.setMinimumHeight(86)
        self.trim.rangeChanged.connect(self._trim_dragged)
        self.trim.seekRequested.connect(lambda t: self._seek_in_clip(t))
        l.addWidget(self.trim)
        g = QGridLayout()
        g.setHorizontalSpacing(6)
        self.in_edit, self.out_edit = QLineEdit(), QLineEdit()
        for n, (name, e) in enumerate((("Start", self.in_edit), ("End", self.out_edit))):
            e.setFixedWidth(84)
            e.editingFinished.connect(self._trim_typed)
            g.addWidget(label(name, "cap"), 0, n * 2)
            g.addWidget(e, 0, n * 2 + 1)
        l.addLayout(g)
        self.clip_len = label("", "faint")
        l.addWidget(self.clip_len)
        l.addWidget(label("Turn and flip", "cap"))
        r = QHBoxLayout()
        for txt, ic, fn in (("Turn left", "undo", lambda: self._turn(-90)), ("Turn right", "refresh", lambda: self._turn(90))):
            b = button(txt, ic)
            b.clicked.connect(fn)
            r.addWidget(b)
        r.addStretch(1)
        l.addLayout(r)
        r = QHBoxLayout()
        self.flip_h = QCheckBox("Mirror")
        self.flip_v = QCheckBox("Upside down")
        self.flip_h.toggled.connect(lambda v: self._clip_set(flip_h=v))
        self.flip_v.toggled.connect(lambda v: self._clip_set(flip_v=v))
        r.addWidget(self.flip_h)
        r.addWidget(self.flip_v)
        r.addStretch(1)
        l.addLayout(r)
        l.addWidget(label("Straighten", "cap"))
        r = QHBoxLayout()
        self.angle = _slider(-150, 150, 0)     # tenths of a degree
        self.angle.valueChanged.connect(self._angle_moved)
        self.angle.sliderReleased.connect(lambda: self.changed())
        r.addWidget(self.angle, 1)
        self.angle_lbl = label("0°", "mono")
        self.angle_lbl.setFixedWidth(48)
        r.addWidget(self.angle_lbl)
        l.addLayout(r)
        self.mute = QCheckBox("Mute this clip's sound")
        self.mute.toggled.connect(lambda v: self._clip_set(mute=v))
        l.addWidget(self.mute)
        rb = button("Remove from the edit", "trash", "dangerGhost")
        rb.clicked.connect(self.remove_clip)
        l.addWidget(rb)
        self.clip_info = label("", "faint", wrap=True)
        l.addWidget(self.clip_info)

    def _crop_tool(self, l: QVBoxLayout) -> None:
        self.crop_on = QCheckBox("Crop")
        self.crop_on.toggled.connect(self._crop_toggled)
        l.addWidget(self.crop_on)
        l.addWidget(label("Finished shape", "cap"))
        r = QHBoxLayout()
        self.aspect = QComboBox()
        for k in ASPECTS:
            if k != "free":
                self.aspect.addItem({"source": "Same as the video"}.get(k, k), k)
        self.aspect.setToolTip("The finished video's shape. It's as big as fits in the video; every crop is fitted "
                               "into it, so zooming never changes its size.")
        self.aspect.activated.connect(self._aspect_changed)
        r.addWidget(self.aspect, 1)
        l.addLayout(r)
        l.addWidget(label("Move or resize the box on the picture. Go to another moment and move it again: it stays "
                          "where you put it until you move it again, and glides between the two. Smaller zooms in; "
                          "bigger zooms out (it can go past the edges).", "muted", wrap=True))
        r = QHBoxLayout()
        r.addWidget(label("Moves take", "cap"))
        self.trans = _slider(0, 30, 5)            # tenths of a second
        self.trans.valueChanged.connect(self._trans_moved)
        self.trans.sliderReleased.connect(lambda: self.changed())
        r.addWidget(self.trans, 1)
        self.trans_lbl = label("0.5 s", "mono")
        self.trans_lbl.setFixedWidth(44)
        r.addWidget(self.trans_lbl)
        l.addLayout(r)
        self.keep_shape = QCheckBox("Keep the box the finished shape")
        self.keep_shape.setToolTip("Off: the box can be any shape; it's fitted in without stretching and the rest "
                                   "is filled")
        self.keep_shape.toggled.connect(lambda v: self._crop_set(keep_shape=v))
        l.addWidget(self.keep_shape)
        l.addWidget(label("Fill the rest of the frame with", "cap"))
        r = QHBoxLayout()
        self.fill = Segmented([("blur", "Blurred picture"), ("colour", "Colour")])
        self.fill.changed.connect(lambda v: self._crop_set(fill=v))
        r.addWidget(self.fill)
        self.fill_col = button("", None)
        self.fill_col.setFixedSize(30, 26)
        self.fill_col.setToolTip("The fill colour")
        self.fill_col.clicked.connect(self._pick_fill_colour)
        r.addWidget(self.fill_col)
        r.addStretch(1)
        l.addLayout(r)
        b = button("Follow a subject", "scan", "primary")
        b.setToolTip("Put the box around it at the playhead first. It follows it to the end of the marked part, or "
                     "to the end of the video.")
        b.clicked.connect(lambda: self.track("crop", "crop"))
        l.addWidget(b)
        self.crop_section = button("Keep this box for the marked part", "check")
        self.crop_section.setToolTip("Fix a stretch the following got wrong: put the box right at the playhead, mark "
                                     "the part, then use this. Before and after stay as they were.")
        self.crop_section.clicked.connect(lambda: self._keep_for_part("crop", "crop"))
        l.addWidget(self.crop_section)
        d = button("Remove the move here", "minus", "ghost")
        d.clicked.connect(lambda: self._delete_key("crop", "crop"))
        l.addWidget(d, 0, Qt.AlignLeft)
        c = button("Use this box for the whole video", "refresh", "ghost")
        c.setToolTip("Remove every move: the box stays as it is now, all the way through")
        c.clicked.connect(lambda: self._clear_keys("crop", "crop"))
        l.addWidget(c, 0, Qt.AlignLeft)
        self.crop_info = label("", "faint", wrap=True)
        l.addWidget(self.crop_info)

    def _blur_tool(self, l: QVBoxLayout) -> None:
        l.addWidget(label("Draw a box on the picture to blur that part; drag it or its corners to change it.", "muted",
                          wrap=True))
        r = QHBoxLayout()
        ab = button("Add a box", "plus")
        ab.clicked.connect(lambda: self._blur_drawn((0.4, 0.4, 0.2, 0.2)))
        r.addWidget(ab)
        self.blur_del = button("Remove", "trash", "dangerGhost")
        self.blur_del.clicked.connect(self.delete_selected)
        r.addWidget(self.blur_del)
        r.addStretch(1)
        l.addLayout(r)
        self.blur_box = QWidget()
        self.blur_box.setObjectName("transparent")
        bl = QVBoxLayout(self.blur_box)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(8)
        self.blur_space = Segmented([("output", "On the frame"), ("video", "On the picture")])
        self.blur_space.setToolTip("On the frame: stays in the same spot of the finished video, even when the crop "
                                   "moves. With the picture: sticks to what's filmed (following a subject uses this).")
        self.blur_space.changed.connect(self._blur_space)
        bl.addWidget(self.blur_space)
        self.blur_kind = Segmented([("blur", "Blur"), ("pixelate", "Pixelate")])
        self.blur_kind.changed.connect(lambda v: self._blur_set(kind=v))
        bl.addWidget(self.blur_kind)
        self.blur_shape = Segmented([("rect", "Box"), ("ellipse", "Oval")])
        self.blur_shape.changed.connect(lambda v: self._blur_set(shape=v))
        bl.addWidget(self.blur_shape)
        r = QHBoxLayout()
        r.addWidget(label("Strength", "cap"))
        self.blur_strength = _slider(1, 100, 50)
        self.blur_strength.valueChanged.connect(lambda v: self._blur_set(strength=v, commit=False))
        self.blur_strength.sliderReleased.connect(lambda: self.changed())
        r.addWidget(self.blur_strength, 1)
        bl.addLayout(r)
        bl.addLayout(self._when_row("blur"))
        self.blur_moving = QCheckBox("Moves at different moments")
        self.blur_moving.setToolTip("Off: dragging it just moves it, for its whole time. On: like the crop, it stays "
                                    "where you put it at each moment and glides between.")
        self.blur_moving.toggled.connect(lambda v: self._blur_set(moving=v))
        bl.addWidget(self.blur_moving)
        tb = button("Follow a subject", "scan", "primary")
        tb.setToolTip("Put the box on it at the playhead; the box follows it until the box's end time")
        tb.clicked.connect(lambda: self.sel and self.track("blur", self.sel[1]))
        bl.addWidget(tb)
        ks = button("Keep this box for the marked part", "check")
        ks.clicked.connect(lambda: self.sel and self._keep_for_part("blur", self.sel[1]))
        bl.addWidget(ks)
        dk = button("Remove the move here", "minus", "ghost")
        dk.clicked.connect(lambda: self.sel and self._delete_key("blur", self.sel[1]))
        bl.addWidget(dk, 0, Qt.AlignLeft)
        l.addWidget(self.blur_box)

    def _when_row(self, kind: str) -> QGridLayout:
        g = QGridLayout()
        g.setHorizontalSpacing(6)
        lbl = label("", "faint")
        setattr(self, f"{kind}_when", lbl)
        g.addWidget(lbl, 0, 0, 1, 3)
        a = button("Starts here", "tri_down", "ghost")
        a.clicked.connect(lambda: self._set_when(kind, "a"))
        b = button("Ends here", "tri_down", "ghost")
        b.clicked.connect(lambda: self._set_when(kind, "b"))
        w = button("Whole video", "refresh", "ghost")
        w.clicked.connect(lambda: self._set_when(kind, "all"))
        g.addWidget(a, 1, 0)
        g.addWidget(b, 1, 1)
        g.addWidget(w, 2, 0)
        return g

    def _overlay_tool(self, l: QVBoxLayout) -> None:
        l.addWidget(label("Put pictures, GIFs or short videos on top. Keep them in the overlay library (left), in "
                          "folders, and double-click one to add it.", "muted", wrap=True))
        r = QHBoxLayout()
        lb = button("Open the library", "folder")
        lb.clicked.connect(lambda: self.left_seg.set_value("library") or self.left_stack.setCurrentIndex(1))
        r.addWidget(lb)
        self.ov_del = button("Remove", "trash", "dangerGhost")
        self.ov_del.clicked.connect(self.delete_selected)
        r.addWidget(self.ov_del)
        r.addStretch(1)
        l.addLayout(r)
        self.ov_box = QWidget()
        self.ov_box.setObjectName("transparent")
        bl = QVBoxLayout(self.ov_box)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(8)
        self.ov_name = label("", "h2", wrap=True)
        bl.addWidget(self.ov_name)
        r = QHBoxLayout()
        r.addWidget(label("See-through", "cap"))
        self.ov_opacity = _slider(5, 100, 100)
        self.ov_opacity.valueChanged.connect(lambda v: self._overlay_set(opacity=v / 100, commit=False))
        self.ov_opacity.sliderReleased.connect(lambda: self.changed())
        r.addWidget(self.ov_opacity, 1)
        bl.addLayout(r)
        bl.addLayout(self._when_row("overlay"))
        bl.addWidget(label("Drag it on the picture to place it; drag a corner to size it. It stays in the same spot "
                           "of the finished video even when the crop moves.", "faint", wrap=True))
        l.addWidget(self.ov_box)

    def _colour_tool(self, l: QVBoxLayout) -> None:
        self.colour_sliders = {}
        for key, name, lo, hi, scale in (("brightness", "Brightness", -100, 100, 100), ("contrast", "Contrast", 50, 200, 100),
                                         ("saturation", "Colour", 0, 250, 100), ("gamma", "Shadows (gamma)", 50, 200, 100),
                                         ("warmth", "Warmth", -100, 100, 100), ("tint", "Tint (green ↔ pink)", -100, 100, 100),
                                         ("sharpen", "Sharpen", 0, 100, 100)):
            r = QHBoxLayout()
            lb = label(name, "cap")
            lb.setFixedWidth(116)
            r.addWidget(lb)
            s = _slider(lo, hi, 0)
            s.valueChanged.connect(lambda v, k=key, sc=scale: self._colour_moved(k, v / sc))
            s.sliderReleased.connect(lambda: self.changed())
            r.addWidget(s, 1)
            val = label("", "mono")
            val.setFixedWidth(40)
            r.addWidget(val)
            l.addLayout(r)
            self.colour_sliders[key] = (s, scale, val)
        rb = button("Reset colour", "undo", "ghost")
        rb.clicked.connect(self._colour_reset)
        l.addWidget(rb)
        l.addWidget(label("Colour changes show on the picture straight away.", "faint", wrap=True))

    def _sound_tool(self, l: QVBoxLayout) -> None:
        self.norm = QCheckBox("Even out the loudness")
        self.norm.setToolTip("Brings the whole edit to the same loudness as your song library (Settings > Song "
                             "volume), without letting it distort")
        self.norm.toggled.connect(self._norm_toggled)
        l.addWidget(self.norm)
        self.norm_lbl = label("", "faint", wrap=True)
        l.addWidget(self.norm_lbl)
        r = QHBoxLayout()
        lb = label("Volume", "cap")
        lb.setFixedWidth(80)
        r.addWidget(lb)
        self.vol = _slider(-40, 40, 0)          # half dB
        self.vol.valueChanged.connect(self._vol_moved)
        self.vol.sliderReleased.connect(lambda: self.changed())
        r.addWidget(self.vol, 1)
        self.vol_lbl = label("0 dB", "mono")
        self.vol_lbl.setFixedWidth(60)
        r.addWidget(self.vol_lbl)
        l.addLayout(r)
        l.addWidget(label("Louder or quieter parts", "h2"))
        l.addWidget(label("Mark a part (drag along the times above the clips, or Starts here / Ends here under the "
                          "picture), then:", "muted", wrap=True))
        r = QHBoxLayout()
        lo = button("Make it louder", "plus")
        lo.clicked.connect(lambda: self._add_section(6.0))
        r.addWidget(lo)
        qu = button("Make it quieter", "minus")
        qu.clicked.connect(lambda: self._add_section(-6.0))
        r.addWidget(qu)
        r.addStretch(1)
        l.addLayout(r)
        self.sec_box = QWidget()
        self.sec_box.setObjectName("transparent")
        sl = QVBoxLayout(self.sec_box)
        sl.setContentsMargins(0, 0, 0, 0)
        self.sec_when = label("", "faint")
        sl.addWidget(self.sec_when)
        r = QHBoxLayout()
        lb = label("This part", "cap")
        lb.setFixedWidth(80)
        r.addWidget(lb)
        self.sec_gain = _slider(-40, 40, 12)
        self.sec_gain.valueChanged.connect(self._sec_moved)
        self.sec_gain.sliderReleased.connect(lambda: self.changed())
        r.addWidget(self.sec_gain, 1)
        self.sec_lbl = label("", "mono")
        self.sec_lbl.setFixedWidth(60)
        r.addWidget(self.sec_lbl)
        sl.addLayout(r)
        rm = button("Remove this part", "trash", "dangerGhost")
        rm.clicked.connect(self.delete_selected)
        sl.addWidget(rm, 0, Qt.AlignLeft)
        l.addWidget(self.sec_box)
        l.addWidget(label("Parts show on the Sound line of the timeline: click one to change it, drag its ends to "
                          "move them. The sound fades smoothly in and out of them.", "faint", wrap=True))

    def _export_tool(self, l: QVBoxLayout) -> None:
        l.addWidget(label("File name", "cap"))
        self.out_name = QLineEdit()
        self.out_name.editingFinished.connect(self._out_name)
        l.addWidget(self.out_name)
        self.out_where = label("", "faint", wrap=True)
        l.addWidget(self.out_where)
        r = QHBoxLayout()
        r.addWidget(label("Quality", "cap"))
        self.quality = QComboBox()
        for k, t in (("best", "Best (biggest file)"), ("high", "High"), ("smaller", "Smaller file")):
            self.quality.addItem(t, k)
        self.quality.activated.connect(lambda: self._out_set(quality=self.quality.currentData()))
        r.addWidget(self.quality, 1)
        l.addLayout(r)
        self.gpu = QCheckBox("Export faster (graphics card)")
        self.gpu.setToolTip("NVENC on the NVIDIA card: much faster, files a little bigger for the same quality")
        self.gpu.toggled.connect(lambda v: self._out_set(gpu=v))
        l.addWidget(self.gpu)
        self.lossless = QCheckBox("No re-encoding when possible")
        self.lossless.setToolTip("Only trims, joins of matching videos, turns and flips. Cuts then start on the "
                                 "nearest keyframe before your start.")
        self.lossless.toggled.connect(lambda v: self._out_set(lossless_when_possible=v))
        l.addWidget(self.lossless)
        self.plan_lbl = label("", "muted", wrap=True)
        l.addWidget(self.plan_lbl)
        b = button("Export", "export", "primary")
        b.clicked.connect(self.export)
        l.addWidget(b)

    # =========================================================================================================
    # opening and saving
    # =========================================================================================================
    def open_dialog(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Open video", "", "Videos (*.mp4 *.mov *.mkv *.webm *.avi *.m4v "
                                                "*.wmv *.flv *.ts *.mts *.m2ts *.3gp *.gif);;All files (*)")
        if files:
            self.open_files(files)

    def add_dialog(self) -> None:
        if self.p is None:
            return self.open_dialog()
        files, _ = QFileDialog.getOpenFileNames(self, "Add video to join", str(Path(self.p.clips[0].path).parent),
                                                "Videos (*.mp4 *.mov *.mkv *.webm *.avi *.m4v *.wmv *.gif);;All files (*)")
        if files:
            self.add_files(files)

    def open_files(self, files: list[str]) -> None:
        """Start editing these videos (joined in this order), or pick up a saved edit of the first one."""
        files = [f for f in files if Path(f).is_file()]
        if not files:
            return
        self.stop()
        key = str(Path(files[0])).casefold()
        saved = get_db().load_edit(key)
        p = None
        if saved and len(files) == 1:
            try:
                p = Project.from_json(saved)
                missing = [c for c in p.clips if not Path(c.path).exists()]
                if missing:
                    hub.toast.emit(f"{len(missing)} video{'s' if len(missing) != 1 else ''} of the saved edit "
                                   "are missing; they were left out.", "warn")
                    p.clips = [c for c in p.clips if Path(c.path).exists()]
                if not p.clips:
                    p = None
                else:
                    hub.toast.emit("Picked up where you left off.", "info")
            except Exception as e:  # noqa: BLE001
                log.warning("saved edit couldn't be read: %s", e)
                p = None
        if p is None:
            p = Project()
            for f in files:
                try:
                    p.clips.append(clip_from_file(f))
                except Exception as e:  # noqa: BLE001
                    hub.toast.emit(f"{Path(f).name} can't be edited: {e}", "warn")
            if not p.clips:
                return
        self.p = p
        self.key = key
        self.undo_stack.clear()
        self.redo_stack.clear()
        self._last_saved = p.to_json()
        self._frame_cache.clear()
        self.t = 0.0
        self.sel = None
        self._sync_all()
        self.seek(0.0)

    def add_files(self, files: list[str]) -> None:
        if self.p is None:
            return self.open_files(files)
        before = self.p.to_json()
        n = 0
        for f in files:
            try:
                self.p.clips.append(clip_from_file(f))
                n += 1
            except Exception as e:  # noqa: BLE001
                hub.toast.emit(f"{Path(f).name} can't be added: {e}", "warn")
        if n:
            self.changed(before=before, frames=True)
            hub.toast.emit(f"Added {n} video{'s' if n != 1 else ''} to the end of the edit.", "ok")

    def _save(self) -> None:
        if self.p is None or self.key is None:
            return
        js = self.p.to_json()
        if js != self._last_saved:
            get_db().save_edit(self.key, js, Path(self.p.clips[0].path).name if self.p.clips else "")
            self._last_saved = js

    def _recent_menu(self) -> None:
        m = QMenu(self)
        rows = get_db().recent_edits()
        if not rows:
            a = m.addAction("No saved edits yet")
            a.setEnabled(False)
        for r in rows:
            m.addAction(icon("film"), r["name"] or Path(r["key"]).name, lambda k=r["key"]: self._open_recent(k))
        m.exec(self.recent_btn.mapToGlobal(self.recent_btn.rect().bottomLeft()))

    def _open_recent(self, key: str) -> None:
        js = get_db().load_edit(key)
        if not js:
            return
        p = Project.from_json(js)
        if p.clips and Path(p.clips[0].path).exists():
            self.open_files([p.clips[0].path])
        else:
            hub.toast.emit("That video isn't there any more.", "warn")

    def _fill_empty_recent(self) -> None:
        from .widgets import clear_layout
        clear_layout(self.empty_recent)
        rows = [r for r in get_db().recent_edits(6)]
        if rows:
            self.empty_recent.addWidget(label("Carry on with", "cap"), 0, Qt.AlignHCenter)
        for r in rows:
            b = button(r["name"] or Path(r["key"]).name, "film", "ghost")
            b.clicked.connect(lambda _=False, k=r["key"]: self._open_recent(k))
            self.empty_recent.addWidget(b, 0, Qt.AlignHCenter)

    # drag and drop
    def dragEnterEvent(self, e):  # noqa: N802
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):  # noqa: N802
        files = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        vids = [f for f in files if Path(f).suffix.lower() in VIDEO_EXT]
        pics = [f for f in files if Path(f).suffix.lower() in ovl.KINDS - VIDEO_EXT]
        if vids:
            (self.add_files if self.p is not None else self.open_files)(vids)
        if pics and self.p is not None:
            folder = self._current_overlay_folder()
            for f in ovl.import_files(pics, folder):
                self.add_overlay(str(f))
            self._fill_overlay_library()

    # =========================================================================================================
    # changes, undo
    # =========================================================================================================
    def changed(self, before: str | None = None, frames: bool = False) -> None:
        """Something in the project changed (and should be undoable). frames=True: the pictures themselves change
        (trims, turns, colour), so frames are fetched again."""
        if self.p is None:
            return
        js = self.p.to_json()
        prev = before if before is not None else getattr(self, "_committed", None)
        if prev is not None and prev != js:
            self.undo_stack.append(prev)
            del self.undo_stack[:-150]
            self.redo_stack.clear()
        self._committed = js
        if frames:
            self._frame_cache.clear()
        if self.p.sound.normalize:
            self._measure_sound()          # trims or clips changed: the sound is measured again
        self._save_timer.start()
        self._sync_all(keep_panels=True)
        if frames:
            self._grab_timer.start()
        else:
            self._ops_timer.start()

    def _live(self) -> None:
        """While dragging: show it, don't record an undo step yet."""
        self._ops_timer.start()
        self.timeline.update()

    def _ops_changed(self) -> None:
        self.canvas.refresh_ops()

    def undo(self) -> None:
        if not self.undo_stack or self.p is None:
            return
        self.redo_stack.append(self.p.to_json())
        self._restore(self.undo_stack.pop())

    def redo(self) -> None:
        if not self.redo_stack or self.p is None:
            return
        self.undo_stack.append(self.p.to_json())
        self._restore(self.redo_stack.pop())

    def _restore(self, js: str) -> None:
        self.p = Project.from_json(js)
        self._committed = js
        self._frame_cache.clear()
        self._save_timer.start()
        self._sync_all()
        self.seek(min(self.t, self.p.duration))

    def key_undo(self) -> None:
        self.undo()

    # =========================================================================================================
    # syncing the controls to the project
    # =========================================================================================================
    def _sync_all(self, keep_panels: bool = False) -> None:
        p = self.p
        has = p is not None and bool(p.clips)
        self.stack.setCurrentIndex(1 if has else 0)
        for w in (self.add_btn, self.export_btn, self.undo_btn, self.redo_btn, self.reset_btn):
            w.setEnabled(has)
        self.undo_btn.setEnabled(has and bool(self.undo_stack))
        self.redo_btn.setEnabled(has and bool(self.redo_stack))
        if not has:
            self.name_lbl.setText("")
            self._fill_empty_recent()
            return
        if getattr(self, "_committed", None) is None:
            self._committed = p.to_json()
        W, H, fps = p.canvas()
        OW, OH = p.output_size()
        n = len(p.clips)
        self.name_lbl.setText(f"{Path(p.clips[0].path).name}" + (f" + {n - 1} more" if n > 1 else "") +
                              f" · {fmt_time(p.duration)} · {OW}×{OH} at {fps:g} fps")
        self.canvas.set_project(p)
        self.timeline.set_project(p)
        self.timeline_scroll.setMinimumHeight(min(170, self.timeline.minimumHeight() + 4))
        self.timeline.t = self.t
        self.timeline.selected = self.sel
        self.canvas.selected = self.sel
        self._fill_clips()
        if not hasattr(self, "_ov_filled"):
            self._ov_filled = True
            self._fill_overlay_library()
        self._sync_tool_pages()

    def _fill_clips(self) -> None:
        cur = max(0, self.clip_list.currentRow())
        self.clip_list.blockSignals(True)
        self.clip_list.clear()
        for i, c in enumerate(self.p.clips):
            it = QListWidgetItem(icon("film", C["acc"]), f"{Path(c.path).name}\n{fmt_time(c.length)}"
                                 + ("  · trimmed" if c.length < c.duration - 0.05 else ""))
            it.setData(Qt.UserRole, i)
            it.setToolTip(c.path)
            self.clip_list.addItem(it)
        self.clip_list.setCurrentRow(min(cur, self.clip_list.count() - 1))
        self.clip_list.blockSignals(False)

    def _clip_i(self) -> int:
        if self.p is None or not self.p.clips:
            return -1
        r = self.clip_list.currentRow()
        return r if 0 <= r < len(self.p.clips) else 0

    def _sync_tool_pages(self) -> None:
        p = self.p
        i = self._clip_i()
        if i >= 0:
            c = p.clips[i]
            self.clip_name.setText(Path(c.path).name)
            for w, v in ((self.flip_h, c.flip_h), (self.flip_v, c.flip_v), (self.mute, c.mute)):
                w.blockSignals(True)
                w.setChecked(v)
                w.blockSignals(False)
            self.angle.blockSignals(True)
            self.angle.setValue(int(round(c.angle * 10)))
            self.angle.blockSignals(False)
            self.angle_lbl.setText(f"{c.angle:+.1f}°" if c.angle else "0°")
            self.in_edit.setText(fmt_precise(c.start))
            self.out_edit.setText(fmt_precise(c.out))
            self.clip_len.setText(f"Uses {fmt_precise(c.length)} of {fmt_precise(c.duration)}")
            self.clip_info.setText(f"{c.width}×{c.height}, {c.fps:g} fps" + ("" if c.has_audio else ", no sound") +
                                   (f", turned {c.rotate}°" if c.rotate else ""))
            if getattr(self, "_trim_loaded", None) != (c.path, c.duration):
                self._trim_loaded = (c.path, c.duration)
                self.trim.set_audio(None, c.duration)
                if c.has_audio:
                    key = f"edit-clip-{abs(hash(c.path))}-{int(c.duration * 1000)}"
                    workers.run_bg(compute_peaks, c.path, key, int(min(8000, max(800, c.duration * 20))),
                                   done=lambda pk, path=c.path: self.trim.set_audio(pk, c.duration)
                                   if self.p and self._trim_loaded[0] == path else None)
            self.trim.set_range(c.start, c.out)
        # crop
        cr = p.crop
        for w, v in ((self.crop_on, cr.enabled), (self.keep_shape, cr.keep_shape)):
            w.blockSignals(True)
            w.setChecked(v)
            w.blockSignals(False)
        self.aspect.setCurrentIndex(max(0, self.aspect.findData(cr.aspect if cr.aspect != "free" else "source")))
        self.trans.blockSignals(True)
        self.trans.setValue(int(round(cr.transition * 10)))
        self.trans.blockSignals(False)
        self.trans_lbl.setText(f"{cr.transition:.1f} s" if cr.transition else "a cut")
        self.fill.set_value(cr.fill)
        self.fill_col.setStyleSheet(f"QPushButton {{ background: {cr.fill_colour}; border: 1px solid {C['line2']}; }}")
        self.fill_col.setEnabled(cr.fill == "colour")
        self.crop_section.setEnabled(self.mark is not None and cr.enabled)
        OW, OH = p.output_size()
        moves = sum(1 for k in cr.keys[1:] if not (len(k) > 5 and k[5] == "l"))
        tracked = sum(1 for k in cr.keys if len(k) > 5 and k[5] == "l")
        self.crop_info.setText((f"Finished size {OW}×{OH}" + (f" · {moves} move{'s' if moves != 1 else ''}" if moves else "")
                                + (" · follows a subject" if tracked else "")) if cr.enabled
                               else "Off: the whole picture is used.")
        # blur
        b = self._sel_blur()
        self.blur_box.setVisible(b is not None)
        self.blur_del.setEnabled(b is not None)
        if b is not None:
            self.blur_kind.set_value(b.kind)
            self.blur_shape.set_value(b.shape)
            self.blur_strength.blockSignals(True)
            self.blur_strength.setValue(b.strength)
            self.blur_strength.blockSignals(False)
            self.blur_space.set_value(b.space)
            self.blur_moving.blockSignals(True)
            self.blur_moving.setChecked(b.moving or len(b.keys) > 1)
            self.blur_moving.blockSignals(False)
            self.blur_when.setText(f"On from {fmt_precise(b.start)} to {fmt_precise(p.blur_end(b))}")
        # overlay
        o = self._sel_overlay()
        self.ov_box.setVisible(o is not None)
        self.ov_del.setEnabled(o is not None)
        if o is not None:
            self.ov_name.setText(Path(o.path).name)
            self.ov_opacity.blockSignals(True)
            self.ov_opacity.setValue(int(o.opacity * 100))
            self.ov_opacity.blockSignals(False)
            self.overlay_when.setText(f"On from {fmt_precise(o.start)} to {fmt_precise(p.overlay_end(o))}")
        # colour
        for k, (s, scale, val) in self.colour_sliders.items():
            v = getattr(p.colour, k)
            s.blockSignals(True)
            s.setValue(int(round(v * scale)))
            s.blockSignals(False)
            val.setText(f"{v:.2f}")
        # sound
        snd = p.sound
        self.norm.blockSignals(True)
        self.norm.setChecked(snd.normalize)
        self.norm.blockSignals(False)
        self.norm_lbl.setText(self._norm_text())
        self.vol.blockSignals(True)
        self.vol.setValue(int(round(snd.gain_db * 2)))
        self.vol.blockSignals(False)
        self.vol_lbl.setText(f"{snd.gain_db:+.1f} dB" if snd.gain_db else "0 dB")
        sec = self._sel_section()
        self.sec_box.setVisible(sec is not None)
        if sec is not None:
            self.sec_gain.blockSignals(True)
            self.sec_gain.setValue(int(round(sec["gain_db"] * 2)))
            self.sec_gain.blockSignals(False)
            self.sec_lbl.setText(f"{sec['gain_db']:+.1f} dB")
            self.sec_when.setText(f"From {fmt_precise(sec['start'])} to {fmt_precise(sec['end'])}")
        # export
        self.out_name.setText(p.output.name or p.default_output_path().name)
        self.out_where.setText(f"Saved next to {Path(p.clips[0].path).name}, in {Path(p.clips[0].path).parent}. "
                               "An existing file is never replaced.")
        self.quality.setCurrentIndex(max(0, self.quality.findData(p.output.quality)))
        for w, v in ((self.gpu, p.output.gpu), (self.lossless, p.output.lossless_when_possible)):
            w.blockSignals(True)
            w.setChecked(v)
            w.blockSignals(False)
        self.plan_lbl.setText(self._plan_text())

    def _plan_text(self) -> str:
        p = self.p
        if p.is_empty_edit():
            return "Nothing is changed yet: the export would be a copy of the video."
        if p.output.lossless_when_possible and not p.frame_effects():
            if len(p.clips) == 1:
                return "Only trims or turns: the video is copied without re-encoding (no quality lost)."
            return ("Joining: if the videos match (same size, frame rate and format) they're joined without "
                    "re-encoding; otherwise they're re-encoded to match.")
        W, H, fps = p.canvas()
        OW, OH = p.output_size()
        return f"Re-encoded at {OW}×{OH}, {fps:g} fps, so the crop, blur, overlays or colour can be added."

    # =========================================================================================================
    # playhead and frames
    # =========================================================================================================
    def preview_size(self) -> tuple[int, int]:
        W, H, _ = self.p.canvas()
        dpr = self.devicePixelRatioF()
        k = min(1.0, max(320, self.canvas.width() * dpr) / W, max(200, self.canvas.height() * dpr) / H)
        return max(2, int(W * k) // 2 * 2), max(2, int(H * k) // 2 * 2)

    def seek(self, t: float) -> None:
        if self.p is None:
            return
        self.t = max(0.0, min(t, max(0.0, self.p.duration - 1e-3)))
        if self.preview.playing:
            self.preview.start(self.p, self.t, self.preview_size())
        self._show_time()
        self._grab_timer.start()

    def _show_time(self) -> None:
        self.timeline.t = self.t
        self.timeline.update()
        self.time_lbl.setText(f"{fmt_precise(self.t)} / {fmt_time(self.p.duration)}")

    def _grab(self) -> None:
        if self.p is None or not self.p.clips:
            return
        size = self.preview_size()
        _w, _h, fps = self.p.canvas()
        tq = round(self.t * fps) / fps
        key = (tq, size)
        hit = self._frame_cache.get(key)
        if hit is not None:
            self.canvas.set_frame(hit, self.t)
            return
        self._grab_token += 1
        tok = self._grab_token
        pc = self.p.copy()
        t = self.t

        def done(img, tok=tok, key=key, t=t):
            if img is None or tok != self._grab_token:
                return
            if len(self._frame_cache) > 120:
                self._frame_cache.clear()
            self._frame_cache[key] = img
            self.canvas.set_frame(img, t)

        workers.run_bg(R.grab, pc, t, size[0], size[1], done=done,
                       failed=lambda m: setattr(self.canvas, "message", f"Couldn't show this frame: {m}"))

    def _play_time(self, t: float) -> None:
        self.t = t
        self._show_time()

    def _play_end(self) -> None:
        self._playing_ui(False)
        self._update_view()

    def key_play_pause(self) -> None:
        if self.p is None:
            return
        if self.preview.playing:
            self.stop()
            self._grab_timer.start()
        else:
            self.canvas.set_view("result")
            self.preview.start(self.p, self.t, self.preview_size())
            self._playing_ui(True)

    def _playing_ui(self, on: bool) -> None:
        self.play_btn.setIcon(icon("pause" if on else "play", C["on_acc"], 15))

    def stop(self) -> None:
        if self.preview.playing:
            self.preview.stop()
            self._playing_ui(False)
            self._update_view()

    def _step(self, frames: int) -> None:
        if self.p is None:
            return
        _w, _h, fps = self.p.canvas()
        self.stop()
        self.seek(self.t + frames / fps)

    def key_prev(self) -> None:
        self._step(-1)

    def key_next(self) -> None:
        self._step(1)

    def key_first(self) -> None:
        self.stop()
        self.seek(0.0)

    def key_last(self) -> None:
        if self.p:
            self.stop()
            self.seek(self.p.duration)

    def key_volume_up(self) -> None:
        self.audio.set_volume(min(100, self.audio._volume + 5))

    def key_volume_down(self) -> None:
        self.audio.set_volume(max(0, self.audio._volume - 5))

    def key_mute(self) -> None:
        self.audio.set_volume(0 if self.audio._volume else 80)

    def _update_view(self) -> None:
        """Cropping shows the whole picture (to move the box); everything else shows the finished frame, unless
        Whole picture is ticked. Playing always shows the finished frame."""
        if self.preview.playing:
            view = "result"
        elif self.tool == "crop" or self.whole_box.isChecked():
            view = "whole"
        else:
            view = "result"
        self.whole_box.setEnabled(self.tool != "crop")
        self.canvas.set_view(view)

    # =========================================================================================================
    # tools
    # =========================================================================================================
    def set_tool(self, tool: str) -> None:
        self.tool_seg.set_value(tool) if tool in dict(TOOLS[:3]) else self.tool_seg.set_value(None)
        self.tool_seg2.set_value(tool) if tool in dict(TOOLS[3:]) else self.tool_seg2.set_value(None)
        self.tools.setCurrentWidget(self.pages[tool])
        self.tool = tool
        self.canvas.tool = tool
        self._update_view()
        if tool == "crop" and self.p is not None and not self.p.crop.enabled:
            self.crop_on.setChecked(True)
        if tool == "overlay":
            self.left_seg.set_value("library")
            self.left_stack.setCurrentIndex(1)
        elif tool == "clip":
            self.left_seg.set_value("clips")
            self.left_stack.setCurrentIndex(0)
        self.canvas.update()

    def _picked(self, kind: str, iid: str) -> None:
        # Picking something only selects it: the tool you're in stays until you choose another tab.
        if kind == "clip":
            self.clip_list.setCurrentRow(int(iid))
            return
        self.sel = (kind, iid) if kind else None
        self.canvas.selected = self.sel
        self.timeline.selected = self.sel
        self._sync_tool_pages()
        self.canvas.update()
        self.timeline.update()

    def _sel_blur(self) -> Blur | None:
        if self.p is None or not self.sel or self.sel[0] != "blur":
            return None
        return next((b for b in self.p.blurs if b.id == self.sel[1]), None)

    def _sel_overlay(self) -> Overlay | None:
        if self.p is None or not self.sel or self.sel[0] != "overlay":
            return None
        return next((o for o in self.p.overlays if o.id == self.sel[1]), None)

    # ---- boxes on the picture --------------------------------------------------------------------------------
    def _native(self, box, frm: str, to: str):
        if frm == to:
            return box
        return self.p.canvas_to_output(box, self.t) if to == "output" else self.p.output_to_canvas(box, self.t)

    def _box_dragged(self, kind: str, iid: str, box, space: str) -> None:
        p = self.p
        if kind == "crop":
            set_key(p.crop.keys, self.t, box)
        elif kind == "blur":
            b = next((x for x in p.blurs if x.id == iid), None)
            if b is not None:
                nb = self._native(box, space, b.space)
                if b.moving or len(b.keys) > 1:
                    set_key(b.keys, self.t, nb)
                else:                          # a still box: it just moves
                    b.keys[0][1:5] = list(nb)
        elif kind == "overlay":
            o = next((x for x in p.overlays if x.id == iid), None)
            if o is not None:
                x, y, w, _h = self._native(box, space, "output")
                o.x, o.y, o.w = x, y, max(0.01, w)
        self._live()
        self.canvas.update()

    def _blur_drawn(self, box, space: str = "output") -> None:
        if self.p is None:
            return
        before = self.p.to_json()
        b = Blur(start=0.0, end=0.0, space=space, keys=[[0.0] + list(box) + ["h"]])
        self.p.blurs.append(b)
        self.sel = ("blur", b.id)
        self.changed(before=before)

    def _blur_space(self, space: str) -> None:
        """Change where a blur box lives, keeping it where it is on screen at each of its keys."""
        b = self._sel_blur()
        if b is None or b.space == space:
            return
        before = self.p.to_json()
        conv = self.p.canvas_to_output if space == "output" else self.p.output_to_canvas
        b.keys = [[k[0]] + list(conv(k[1:5], k[0])) + ([k[5]] if len(k) > 5 else []) for k in b.keys]
        b.space = space
        self.changed(before=before)

    def _crop_toggled(self, on: bool) -> None:
        if self.p is None:
            return
        before = self.p.to_json()
        self.p.crop.enabled = on
        self.changed(before=before)

    def _aspect_changed(self) -> None:
        before = self.p.to_json()
        self.p.crop.aspect = self.aspect.currentData()
        self._fit_crop_aspect()
        self.changed(before=before)

    def _fit_crop_aspect(self) -> None:
        """Reshape every crop box to the new finished shape, keeping its centre; the first one fills the video."""
        p = self.p
        a = p.aspect_value()
        W, H, _ = p.canvas()
        for n, k in enumerate(p.crop.keys):
            _t, x, y, w, h = k[:5]
            cx, cy = x + w / 2, y + h / 2
            hh = h if n else 1.0
            nw = hh * H * a / W
            if n == 0 and nw > 1:
                nw, hh = 1.0, W / a / H
            k[1:5] = [cx - nw / 2 if n else (1 - nw) / 2, cy - hh / 2 if n else (1 - hh) / 2, nw, hh]

    def _crop_set(self, **kw) -> None:
        if self.p is None:
            return
        before = self.p.to_json()
        for k, v in kw.items():
            setattr(self.p.crop, k, v)
        self.changed(before=before)

    def _trans_moved(self, v: int) -> None:
        self.p.crop.transition = v / 10
        self.trans_lbl.setText(f"{v / 10:.1f} s" if v else "a cut")
        self._live()

    def _pick_fill_colour(self) -> None:
        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import QColorDialog
        c = QColorDialog.getColor(QColor(self.p.crop.fill_colour), self, "Fill colour")
        if c.isValid():
            self._crop_set(fill_colour=c.name())

    def _delete_key(self, kind: str, iid: str) -> None:
        keys = self.p.crop.keys if kind == "crop" else next((b.keys for b in self.p.blurs if b.id == iid), None)
        if not keys or len(keys) < 2:
            hub.toast.emit("There's only one key: nothing to remove.", "info")
            return
        k = min(keys, key=lambda k: abs(k[0] - self.t))
        if k is keys[0]:
            hub.toast.emit("That's the starting box: move it instead.", "info")
            return
        before = self.p.to_json()
        keys.remove(k)
        self.changed(before=before)

    def _clear_keys(self, kind: str, iid: str) -> None:
        before = self.p.to_json()
        if kind == "crop":
            self.p.crop.keys = [[0.0] + list(self.p.crop_box(self.t)) + ["h"]]
        self.changed(before=before)

    def _keep_for_part(self, kind: str, iid: str) -> None:
        """Keep the box as it is at the playhead for the whole marked part (fixing a stretch tracking got wrong)."""
        if self.mark is None:
            hub.toast.emit("Mark the part first: drag along the times above the clips, or use Starts here and "
                           "Ends here.", "info")
            return
        a, b = self.mark
        p = self.p
        if kind == "crop":
            keys, box = p.crop.keys, p.crop_box(self.t)
        else:
            bl = next((x for x in p.blurs if x.id == iid), None)
            if bl is None:
                return
            keys, box = bl.keys, interp(bl.keys, self.t, p.crop.transition)
        before = p.to_json()
        apply_section(keys, a, b, box, p.crop.transition, p.duration)
        self.changed(before=before)
        hub.toast.emit(f"The box stays put from {fmt_precise(a)} to {fmt_precise(b)}.", "ok")

    def track(self, kind: str, iid: str) -> None:
        """Follow the subject inside the box at the playhead, as a job in the queue."""
        from ..edit.track import track
        p = self.p
        if p is None:
            return
        if kind == "crop":
            if not p.crop.enabled:
                hub.toast.emit("Turn the crop on and put its box around the subject first.", "info")
                return
            keys, t1 = p.crop.keys, p.duration
        else:
            b = next((x for x in p.blurs if x.id == iid), None)
            if b is None:
                return
            if b.space == "output":           # following works on the picture itself
                self._picked("blur", iid)
                self._blur_space("video")
                p = self.p
                b = next(x for x in p.blurs if x.id == iid)
            keys, t1 = b.keys, p.blur_end(b)
            b.moving = True
        if self.mark is not None and self.mark[0] - 0.05 <= self.t < self.mark[1]:
            t1 = min(t1, self.mark[1])        # only the marked part (to redo a stretch it got wrong)
        if t1 - self.t < 0.2:
            hub.toast.emit("Move the playhead back: there's nothing left to follow after it.", "info")
            return
        self.stop()
        snap = p.copy()
        t0 = self.t
        start_keys = [list(k) for k in keys]

        def done(res):
            if self.p is None:
                return
            before = self.p.to_json()
            if kind == "crop":
                self.p.crop.keys = res["keys"]
            else:
                bb = next((x for x in self.p.blurs if x.id == iid), None)
                if bb is None:
                    return
                bb.keys = res["keys"]
            self.changed(before=before)
            msg = f"Followed it for {fmt_time(res['until'] - t0)} ({res['frames']} frames, {res['tracker']})."
            if res["lost"] is not None:
                msg += f" It lost the subject at {fmt_precise(res['lost'])}: put the box back on it there and " \
                       "follow again."
            else:
                msg += " Where it's wrong: move the box there, mark the part, Keep this box for the marked part."
            hub.toast.emit(msg, "warn" if res["lost"] is not None else "ok")

        self.queue.add("Following a subject", track, snap, start_keys, t0, t1, pausable=True, on_done=done,
                       done_text=lambda res: f"followed {res['frames']} frames",
                       on_failed=lambda m: hub.toast.emit(f"Following failed: {m}", "error"))

    # ---- blur settings ------------------------------------------------------------------------------------------
    def _blur_set(self, commit: bool = True, **kw) -> None:
        b = self._sel_blur()
        if b is None:
            return
        before = self.p.to_json()
        for k, v in kw.items():
            setattr(b, k, v)
        if kw.get("moving") is False and len(b.keys) > 1:      # still again: where it is now, all the time
            b.keys = [[0.0] + list(interp(b.keys, self.t, self.p.crop.transition)) + ["h"]]
        if commit:
            self.changed(before=before)
        else:
            self._live()

    def _set_when(self, kind: str, which: str) -> None:
        it = self._sel_blur() if kind == "blur" else self._sel_overlay()
        if it is None:
            return
        before = self.p.to_json()
        end = it.end if it.end > 0 else self.p.duration
        if which == "a":
            it.start = min(self.t, end - 0.1)
        elif which == "b":
            it.end = max(self.t, it.start + 0.1)
        else:
            it.start, it.end = 0.0, 0.0
        self.changed(before=before)

    def _range_dragged(self, kind: str, iid: str, a: float, b: float) -> None:
        if kind == "sound":
            sec = next((x for x in self.p.sound.sections if x.get("id") == iid), None)
            if sec is not None:
                sec["start"], sec["end"] = a, b
                self._live()
            return
        it = next((x for x in (self.p.blurs if kind == "blur" else self.p.overlays) if x.id == iid), None)
        if it is None:
            return
        it.start = a
        it.end = 0.0 if b >= self.p.duration - 0.02 else b
        self._live()

    # ---- overlays -----------------------------------------------------------------------------------------------
    def add_overlay(self, path: str) -> None:
        if self.p is None or not path:
            if self.p is None:
                hub.toast.emit("Open a video first.", "info")
            return
        before = self.p.to_json()
        o = Overlay(path=path, start=0.0, end=0.0)
        self.p.overlays.append(o)
        self.sel = ("overlay", o.id)
        self.set_tool("overlay")
        self.changed(before=before)

    def _overlay_set(self, commit: bool = True, **kw) -> None:
        o = self._sel_overlay()
        if o is None:
            return
        before = self.p.to_json()
        for k, v in kw.items():
            setattr(o, k, v)
        if commit:
            self.changed(before=before)
        else:
            self._live()
            self.canvas.update()

    def delete_selected(self) -> None:
        if self.p is None or not self.sel:
            return
        before = self.p.to_json()
        kind, iid = self.sel
        if kind == "blur":
            self.p.blurs = [b for b in self.p.blurs if b.id != iid]
        elif kind == "overlay":
            self.p.overlays = [o for o in self.p.overlays if o.id != iid]
        elif kind == "sound":
            self.p.sound.sections = [x for x in self.p.sound.sections if x.get("id") != iid]
        else:
            return
        self.sel = None
        self.changed(before=before)

    def key_delete(self) -> None:
        self.delete_selected()

    # overlay library
    def _fill_overlay_library(self) -> None:
        cur = self._current_overlay_folder() if self.ov_tree.currentItem() else None
        self.ov_tree.blockSignals(True)
        self.ov_tree.clear()
        root = ovl.root()
        items = {}
        for f in ovl.folders():
            rel = f.relative_to(root)
            parent = items.get(rel.parent) if f != root else None
            it = QTreeWidgetItem([("Overlay library" if f == root else f.name)])
            it.setIcon(0, icon("folder", C["acc"] if f == root else C["sec"]))
            it.setData(0, Qt.UserRole, str(f))
            (parent.addChild(it) if parent is not None else self.ov_tree.addTopLevelItem(it))
            items[rel] = it
            if cur is not None and f == cur:
                self.ov_tree.setCurrentItem(it)
        self.ov_tree.expandAll()
        if self.ov_tree.currentItem() is None and self.ov_tree.topLevelItemCount():
            self.ov_tree.setCurrentItem(self.ov_tree.topLevelItem(0))
        self.ov_tree.blockSignals(False)
        self._fill_overlay_files()

    def _current_overlay_folder(self) -> Path:
        it = self.ov_tree.currentItem()
        return Path(it.data(0, Qt.UserRole)) if it else ovl.root()

    def _fill_overlay_files(self) -> None:
        self.ov_files.clear()
        folder = self._current_overlay_folder()
        files = ovl.files(folder)
        for f in files:
            it = QListWidgetItem(f.stem)
            it.setData(Qt.UserRole, str(f))
            it.setToolTip(f.name)
            it.setIcon(icon("film" if f.suffix.lower() in ovl.MOVING else "tag", C["faint"], 72))
            self.ov_files.addItem(it)
            workers.run_bg(ovl.thumbnail, f, 96, done=lambda pth, item=it: self._thumb(item, pth))
        if not files:
            it = QListWidgetItem("Empty: add files")
            it.setFlags(Qt.NoItemFlags)
            self.ov_files.addItem(it)

    def _thumb(self, item: QListWidgetItem, path) -> None:
        try:
            if path is not None:
                item.setIcon(QIcon(QPixmap(str(path))))
        except RuntimeError:
            pass

    def import_overlays(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Add to the overlay library", "",
                                                "Pictures, GIFs, videos (*.png *.jpg *.jpeg *.webp *.bmp *.gif *.mp4 "
                                                "*.mov *.webm *.mkv);;All files (*)")
        if not files:
            return
        got = ovl.import_files(files, self._current_overlay_folder())
        self._fill_overlay_files()
        hub.toast.emit(f"Added {len(got)} to the overlay library (copies; your files stay where they are).", "ok")

    def new_overlay_folder(self) -> None:
        name = dialogs.ask_text(self, "New folder", "Name:", ok="Make folder")
        if name:
            ovl.new_folder(self._current_overlay_folder(), name)
            self._fill_overlay_library()

    def _ov_folder_menu(self, pos) -> None:
        it = self.ov_tree.itemAt(pos)
        if it is None:
            return
        from .widgets import open_in_explorer
        path = it.data(0, Qt.UserRole)
        m = QMenu(self)
        m.addAction(icon("plus"), "New folder inside…", self.new_overlay_folder)
        m.addAction(icon("folder"), "Open folder", lambda: open_in_explorer(path))
        m.exec(self.ov_tree.viewport().mapToGlobal(pos))

    def _ov_file_menu(self, pos) -> None:
        it = self.ov_files.itemAt(pos)
        if it is None or not it.data(Qt.UserRole):
            return
        path = Path(it.data(Qt.UserRole))
        m = QMenu(self)
        m.addAction(icon("plus", C["acc"]), "Put on the video", lambda: self.add_overlay(str(path)))
        mv = m.addMenu(icon("move"), "Move to folder")
        for f in ovl.folders():
            if f != path.parent:
                mv.addAction(str(f.relative_to(ovl.root())) if f != ovl.root() else "Overlay library",
                             lambda dest=f: self._move_overlay(path, dest))

        def rm():
            if dialogs.ask(self, "Remove from the library?", f"{path.name} goes to the Recycle Bin.", "Remove",
                           danger=True):
                ovl.remove(path)
                self._fill_overlay_files()
        m.addAction(icon("trash", C["danger"]), "Remove from the library…", rm)
        m.exec(self.ov_files.viewport().mapToGlobal(pos))

    def _move_overlay(self, path: Path, dest: Path) -> None:
        from ..jobs.library_files import unique_path
        path.rename(unique_path(dest / path.name))
        self._fill_overlay_files()

    # ---- clips ---------------------------------------------------------------------------------------------------
    def _clip_selected(self, row: int) -> None:
        if self.p is None or row < 0:
            return
        self.timeline.selected = ("clip", str(row))
        self._sync_tool_pages()
        self.timeline.update()

    def _clips_reordered(self, *_a) -> None:
        order = [self.clip_list.item(i).data(Qt.UserRole) for i in range(self.clip_list.count())]
        before = self.p.to_json()
        self.p.clips = [self.p.clips[i] for i in order]
        self.changed(before=before, frames=True)

    def _clip_set(self, **kw) -> None:
        i = self._clip_i()
        if i < 0:
            return
        before = self.p.to_json()
        for k, v in kw.items():
            setattr(self.p.clips[i], k, v)
        self.changed(before=before, frames=True)

    def _turn(self, deg: int) -> None:
        i = self._clip_i()
        if i < 0:
            return
        self._clip_set(rotate=(self.p.clips[i].rotate + deg) % 360)

    def _angle_moved(self, v: int) -> None:
        i = self._clip_i()
        if i < 0:
            return
        self.p.clips[i].angle = v / 10
        self.angle_lbl.setText(f"{v / 10:+.1f}°" if v else "0°")
        self._frame_cache.clear()
        self._grab_timer.start()

    def _trim_dragged(self, a: float, b: float) -> None:
        i = self._clip_i()
        if i < 0:
            return
        c = self.p.clips[i]
        moved_start = abs(a - c.start) > 1e-6
        c.start, c.end = a, (0.0 if b >= c.duration - 0.02 else b)
        self.in_edit.setText(fmt_precise(c.start))
        self.out_edit.setText(fmt_precise(c.out))
        self._frame_cache.clear()
        # show the frame at the handle being moved
        off = self.p.clip_offset(i)
        self.t = off if moved_start else off + c.length - 0.04
        self._show_time()
        self._grab_timer.start()
        self.timeline.update()
        if not getattr(self, "_trim_commit", None):
            self._trim_commit = QTimer(self, singleShot=True, interval=500, timeout=lambda: self.changed(frames=True))
        self._trim_commit.start()

    def _trim_typed(self) -> None:
        i = self._clip_i()
        if i < 0:
            return
        c = self.p.clips[i]
        a, b = parse_time(self.in_edit.text()), parse_time(self.out_edit.text())
        a = c.start if a is None else max(0.0, min(a, c.duration - 0.1))
        b = c.out if b is None else max(a + 0.1, min(b, c.duration))
        before = self.p.to_json()
        c.start, c.end = a, (0.0 if b >= c.duration - 0.02 else b)
        self.changed(before=before, frames=True)

    def _seek_in_clip(self, src_t: float) -> None:
        i = self._clip_i()
        if i < 0:
            return
        c = self.p.clips[i]
        self.seek(self.p.clip_offset(i) + max(0.0, min(c.length, src_t - c.start)))

    def _set_trim_at_playhead(self, which: str) -> None:
        i, src = self.p.clip_at(self.t)
        c = self.p.clips[i]
        before = self.p.to_json()
        if which == "a":
            c.start = min(src, c.out - 0.1)
        else:
            c.end = max(src, c.start + 0.1)
        self.clip_list.setCurrentRow(i)
        self.changed(before=before, frames=True)

    def key_in(self) -> None:
        if self.p:
            self._set_trim_at_playhead("a")

    def key_out(self) -> None:
        if self.p:
            self._set_trim_at_playhead("b")

    def remove_clip(self) -> None:
        i = self._clip_i()
        if i < 0:
            return
        if len(self.p.clips) == 1:
            if dialogs.ask(self, "Close this edit?", "It stays saved: open the video again to carry on.", "Close"):
                self.close_project()
            return
        before = self.p.to_json()
        del self.p.clips[i]
        self.changed(before=before, frames=True)

    def close_project(self) -> None:
        self._save()
        self.stop()
        self.p = None
        self.key = None
        self._sync_all()

    # ---- marking a part -----------------------------------------------------------------------------------------
    def _set_mark(self, mark) -> None:
        self.mark = tuple(mark) if mark else None
        self.timeline.mark = self.mark
        self.timeline.update()
        self.mark_lbl.setText(f"{fmt_precise(self.mark[0])} – {fmt_precise(self.mark[1])}" if self.mark
                              else "none marked")
        self.mark_clear.setEnabled(self.mark is not None)
        if self.p is not None:
            self.crop_section.setEnabled(self.mark is not None and self.p.crop.enabled)

    def _mark_at(self, which: str) -> None:
        if self.p is None:
            return
        a, b = self.mark if self.mark else (0.0, self.p.duration)
        if which == "a":
            a = self.t
            b = b if b > a + 0.05 else self.p.duration
        else:
            b = self.t
            a = a if a < b - 0.05 else 0.0
        self._set_mark((a, b))

    # ---- sound --------------------------------------------------------------------------------------------------
    def _sel_section(self):
        if self.p is None or not self.sel or self.sel[0] != "sound":
            return None
        return next((x for x in self.p.sound.sections if x.get("id") == self.sel[1]), None)

    def _norm_text(self) -> str:
        snd = self.p.sound
        if not snd.normalize:
            return "Off: the sound stays as loud as it is."
        if len(snd.measured) >= 3 and snd.measured[0] == self.p.sound_signature():
            if snd.measured[1] is None:
                return "There's no sound to even out."
            g = R.norm_gain(self.p)
            return f"Measured {snd.measured[1]:.1f} LUFS: {g:+.1f} dB to match your songs."
        return "Measuring the sound…"

    def _norm_toggled(self, on: bool) -> None:
        before = self.p.to_json()
        self.p.sound.normalize = on
        self.changed(before=before)
        if on:
            self._measure_sound()

    def _measure_sound(self) -> None:
        p = self.p
        if p is None or not p.sound.normalize:
            return
        sig = p.sound_signature()
        if len(p.sound.measured) >= 3 and p.sound.measured[0] == sig:
            return
        snap = p.copy()

        def done(l, sig=sig):
            if self.p is None or self.p.sound_signature() != sig:
                return
            self.p.sound.measured = [sig, l.lufs if l else None, l.peak if l else None]
            self._committed = self.p.to_json()      # a measurement, not an edit: nothing to undo
            self._save_timer.start()
            self.norm_lbl.setText(self._norm_text())

        workers.run_bg(R.measure_sound, snap, done=done,
                       failed=lambda m: self.norm_lbl.setText(f"Couldn't measure the sound: {m}"))

    def _vol_moved(self, v: int) -> None:
        self.p.sound.gain_db = v / 2
        self.vol_lbl.setText(f"{v / 2:+.1f} dB" if v else "0 dB")

    def _add_section(self, gain: float) -> None:
        if self.p is None:
            return
        if self.mark is None:
            hub.toast.emit("Mark the part first: drag along the times above the clips, or use Starts here and "
                           "Ends here.", "info")
            return
        before = self.p.to_json()
        sec = Section(start=self.mark[0], end=self.mark[1], gain_db=gain)
        self.p.sound.sections.append(dict(sec.__dict__))
        self.sel = ("sound", sec.id)
        self.changed(before=before)

    def _sec_moved(self, v: int) -> None:
        sec = self._sel_section()
        if sec is not None:
            sec["gain_db"] = v / 2
            self.sec_lbl.setText(f"{v / 2:+.1f} dB")
            self.timeline.update()

    # ---- starting over --------------------------------------------------------------------------------------------
    def start_over(self) -> None:
        if self.p is None:
            return
        first = self.p.clips[0].path
        if not dialogs.ask(self, "Start over?", f"Every edit of {Path(first).name} is thrown away: trims, other videos "
                           "joined to it, crop, blur, overlays, colour and sound. Ctrl+Z brings it back while the "
                           "editor stays open.", "Start over", danger=True):
            return
        before = self.p.to_json()
        try:
            self.p = Project(clips=[clip_from_file(first)])
        except Exception as e:  # noqa: BLE001
            hub.toast.emit(f"Couldn't read {Path(first).name}: {e}", "error")
            return
        self.sel = None
        self._set_mark(None)
        self.changed(before=before, frames=True)

    # ---- colour / output -------------------------------------------------------------------------------------
    def _colour_moved(self, key: str, v: float) -> None:
        setattr(self.p.colour, key, v)
        self.colour_sliders[key][2].setText(f"{v:.2f}")
        self._frame_cache.clear()
        self._grab_timer.start()

    def _colour_reset(self) -> None:
        from ..edit.project import Colour
        before = self.p.to_json()
        self.p.colour = Colour()
        self.changed(before=before, frames=True)

    def _out_set(self, **kw) -> None:
        before = self.p.to_json()
        for k, v in kw.items():
            setattr(self.p.output, k, v)
        self.changed(before=before)

    def _out_name(self) -> None:
        name = self.out_name.text().strip()
        default = Path(self.p.clips[0].path).stem
        self._out_set(name="" if not name or name == self.p.default_output_path().name and name.startswith(default)
                      else name)

    # ---- export ----------------------------------------------------------------------------------------------
    def export(self) -> None:
        if self.p is None:
            return
        self._save()
        self.stop()
        snap = self.p.copy()
        name = snap.default_output_path().name

        def done(res):
            hub.toast.emit(f"Exported {Path(res['path']).name} · {res['how']}", "ok")

        self.queue.add(f"Exporting {name}", R.export, snap, pausable=True, on_done=done,
                       done_text=lambda res: f"{Path(res['path']).name}, {res['how']}",
                       on_failed=lambda m: hub.toast.emit(f"Export failed: {m}", "error"))

    # ---- page plumbing --------------------------------------------------------------------------------------------
    def theme_changed(self) -> None:
        self.canvas.update()
        self.timeline.update()

    def set_panels_hidden(self, hide: bool) -> None:
        for w in (self.left_w, self.right_w):
            w.setVisible(not hide)

    def hideEvent(self, e):  # noqa: N802
        self.stop()
        self._save()
        super().hideEvent(e)

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        if self.p is not None:
            self._frame_cache.clear()
            self._grab_timer.start()
