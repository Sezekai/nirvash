"""Auto-music: pick songs for silent videos, review, swap, export with the video untouched."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDoubleSpinBox, QFileDialog, QFrame, QGridLayout,
                               QHBoxLayout, QHeaderView, QProgressBar, QPushButton, QSizePolicy, QSlider, QSplitter,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..core.db import get_db
from ..core.settings import SHORT_SONG_MODES, get_settings
from ..jobs import automusic as am
from ..media import ff
from ..media.ff import fmt_time
from . import workers
from .hub import hub
from .player import Player
from .song_picker import SongPicker
from .theme import C, icon
from .widgets import (PlayerBar, Toggle, button, card, chip, clear_layout, cover_pixmap, label, open_in_explorer, tool)

STATUS_TXT = {"new": ("Not matched", C["faint"]), "matched": ("Matched", C["violet"]), "no_match": ("No song fits", C["amber"]),
              "exported": ("Exported", C["teal"]), "error": ("Error", C["red"])}


def find_songs_job(ctx, folder: str, recursive: bool, existing: list[am.AutoItem]) -> list[am.AutoItem]:
    by_path = {i.video: i for i in existing}
    vids = ff.list_videos(folder, recursive)
    suffix = get_settings().get("export.suffix", "_edited") or "_edited"
    vids = [v for v in vids if suffix not in v.stem and ".nirvash-part" not in v.name]
    items = []
    for i, v in enumerate(vids):
        ctx.check()
        ctx.progress(0.7 * i / max(1, len(vids)), f"Looking at {v.name}")
        it = by_path.get(str(v)) or am.AutoItem(str(v))
        try:
            am.analyse_video(it, ctx)
        except Exception as e:
            it.status = "error"
            it.message = f"Couldn't read the video: {e}"[:200]
        items.append(it)
    ok = [i for i in items if i.status != "error"]
    if ok:
        sub = am.JobContext(lambda f, m: ctx.progress(0.7 + 0.3 * f, m), lambda k, p: ctx.event(k, p))
        sub.cancel = ctx.cancel
        am.match_items(ok, sub)
    return items


def export_job(ctx, items: list[am.AutoItem]) -> list[am.AutoItem]:
    for n, it in enumerate(items):
        ctx.check()
        sub = am.JobContext(lambda f, m, n=n: ctx.progress((n + f) / len(items), m))
        sub.cancel = ctx.cancel
        try:
            am.export_item(it, sub)
        except InterruptedError:
            raise
        except Exception as e:
            it.status = "error"
            it.message = str(e)[:300]
        ctx.event("item_changed", it)
    return items


class AutoMusicPage(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("page")
        self.s = get_settings()
        self.folder, self.items = am.load_session()
        self.cur: am.AutoItem | None = None
        self.job: workers.Job | None = None
        self._preview_token = 0
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 14)
        root.setSpacing(12)

        head = QHBoxLayout()
        head.addWidget(label("Auto-music", "h1"))
        head.addSpacing(12)
        self.folder_lbl = label("", "muted")
        head.addWidget(self.folder_lbl, 1)
        pick = button("Choose folder", "folder")
        pick.clicked.connect(self.choose_folder)
        head.addWidget(pick)
        self.find_btn = button("Find songs", "sparkle", "primary")
        self.find_btn.clicked.connect(self.find_songs)
        head.addWidget(self.find_btn)
        root.addLayout(head)

        opts = QHBoxLayout()
        opts.setSpacing(8)
        self.t_rec = Toggle(bool(self.s.get("automusic.recursive", False)))
        self.t_rec.toggledOn.connect(lambda v: self.s.set("automusic.recursive", v))
        opts.addWidget(self.t_rec)
        opts.addWidget(label("Include subfolders"))
        opts.addSpacing(18)
        opts.addWidget(label("Prefer songs in category", "muted"))
        self.rule = QComboBox()
        self.rule.setMinimumWidth(170)
        self.rule.activated.connect(self._rule_changed)
        opts.addWidget(self.rule)
        opts.addStretch(1)
        self.prog = QProgressBar()
        self.prog.setRange(0, 1000)
        self.prog.setFixedWidth(200)
        self.prog.setVisible(False)
        opts.addWidget(self.prog)
        self.status = label("", "muted")
        opts.addWidget(self.status)
        self.stop_btn = button("Stop", "x")
        self.stop_btn.setVisible(False)
        self.stop_btn.clicked.connect(lambda: self.job and self.job.cancel())
        opts.addWidget(self.stop_btn)
        root.addLayout(opts)

        split = QSplitter(Qt.Horizontal)
        split.setHandleWidth(12)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(8)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Ready", "Video", "Length", "Song", "Status"])
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hh = self.table.horizontalHeader()
        for i, w in enumerate((54, 220, 64, 220, 110)):
            self.table.setColumnWidth(i, w)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.cellClicked.connect(self._cell_clicked)
        self.table.currentCellChanged.connect(lambda r, *_: self._select(r))
        ll.addWidget(self.table, 1)
        eb = QHBoxLayout()
        self.summary = label("", "muted")
        eb.addWidget(self.summary, 1)
        self.exp_ready = button("Export ready", "export")
        self.exp_ready.setToolTip("Export the videos ticked as Ready")
        self.exp_ready.clicked.connect(lambda: self.export([i for i in self.items if i.ready and i.song_id and i.status != "exported"]))
        eb.addWidget(self.exp_ready)
        self.exp_all = button("Export all", "export", "primary")
        self.exp_all.setToolTip("Export every matched video that hasn't been exported yet")
        self.exp_all.clicked.connect(lambda: self.export([i for i in self.items if i.song_id and i.status == "matched"]))
        eb.addWidget(self.exp_all)
        ll.addLayout(eb)
        split.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(10)
        self.player = Player(self, video=True)
        self.player.setMinimumHeight(220)
        self.player.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        rl.addWidget(self.player, 1)
        self.bar = PlayerBar(self.player, show_title=False)
        rl.addWidget(self.bar)
        self.song_card = card()
        self.song_card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self.song_lay = QVBoxLayout(self.song_card)
        self.song_lay.setContentsMargins(14, 12, 14, 12)
        self.song_lay.setSpacing(8)
        rl.addWidget(self.song_card)
        self.ctrl_card = card()
        self.ctrl_card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self._build_controls(self.ctrl_card)
        rl.addWidget(self.ctrl_card)
        split.addWidget(right)
        split.setSizes([560, 640])
        root.addWidget(split, 1)

        self._preview_timer = QTimer(self, singleShot=True, interval=350, timeout=self._render_preview)
        sc = QShortcut(QKeySequence("Space"), self.table)
        sc.setContext(Qt.WidgetWithChildrenShortcut)
        sc.activated.connect(self.player.toggle)
        sc2 = QShortcut(QKeySequence("R"), self.table)
        sc2.setContext(Qt.WidgetWithChildrenShortcut)
        sc2.activated.connect(self._toggle_ready_current)
        hub.categoriesChanged.connect(self._fill_rules)
        self._fill_rules()
        self._fill_table()
        self._update_folder_lbl()
        self._show_item(None)

    # ---- controls -------------------------------------------------------------
    def _build_controls(self, box: QFrame) -> None:
        g = QGridLayout(box)
        g.setContentsMargins(14, 12, 14, 12)
        g.setHorizontalSpacing(10)
        g.setVerticalSpacing(8)
        g.addWidget(label("Start the song at", "muted"), 0, 0)
        self.start_slider = QSlider(Qt.Horizontal)
        self.start_slider.setRange(0, 1000)
        self.start_slider.sliderReleased.connect(lambda: self._set_offset(self.start_slider.value() / 10.0))
        g.addWidget(self.start_slider, 0, 1, 1, 2)
        self.start_spin = QDoubleSpinBox()
        self.start_spin.setDecimals(1)
        self.start_spin.setSuffix(" s")
        self.start_spin.setRange(0, 99999)
        self.start_spin.setFixedWidth(90)
        self.start_spin.editingFinished.connect(lambda: self._set_offset(self.start_spin.value()))
        g.addWidget(self.start_spin, 0, 3)
        smart = button("Smart start", "sparkle", color=C["teal"])
        smart.setToolTip("Skip a quiet intro and start on a beat")
        smart.clicked.connect(self._smart_start)
        g.addWidget(smart, 0, 4)

        g.addWidget(label("Volume", "muted"), 1, 0)
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 150)
        self.vol.sliderReleased.connect(lambda: self._set("volume", self.vol.value() / 100.0))
        g.addWidget(self.vol, 1, 1, 1, 2)
        self.vol_lbl = label("100%", "muted")
        g.addWidget(self.vol_lbl, 1, 3)
        self.vol.valueChanged.connect(lambda v: self.vol_lbl.setText(f"{v}%"))

        g.addWidget(label("Fade in / out", "muted"), 2, 0)
        fades = QHBoxLayout()
        self.fin = QDoubleSpinBox()
        self.fout = QDoubleSpinBox()
        for sp, key in ((self.fin, "fade_in"), (self.fout, "fade_out")):
            sp.setRange(0, 20)
            sp.setDecimals(1)
            sp.setSuffix(" s")
            sp.setFixedWidth(80)
            sp.editingFinished.connect(lambda k=key, w=sp: self._set(k, w.value()))
            fades.addWidget(sp)
        fades.addStretch(1)
        g.addLayout(fades, 2, 1, 1, 2)

        g.addWidget(label("If the song is shorter", "muted"), 3, 0)
        self.mode = QComboBox()
        self.mode.addItem(f"Default: {SHORT_SONG_MODES[self.s.get('automusic.short_mode', 'chain')]}", None)
        for k, v in SHORT_SONG_MODES.items():
            self.mode.addItem(v, k)
        self.mode.activated.connect(self._mode_changed)
        g.addWidget(self.mode, 3, 1, 1, 3)
        self.chain_lbl = label("", "muted", wrap=True)
        g.addWidget(self.chain_lbl, 4, 1, 1, 4)
        g.setColumnStretch(1, 1)
        self.badges = QHBoxLayout()
        g.addLayout(self.badges, 5, 0, 1, 5)
        row = QHBoxLayout()
        self.ready_btn = button("Ready", "check")
        self.ready_btn.setCheckable(True)
        self.ready_btn.setToolTip("Mark as checked and ready to export (R)")
        self.ready_btn.toggled.connect(self._ready_toggled)
        row.addWidget(self.ready_btn)
        row.addStretch(1)
        self.open_out = button("Show exported file", "folder")
        self.open_out.clicked.connect(lambda: self.cur and self.cur.output and open_in_explorer(self.cur.output))
        row.addWidget(self.open_out)
        self.exp_one = button("Export this video", "export")
        self.exp_one.clicked.connect(lambda: self.cur and self.export([self.cur]))
        row.addWidget(self.exp_one)
        g.addLayout(row, 6, 0, 1, 5)

    # ---- folder / rules ---------------------------------------------------------
    def _update_folder_lbl(self) -> None:
        self.folder_lbl.setText(self.folder or "Choose a folder of videos without sound")
        self.folder_lbl.setToolTip(self.folder)

    def choose_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Folder of videos without sound", self.folder or str(Path.home()))
        if not d:
            return
        d = str(Path(d))
        if d != self.folder:
            self.folder = d
            self.items = []
            self._fill_table()
            self._show_item(None)
        self._update_folder_lbl()
        self._fill_rules()
        self.find_songs()

    def _fill_rules(self) -> None:
        self.rule.blockSignals(True)
        self.rule.clear()
        self.rule.addItem("No preference", None)
        rules = get_db().folder_rules()
        cur = rules.get(self.folder) if self.folder else None
        for c in get_db().categories():
            self.rule.addItem(c["name"], c["id"])
            if c["id"] == cur:
                self.rule.setCurrentIndex(self.rule.count() - 1)
        self.rule.setEnabled(bool(self.folder))
        self.rule.blockSignals(False)

    def _rule_changed(self) -> None:
        if self.folder:
            get_db().set_folder_rule(self.folder, self.rule.currentData())
            if self.items:
                hub.toast.emit("Press Find songs to re-match with this preference.", "info")

    # ---- jobs -------------------------------------------------------------------
    def _busy(self, on: bool, text: str = "") -> None:
        self.prog.setVisible(on)
        self.stop_btn.setVisible(on)
        self.find_btn.setEnabled(not on)
        self.exp_all.setEnabled(not on)
        self.exp_ready.setEnabled(not on)
        self.exp_one.setEnabled(not on)
        self.status.setText(text)
        if on:
            self.prog.setValue(0)

    def find_songs(self) -> None:
        if not self.folder or not Path(self.folder).exists():
            self.choose_folder() if not self.folder else hub.toast.emit("That folder no longer exists.", "warn")
            return
        if self.job:
            return
        self.player.unload()
        self.job = workers.Job(find_songs_job, self.folder, bool(self.s.get("automusic.recursive")), self.items)
        self.job.sig.progress.connect(lambda f, m: (self.prog.setValue(int(f * 1000)), self.status.setText(m)))
        self.job.sig.event.connect(self._event)
        self.job.sig.done.connect(self._found)
        self.job.sig.failed.connect(lambda e: (hub.toast.emit(e, "error"), self._job_done()))
        self.job.sig.cancelled.connect(self._job_done)
        self._busy(True, "Looking at the videos…")
        workers.start(self.job)

    def _found(self, items) -> None:
        self.items = items
        self._save()
        self._fill_table()
        self._job_done()
        n = sum(1 for i in items if i.song_id)
        skipped = sum(1 for i in items if i.has_sound and not i.song_id)
        msg = f"Picked songs for {n} video{'s' if n != 1 else ''}."
        if skipped:
            msg += f" {skipped} already have sound and were skipped."
        hub.toast.emit(msg, "ok")
        if self.table.rowCount():
            self.table.selectRow(0)

    def _job_done(self) -> None:
        self.job = None
        self._busy(False)
        self._update_summary()

    def _event(self, kind: str, payload) -> None:
        if kind == "warning":
            hub.toast.emit(str(payload), "warn")
        elif kind == "item_changed":
            self._refresh_row(payload)

    def export(self, items: list[am.AutoItem]) -> None:
        items = [i for i in items if i.song_id is not None]
        if not items:
            hub.toast.emit("Nothing to export yet.", "info")
            return
        if self.job:
            return
        self.player.pause()
        self.job = workers.Job(export_job, items)
        self.job.sig.progress.connect(lambda f, m: (self.prog.setValue(int(f * 1000)), self.status.setText(f"Exporting {m}")))
        self.job.sig.event.connect(self._event)
        self.job.sig.done.connect(lambda res: self._exported(res))
        self.job.sig.failed.connect(lambda e: (hub.toast.emit(e, "error"), self._job_done()))
        self.job.sig.cancelled.connect(lambda: (self._save(), self._job_done()))
        self._busy(True, "Exporting…")
        workers.start(self.job)

    def _exported(self, res) -> None:
        self._save()
        self._job_done()
        ok = sum(1 for i in res if i.status == "exported")
        bad = len(res) - ok
        hub.toast.emit(f"Exported {ok} video{'s' if ok != 1 else ''} next to the originals."
                       + (f" {bad} failed — see their status." if bad else ""), "warn" if bad else "ok")
        if self.cur:
            self._show_item(self.cur)

    # ---- table ------------------------------------------------------------------
    def _song_title(self, sid: int | None) -> str:
        if sid is None:
            return ""
        r = get_db().song(sid)
        if r is None:
            return "(deleted song)"
        t = r["title"] or Path(r["file_path"]).stem
        return f"{t} — {r['artist']}" if r["artist"] else t

    def _fill_table(self) -> None:
        self.table.setRowCount(len(self.items))
        for i, it in enumerate(self.items):
            self._fill_row(i, it)
        self._update_summary()

    def _fill_row(self, i: int, it: am.AutoItem) -> None:
        rd = QTableWidgetItem()
        rd.setIcon(icon("check", C["teal"], 16) if it.ready else icon("check", C["surface3"], 16))
        rd.setToolTip("Click to mark ready")
        self.table.setItem(i, 0, rd)
        name = QTableWidgetItem(Path(it.video).name)
        name.setToolTip(it.video)
        self.table.setItem(i, 1, name)
        self.table.setItem(i, 2, QTableWidgetItem(fmt_time(it.duration)))
        if it.has_sound and it.song_id is None:
            song = QTableWidgetItem("Has sound — skipped")
            song.setForeground(QColor(C["faint"]))
        else:
            song = QTableWidgetItem(self._song_title(it.song_id))
            if it.manual:
                song.setIcon(icon("edit", C["muted"], 14))
        self.table.setItem(i, 3, song)
        txt, col = STATUS_TXT.get(it.status, (it.status, C["muted"]))
        st = QTableWidgetItem(txt)
        st.setForeground(QColor(col))
        if it.message:
            st.setToolTip(it.message)
        self.table.setItem(i, 4, st)

    def _refresh_row(self, it: am.AutoItem) -> None:
        for i, x in enumerate(self.items):
            if x is it or x.video == it.video:
                self.items[i] = it
                self._fill_row(i, it)
                break
        self._update_summary()

    def _update_summary(self) -> None:
        n = len(self.items)
        m = sum(1 for i in self.items if i.song_id is not None)
        r = sum(1 for i in self.items if i.ready)
        e = sum(1 for i in self.items if i.status == "exported")
        self.summary.setText(f"{n} videos · {m} with songs · {r} ready · {e} exported" if n else
                             "Choose a folder and press Find songs.")

    def _cell_clicked(self, row: int, col: int) -> None:
        if col == 0 and 0 <= row < len(self.items):
            it = self.items[row]
            it.ready = not it.ready
            self._fill_row(row, it)
            if it is self.cur:
                self.ready_btn.blockSignals(True)
                self.ready_btn.setChecked(it.ready)
                self.ready_btn.blockSignals(False)
            self._save()
            self._update_summary()

    def _toggle_ready_current(self) -> None:
        r = self.table.currentRow()
        if r >= 0:
            self._cell_clicked(r, 0)

    def _select(self, row: int) -> None:
        self._show_item(self.items[row] if 0 <= row < len(self.items) else None)

    # ---- detail -------------------------------------------------------------------
    def _show_item(self, it: am.AutoItem | None) -> None:
        self.cur = it
        clear_layout(self.song_lay)
        self.ctrl_card.setEnabled(it is not None and it.song_id is not None)
        clear_layout(self.badges)
        if it is None:
            self.player.unload()
            self.song_lay.addWidget(label("Pick a video on the left to watch it with its song.", "muted"))
            return
        db = get_db()
        r = db.song(it.song_id) if it.song_id is not None else None
        head = QHBoxLayout()
        cov = label()
        if r is not None:
            cov.setPixmap(cover_pixmap(str(self.s.cache("covers") / f"{r['id']}.jpg"), 58, r["title"] or r["file_path"], 9))
            head.addWidget(cov)
        col = QVBoxLayout()
        col.setSpacing(2)
        if r is None:
            col.addWidget(label("No song yet" if not it.has_sound else "This video already has sound", "h2"))
            col.addWidget(label(it.message or "Press Find songs, or choose one from the library.", "muted", wrap=True))
        else:
            col.addWidget(label(r["title"] or Path(r["file_path"]).stem, "h2", wrap=True))
            sub = " · ".join(x for x in (r["artist"], fmt_time(r["duration"]),
                                         f"{r['bpm']:.0f} BPM" if r["bpm"] else "") if x)
            col.addWidget(label(sub, "muted"))
        head.addLayout(col, 1)
        pick = button("Choose from library", "library")
        pick.clicked.connect(self.choose_song)
        head.addWidget(pick, 0, Qt.AlignTop)
        self.song_lay.addLayout(head)
        if r is not None:
            sc = am.score_song(it, next((s for s in am.load_songs() if s.id == r["id"]), None) or am.SongInfo(
                r["id"], r["file_path"], r["duration"], r["pace"], None, False, False, set(), 0),
                am.eff(it, "mode") if am.eff(it, "mode") != "long_only" else "trim_fade", 0, am.rule_category(it.video))
            if sc:
                parts = QHBoxLayout()
                parts.setSpacing(5)
                parts.addWidget(chip(f"Fit {max(0, sc[0]) * 100:.0f}", C["teal"], filled=True))
                for k, nm in (("length", "Length"), ("pace", "Pace"), ("mood", "Mood")):
                    parts.addWidget(chip(f"{nm} {sc[1][k] * 100:.0f}", C["violet"]))
                if not it.features.get("mood"):
                    parts.addWidget(chip("No mood (CLIP missing)", C["amber"]))
                parts.addStretch(1)
                self.song_lay.addLayout(parts)
        alts = [a for a in it.alts if a[0] != it.song_id][:5]
        if alts:
            self.song_lay.addWidget(label("Other good fits", "muted"))
            al = QHBoxLayout()
            al.setSpacing(6)
            for sid, sc in alts:
                t = self._song_title(sid)
                b = QPushButton(f"{t[:28]}{'…' if len(t) > 28 else ''}  {max(0, sc) * 100:.0f}")
                b.setObjectName("chip")
                b.setToolTip(t)
                b.setCursor(Qt.PointingHandCursor)
                b.clicked.connect(lambda _=False, s=sid: self._pick(s))
                al.addWidget(b)
            al.addStretch(1)
            self.song_lay.addLayout(al)
        if it.status == "exported" and it.output:
            self.song_lay.addWidget(label(f"Exported: {Path(it.output).name}", "muted"))
        if it.status == "error" and it.message:
            m = label(it.message, wrap=True)
            m.setStyleSheet(f"color:{C['red']};")
            self.song_lay.addWidget(m)
        self._sync_controls()
        self.open_out.setVisible(bool(it.output))
        self._schedule_preview(immediate=True)

    def _sync_controls(self) -> None:
        it = self.cur
        if it is None:
            return
        r = get_db().song(it.song_id) if it.song_id is not None else None
        L = float(r["duration"]) if r else 0.0
        for w in (self.start_slider, self.start_spin, self.vol, self.fin, self.fout, self.mode, self.ready_btn):
            w.blockSignals(True)
        self.start_slider.setRange(0, int(max(0, L - 1) * 10))
        self.start_slider.setValue(int(it.offset * 10))
        self.start_spin.setMaximum(max(0.0, L - 1))
        self.start_spin.setValue(it.offset)
        self.vol.setValue(int(round(am.eff(it, "volume") * 100)))
        self.vol_lbl.setText(f"{self.vol.value()}%")
        self.fin.setValue(am.eff(it, "fade_in"))
        self.fout.setValue(am.eff(it, "fade_out"))
        self.mode.setItemText(0, f"Default: {SHORT_SONG_MODES[self.s.get('automusic.short_mode', 'chain')]}")
        self.mode.setCurrentIndex(max(0, self.mode.findData(it.mode)) if it.mode else 0)
        self.ready_btn.setChecked(it.ready)
        for w in (self.start_slider, self.start_spin, self.vol, self.fin, self.fout, self.mode, self.ready_btn):
            w.blockSignals(False)
        # Chain / loop description
        txt = ""
        if r is not None and L - it.offset < it.duration:
            mode = am.eff(it, "mode")
            if mode == "chain":
                names = [self._song_title(c) for c in it.chain_ids]
                txt = "Song is shorter than the video. Then: " + (" → ".join(names) if names else "loops (no other songs)")
            elif mode == "loop":
                txt = "Song is shorter than the video, so it loops with a crossfade."
            else:
                txt = f"Song is shorter than the video; music ends at {fmt_time(L - it.offset)} with a fade-out."
        self.chain_lbl.setText(txt)
        self.chain_lbl.setVisible(bool(txt))
        clear_layout(self.badges)
        if r is not None:
            self.badges.addWidget(chip("Lossless video — copied untouched", C["teal"]))
            pieces, _cf = am.plan_pieces(it)
            if am.can_copy_audio(it, pieces):
                self.badges.addWidget(chip("Audio copied (if the format allows)", C["teal"]))
            else:
                ext = Path(it.video).suffix.lower()
                enc = {".webm": "Opus 256k", ".avi": "MP3 320k", ".ogv": "Vorbis", ".wmv": "WMA",
                       ".mpg": "MP2", ".mpeg": "MP2"}.get(ext, "AAC 320k")
                self.badges.addWidget(chip(f"Music mixed → {enc}", C["violet"]))
            self.badges.addStretch(1)

    def _schedule_preview(self, immediate: bool = False) -> None:
        if immediate:
            self._preview_timer.stop()
            self._render_preview()
        else:
            self._preview_timer.start()

    def _render_preview(self) -> None:
        it = self.cur
        if it is None:
            return
        self._preview_token += 1
        tok = self._preview_token
        pos = self.player.position() if self.player.duration() else 0.0
        playing = self.player.is_playing()
        if it.song_id is None:
            self.player.load(it.video, play=False)
            self.bar.wave.load(None, "", it.duration)
            return
        self.status.setText("Preparing preview…")

        def done(wav, it=it, tok=tok):
            if tok != self._preview_token or it is not self.cur:
                return
            self.status.setText("")
            self.player.load(it.video, audio_file=str(wav) if wav else None, play=playing, start=pos)
            if wav:
                self.bar.wave.load(str(wav), Path(str(wav)).stem, it.duration)
                self.bar.wave.set_position(pos)

        workers.run_bg(am.preview_audio, it, done=done,
                       failed=lambda e: (self.status.setText(""), hub.toast.emit(f"Preview failed: {e}", "error")))

    # ---- edits ---------------------------------------------------------------------
    def _changed(self) -> None:
        if self.cur and self.cur.status == "exported":
            self.cur.status = "matched"
        self._save()
        self._sync_controls()
        self._refresh_row(self.cur)
        self._schedule_preview()

    def _set_offset(self, v: float) -> None:
        if self.cur:
            self.cur.offset = round(max(0.0, v), 2)
            if am.eff(self.cur, "mode") == "chain":
                songs = am.load_songs()
                prim = next((s for s in songs if s.id == self.cur.song_id), None)
                if prim and prim.duration - self.cur.offset < self.cur.duration:
                    self.cur.chain_ids = am.pick_chain(self.cur, prim, songs, {})
            self._changed()

    def _smart_start(self) -> None:
        it = self.cur
        if not it or it.song_id is None:
            return
        songs = am.load_songs()
        sg = next((s for s in songs if s.id == it.song_id), None)
        if sg:
            workers.run_bg(am.choose_offset, it, sg, done=lambda off: self._set_offset(off))

    def _set(self, key: str, value) -> None:
        if self.cur:
            setattr(self.cur, key, value)
            self._changed()

    def _mode_changed(self) -> None:
        if not self.cur:
            return
        self.cur.mode = self.mode.currentData()
        songs = am.load_songs()
        prim = next((s for s in songs if s.id == self.cur.song_id), None)
        self.cur.chain_ids = am.pick_chain(self.cur, prim, songs, {}) if (
            prim and am.eff(self.cur, "mode") == "chain" and prim.duration - self.cur.offset < self.cur.duration) else []
        self._changed()

    def _ready_toggled(self, on: bool) -> None:
        if self.cur:
            self.cur.ready = on
            self._save()
            self._refresh_row(self.cur)

    def _pick(self, sid: int) -> None:
        it = self.cur
        if not it:
            return
        try:
            am.manual_pick(it, sid)
        except Exception as e:
            hub.toast.emit(str(e), "error")
            return
        self._save()
        self._refresh_row(it)
        self._show_item(it)

    def choose_song(self) -> None:
        if not self.cur:
            return
        self.player.pause()
        d = SongPicker(self.cur, self)
        if d.exec() and d.chosen is not None:
            self._pick(d.chosen)

    def _save(self) -> None:
        am.save_session(self.folder, self.items)

    def stop(self) -> None:
        self.player.pause()
