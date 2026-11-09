"""Settings: song lookups, auto-music and scan defaults, AI models, Storage, and Reset. Changes apply straight away."""
from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QLineEdit, QScrollArea, QSlider, QVBoxLayout, QWidget)

from ..analysis import encoders, models
from ..analysis.runtime import available_providers
from ..core import gpu
from ..core.db import get_db
from ..core.paths import collection_sorter_clip_dir, drive_ok
from ..core.settings import SHORT_SONG_MODES, get_settings
from ..jobs import scan as scanjob
from ..jobs import storage
from . import dialogs, workers
from .hub import hub
from .jobqueue import get_queue
from .theme import C
from .widgets import ElidedLabel, Toggle, button, chip, clear_layout, hline, label, open_in_explorer, page_header, panel


def _cols_dir(wide: bool):
    from PySide6.QtWidgets import QBoxLayout
    return QBoxLayout.LeftToRight if wide else QBoxLayout.TopToBottom


class SettingsPage(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("page")
        self.s = get_settings()
        self.queue = get_queue()
        self.dl_jobs: dict[str, object] = {}
        self.sizes: dict[str, tuple[int, int] | None] = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 12)
        outer.setSpacing(8)
        head, _ = page_header("Settings", "Changes apply straight away. Where Nirvash keeps things is under Storage")
        outer.addLayout(head)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        inner.setObjectName("transparent")
        self.scroll.setWidget(inner)
        self.root = QVBoxLayout(inner)
        self.root.setContentsMargins(0, 0, 4, 0)
        self.root.setSpacing(8)
        outer.addWidget(self.scroll, 1)
        self.storage_panel: QFrame | None = None
        self.build()
        self.queue.changed.connect(self._queue_changed)

    def build(self) -> None:
        clear_layout(self.root)
        from PySide6.QtWidgets import QBoxLayout
        cols = QBoxLayout(QBoxLayout.LeftToRight)
        cols.setSpacing(8)
        self.cols = cols
        left = QVBoxLayout()
        left.setSpacing(8)
        right = QVBoxLayout()
        right.setSpacing(8)
        left.addWidget(self._identify_card())
        left.addWidget(self._auto_card())
        left.addWidget(self._scan_export_card())
        left.addStretch(1)
        right.addWidget(self._models_card())
        self.storage_panel = self._storage_card()
        right.addWidget(self.storage_panel)
        right.addWidget(self._reset_card())
        right.addStretch(1)
        cols.addLayout(left, 1)
        cols.addLayout(right, 1)
        self.root.addLayout(cols)
        self.root.addStretch(1)
        self._fit_columns()
        self._work_out_sizes()

    def _fit_columns(self) -> None:
        """Two columns when there's room, one under the other on a small window."""
        if getattr(self, "cols", None) is not None:
            self.cols.setDirection(_cols_dir(self.width() >= 1180))

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        self._fit_columns()

    def focus_storage(self) -> None:
        if self.storage_panel is not None:
            self.scroll.ensureWidgetVisible(self.storage_panel, 0, 40)

    def _queue_changed(self) -> None:
        # downloads finishing / starting change the model rows
        if hasattr(self, "model_rows"):
            for k, row in self.model_rows.items():
                try:
                    row["state"].setText(self._dl_state(k))
                except RuntimeError:
                    pass

    # ---- helpers -------------------------------------------------------------------
    def _section(self, title: str, sub: str = "") -> tuple[QFrame, QVBoxLayout]:
        c = panel()
        l = QVBoxLayout(c)
        l.setContentsMargins(12, 10, 12, 12)
        l.setSpacing(8)
        h = QHBoxLayout()
        h.setSpacing(10)
        h.addWidget(label(title, "h2"))
        if sub:
            s = ElidedLabel(sub)
            s.setObjectName("faint")
            h.addWidget(s, 1)
        else:
            h.addStretch(1)
        l.addLayout(h)
        return c, l

    def _toggle_row(self, l: QVBoxLayout, text: str, key: str, tip: str = "") -> QCheckBox:
        t = QCheckBox(text)
        t.setChecked(bool(self.s.get(key)))
        t.setToolTip(tip)
        t.toggled.connect(lambda v: self.s.set(key, v))
        l.addWidget(t)
        return t

    def _spin(self, key: str, lo: float, hi: float, step: float, suffix: str, dec: int = 1) -> QDoubleSpinBox:
        sp = QDoubleSpinBox()
        sp.setRange(lo, hi)
        sp.setSingleStep(step)
        sp.setDecimals(dec)
        sp.setSuffix(suffix)
        sp.setValue(float(self.s.get(key)))
        sp.setFixedWidth(96)
        sp.valueChanged.connect(lambda v: self.s.set(key, v))
        return sp

    def _field_row(self, l, name: str) -> QHBoxLayout:
        r = QHBoxLayout()
        r.setSpacing(8)
        lb = label(name, "cap")
        lb.setFixedWidth(120)
        r.addWidget(lb)
        l.addLayout(r)
        return r

    # ---- AI models --------------------------------------------------------------
    def _dl_state(self, key: str) -> str:
        qj = self.dl_jobs.get(key)
        if qj is None:
            return ""
        if self.queue.current is qj:
            return "Downloading - progress is in the status bar"
        return "Queued"

    def _models_card(self) -> QFrame:
        c, l = self._section("AI models", "They run on your PC: on the graphics card when they can")
        self.model_rows: dict[str, dict] = {}
        for i, spec in enumerate((models.AST, models.CLIP)):
            if i:
                l.addWidget(hline())
            st = models.status(spec)
            top = QHBoxLayout()
            top.addWidget(label(spec.label, "h2"))
            if st["present"]:
                top.addWidget(chip("Ready", C["ok"]))
            else:
                top.addWidget(chip("Not downloaded", C["warn"]))
            state = label(self._dl_state(spec.key), "muted")
            top.addWidget(state)
            top.addStretch(1)
            btn = button(f"Download ({spec.approx_mb} MB)", "download")
            btn.setEnabled(spec.key not in self.dl_jobs)
            btn.clicked.connect(lambda _=False, sp=spec: self.download(sp))
            top.addWidget(btn)
            if st["present"]:
                btn.hide()
            l.addLayout(top)
            where = st["dir"] if st["present"] else str(models.own_dir(spec))
            note = spec.use + ". "
            if spec.key == "clip":
                note += ("Using Collection Sorter's copy." if st["shared"] else
                         "Collection Sorter's copy wasn't found." if not st["present"] else "Using Nirvash's own copy.")
            l.addWidget(label(note, "muted", wrap=True))
            p = ElidedLabel(where, mode=Qt.ElideMiddle)
            p.setObjectName("mono")
            l.addWidget(p)
            self.model_rows[spec.key] = {"btn": btn, "state": state}
        l.addWidget(hline())
        r = self._field_row(l, "Run models on")
        dev = QComboBox()
        for k, t in (("auto", "Graphics card if possible"), ("gpu", "Graphics card"), ("cpu", "Processor only")):
            dev.addItem(t, k)
        dev.setCurrentIndex(max(0, dev.findData(self.s.get("models.device", "auto"))))
        dev.activated.connect(lambda: (self.s.set("models.device", dev.currentData()), encoders.reset()))
        r.addWidget(dev)
        r.addStretch(1)
        ad = gpu.best_adapter()
        provs = [p.replace("ExecutionProvider", "") for p in available_providers()]
        txt = f"Graphics card used: {ad.name} ({ad.memory_gb:.0f} GB)" if ad else "No graphics card found for the models"
        l.addWidget(label(f"{txt} · engines: {', '.join(provs) or 'none'}", "faint", wrap=True))
        return c

    def download(self, spec: models.ModelSpec) -> None:
        if spec.key in self.dl_jobs:
            return

        def fin(ok: bool, msg: str = ""):
            self.dl_jobs.pop(spec.key, None)
            encoders.reset()
            hub.modelsChanged.emit()
            if ok:
                hub.toast.emit(f"{spec.label} downloaded.", "ok")
            elif msg:
                hub.toast.emit(f"{spec.label} didn't download: {msg}", "error")
            self.build()

        qj = self.queue.add(f"Downloading {spec.label.split(' (')[0]}", lambda ctx: models.download(spec, ctx.progress, ctx.cancel),
                            detail=f"{spec.approx_mb} MB to {models.own_dir(spec)}",
                            on_done=lambda _r: fin(True), on_failed=lambda e: fin(False, e),
                            on_stopped=lambda: fin(False), done_text=lambda _r: f"{spec.label} is ready")
        self.dl_jobs[spec.key] = qj
        self.build()

    # ---- identification ----------------------------------------------------------------
    def _identify_card(self) -> QFrame:
        c, l = self._section("Song identification", "Runs in the background after songs are added")
        self._toggle_row(l, "Shazam (no key needed, about 18 lookups a minute)", "identify.shazam")
        r = self._field_row(l, "AcoustID key")
        key = QLineEdit(self.s.get("identify.acoustid_key", ""))
        key.setPlaceholderText("Optional backup - free from acoustid.org")
        key.editingFinished.connect(lambda: self.s.set("identify.acoustid_key", key.text().strip()))
        r.addWidget(key, 1)
        l.addWidget(label("AcoustID matches whole songs, so it rarely finds short clips. Get a free key: sign in at "
                          "acoustid.org and register an application.", "faint", wrap=True))
        r = self._field_row(l, "AudioTag key")
        atk = QLineEdit(self.s.get("identify.audiotag_key", ""))
        atk.setPlaceholderText("Optional third backup - free from audiotag.info")
        atk.setEchoMode(QLineEdit.PasswordEchoOnEdit)
        self.at_status = label("", "muted", wrap=True)

        def save_atk():
            self.s.set("identify.audiotag_key", atk.text().strip())
            from ..jobs.identify import reset_audiotag
            reset_audiotag()

        atk.editingFinished.connect(save_atk)
        r.addWidget(atk, 1)
        chk = button("Check", "refresh")
        chk.setToolTip("Check the key works and how much free time is left this month")
        chk.clicked.connect(lambda: (save_atk(), self._check_audiotag()))
        r.addWidget(chk)
        l.addWidget(self.at_status)
        l.addWidget(label("AudioTag gives 3 hours of audio a month free. Nirvash sends 15 s per song, so that's about "
                          "700 songs a month. Get a key: sign up at audiotag.info, then API → API keys.", "faint",
                          wrap=True))
        self._toggle_row(l, "Full length, genre and year from iTunes", "identify.itunes")
        self._toggle_row(l, "Genres from MusicBrainz", "identify.musicbrainz")
        self._toggle_row(l, "Lyrics from LRCLIB", "identify.lyrics")
        row = QHBoxLayout()
        retry = button("Retry failed lookups", "refresh")
        retry.clicked.connect(lambda: self._retry("error"))
        row.addWidget(retry)
        retry2 = button("Retry songs not found", "refresh")
        retry2.clicked.connect(lambda: self._retry("not_found"))
        row.addWidget(retry2)
        row.addStretch(1)
        l.addLayout(row)
        return c

    def _check_audiotag(self) -> None:
        key = (self.s.get("identify.audiotag_key") or "").strip()
        if not key:
            self.at_status.setText("Enter a key first.")
            return
        self.at_status.setText("Checking…")
        from ..lookup import services

        def done(st):
            secs = int(st.get("identification_free_sec_remainder") or 0)
            self.at_status.setText(f"Key works. Free time left: {secs // 60} min {secs % 60} s "
                                   f"(about {secs // 15} songs). Lookups so far: {st.get('queries_count', 0)}.")

        workers.run_bg(services.audiotag_stat, key, done=done,
                       failed=lambda msg: self.at_status.setText(f"Couldn't check: {msg}"))

    def _retry(self, status: str) -> None:
        n = get_db().x("UPDATE songs SET id_status='pending' WHERE id_status=?", (status,))
        hub.songsReset.emit()
        w = self.window()
        if hasattr(w, "identify"):
            w.identify.poke()
        hub.toast.emit(f"Queued {n} song{'s' if n != 1 else ''} for another lookup.", "ok")

    # ---- auto-music -----------------------------------------------------------------------
    def _auto_card(self) -> QFrame:
        c, l = self._section("Auto-music defaults", "Used for every video unless you change it there")
        r = self._field_row(l, "Shorter song")
        mode = QComboBox()
        for k, t in SHORT_SONG_MODES.items():
            mode.addItem(t, k)
        mode.setCurrentIndex(max(0, mode.findData(self.s.get("automusic.short_mode"))))
        mode.setToolTip("What happens when a song is shorter than its video")
        mode.activated.connect(lambda: self.s.set("automusic.short_mode", mode.currentData()))
        r.addWidget(mode, 1)
        g = QGridLayout()
        g.setHorizontalSpacing(8)
        g.setVerticalSpacing(6)
        for col, (name, w) in enumerate((("Crossfade", self._spin("automusic.crossfade_s", 0.5, 15, 0.5, " s")),
                                          ("Fade in", self._spin("automusic.fade_in_s", 0, 15, 0.5, " s")),
                                          ("Fade out", self._spin("automusic.fade_out_s", 0, 15, 0.5, " s")),
                                          ("Volume", self._spin("automusic.volume", 0.1, 1.5, 0.05, "×", 2)))):
            lb = label(name, "cap")
            if col % 2 == 0:
                lb.setFixedWidth(120)
            g.addWidget(lb, col // 2, (col % 2) * 2)
            g.addWidget(w, col // 2, (col % 2) * 2 + 1)
        g.setColumnStretch(4, 1)
        l.addLayout(g)
        self._toggle_row(l, "Smart start: skip quiet intros, start on a beat", "automusic.smart_start")
        self._toggle_row(l, "Also give songs to videos that already have sound (replaces their audio)",
                         "automusic.include_with_sound")
        l.addWidget(label("How much each thing matters when picking a song", "muted"))
        wg = QGridLayout()
        wg.setVerticalSpacing(4)
        weights = dict(self.s.get("automusic.weights"))
        for i, (k, name) in enumerate((("length", "Length fit"), ("pace", "Pace"), ("mood", "Mood"),
                                       ("prefs", "Favourites & categories"))):
            lb = label(name, "cap")
            lb.setFixedWidth(150)
            wg.addWidget(lb, i, 0)
            sl = QSlider(Qt.Horizontal)
            sl.setRange(0, 100)
            sl.setValue(int(round(float(weights.get(k, 0.25)) * 100)))
            val = label(f"{sl.value()}", "muted")
            val.setFixedWidth(28)
            sl.valueChanged.connect(lambda v, key=k, vl=val: (vl.setText(str(v)), self._weight(key, v)))
            wg.addWidget(sl, i, 1)
            wg.addWidget(val, i, 2)
        wg.setColumnStretch(1, 1)
        l.addLayout(wg)
        return c

    def _weight(self, key: str, v: int) -> None:
        w = dict(self.s.get("automusic.weights"))
        w[key] = v / 100.0
        self.s.set("automusic.weights", w)

    # ---- scanning & export -----------------------------------------------------------------
    def _scan_export_card(self) -> QFrame:
        c, l = self._section("Finding music and exporting", "How music is told apart from talking, and export names")
        g = QGridLayout()
        g.setHorizontalSpacing(8)
        g.setVerticalSpacing(6)

        def name(t):
            lb = label(t, "cap")
            lb.setFixedWidth(220)
            return lb

        g.addWidget(name("Shortest music section kept"), 0, 0)
        g.addWidget(self._spin("scan.min_section_s", 3, 120, 1, " s", 0), 0, 1)
        g.addWidget(name("Counts as all music when at least"), 1, 0)
        pct = QDoubleSpinBox()
        pct.setRange(50, 100)
        pct.setDecimals(0)
        pct.setSuffix(" %")
        pct.setValue(float(self.s.get("scan.full_ratio")) * 100)
        pct.setFixedWidth(96)
        pct.valueChanged.connect(lambda v: self.s.set("scan.full_ratio", v / 100))
        g.addWidget(pct, 1, 1)
        g.addWidget(name("Treat as silent below"), 2, 0)
        g.addWidget(self._spin("scan.silence_db", -80, -20, 1, " dB", 0), 2, 1)
        g.addWidget(name("Talking detection sensitivity"), 3, 0)
        sens = QDoubleSpinBox()
        sens.setRange(10, 90)
        sens.setDecimals(0)
        sens.setSuffix(" %")
        sens.setToolTip("Lower = stricter: less talking is needed to leave a stretch out")
        sens.setValue(float(self.s.get("scan.speech_threshold", 0.5)) * 100)
        sens.setFixedWidth(96)
        sens.valueChanged.connect(lambda v: self.s.set("scan.speech_threshold", v / 100))
        g.addWidget(sens, 3, 1)
        g.addWidget(name("Exported file name ends with"), 4, 0)
        suf = QLineEdit(self.s.get("export.suffix", "_edited"))
        suf.setFixedWidth(140)
        suf.editingFinished.connect(lambda: self.s.set("export.suffix", suf.text().strip() or "_edited"))
        g.addWidget(suf, 4, 1)
        g.setColumnStretch(2, 1)
        l.addLayout(g)
        self._toggle_row(l, "Export faster (GPU), used when an edit needs a re-encode (Stage 2 editor)", "export.gpu")
        l.addWidget(label("Exports always go next to the originals and never overwrite them. Adding music copies the "
                          "video untouched.", "faint", wrap=True))
        return c

    # ---- storage ----------------------------------------------------------------------------
    def _storage_card(self) -> QFrame:
        c, l = self._section("Storage", "Every place Nirvash keeps things. Moving copies, checks, switches, then "
                                        "removes the old copy")
        self.size_lbls: dict[str, QLabel] = {}
        for i, (key, what, movable, clearable) in enumerate(storage.PLACES):
            if i:
                l.addWidget(hline())
            p = storage.place_path(key)
            top = QHBoxLayout()
            top.setSpacing(8)
            name = ElidedLabel(what)
            name.setObjectName("h2")
            top.addWidget(name, 1)
            sz = label("Working out…", "muted")
            self.size_lbls[key] = sz
            top.addWidget(sz)
            l.addLayout(top)
            bottom = QHBoxLayout()
            bottom.setSpacing(6)
            path = ElidedLabel(str(p), mode=Qt.ElideMiddle)
            path.setObjectName("mono")
            path.setToolTip(str(p))
            if not drive_ok(p):
                path.setText(f"{p}  ·  this drive isn't connected")
                path.setStyleSheet(f"color: {C['warn']};")
            bottom.addWidget(path, 1)
            op = button("Open", "folder", "ghost")
            op.setToolTip("Open this folder")
            op.clicked.connect(lambda _=False, pp=str(p): open_in_explorer(pp))
            bottom.addWidget(op)
            if movable:
                mv = button("Move…", "move")
                mv.clicked.connect(lambda _=False, k=key: self.move_place(k))
                bottom.addWidget(mv)
            if clearable:
                cl = button("Clear", "trash", "dangerGhost")
                cl.clicked.connect(self.clear_cache)
                bottom.addWidget(cl)
            l.addLayout(bottom)
        return c

    def _work_out_sizes(self) -> None:
        def calc():
            return {k: storage.folder_size(storage.place_path(k)) if drive_ok(storage.place_path(k)) else None
                    for k, *_ in storage.PLACES}

        def show(res):
            self.sizes = res
            for k, v in res.items():
                lbl = self.size_lbls.get(k)
                if lbl is None:
                    continue
                try:
                    lbl.setText("Can't be reached" if v is None else
                                f"{storage.human(v[0])} · {v[1]:,} file{'s' if v[1] != 1 else ''}")
                except RuntimeError:
                    pass

        workers.run_bg(calc, done=show)

    def move_place(self, key: str) -> None:
        if self.queue.busy():
            dialogs.info(self, "Can't move it right now", f"{self.queue.current.name} is running. Moving is only "
                         "possible when nothing else is running, so nothing is half-written.")
            return
        what = dict((k, w) for k, w, *_ in storage.PLACES)[key]
        src = storage.place_path(key)
        d = QFileDialog.getExistingDirectory(self, f"Move {what.split(':')[0].lower()} to…", str(src.parent))
        if not d:
            return
        dst = Path(d) / (src.name if key != "data" else "Nirvash data")
        size = self.sizes.get(key)
        sz = f" ({storage.human(size[0])})" if size else ""
        extra = ("\n\nThe database and logs are in use, so Nirvash switches to the new place when it restarts, "
                 "then removes the old copy.") if key == "data" else ""
        if not dialogs.ask(self, f"Move {what.split(':')[0].lower()}?",
                           f"From {src}\nTo {dst}{sz}\n\nEverything is copied, then checked file by file. Only then "
                           f"does Nirvash switch to the new place and remove the old copy.{extra}", "Move it"):
            return
        w = self.window()
        if key == "songs" and hasattr(w, "library"):
            w.library.stop_playing()

        def done(res):
            if res.get("restart"):
                if dialogs.ask(self, "Copied", "The copy is checked. Restart Nirvash now to switch to it?",
                               "Restart Nirvash", "Later"):
                    if hasattr(w, "restart"):
                        w.restart()
            else:
                hub.toast.emit(f"Moved to {res['to']}.", "ok")
                if key == "songs":
                    hub.songsReset.emit()
                if key == "models":
                    encoders.reset()
                    hub.modelsChanged.emit()
            self.build()

        self.queue.add(f"Moving {what.split(':')[0].lower()}", storage.move_place, key, d,
                       detail=f"To {dst}", on_done=done,
                       on_failed=lambda e: dialogs.info(self, "The move stopped", f"{e}\n\nNothing was switched; "
                                                        "everything is still in its old place."),
                       done_text=lambda r: f"now in {r['to']}")

    def clear_cache(self) -> None:
        size = self.sizes.get("cache")
        sz = storage.human(size[0]) if size else "the space it uses"
        if dialogs.ask(self, "Clear the cache?", f"Previews, waveforms and cover art are deleted; {sz} comes back. "
                       "They're made again when needed.", f"Clear {sz}", danger=True):
            workers.run_bg(storage.clear_cache, done=lambda n: (hub.toast.emit(f"Cleared {storage.human(n)}.", "ok"),
                                                                self._work_out_sizes()))

    # ---- resets ---------------------------------------------------------------------------------
    def _reset_card(self) -> QFrame:
        c, l = self._section("Reset", "One part at a time; each asks first and says what stays")
        nv, ns = get_db().ignore_counts()
        nm = len(get_db().moves(100000))
        rows = [
            ("Settings", "Back to the defaults (the look and themes stay)", self._reset_settings),
            ("Library", "Forget the song list; the song files stay on disk", self._reset_library),
            (f"Skipped videos & songs ({nv + ns})", "Let scans add music you deleted again", self._reset_ignored),
            (f"Move log ({nm})", "Forget the list of renames (Undo stops working for them)", self._reset_moves),
            ("Cache", "Previews, waveforms and cover art; made again when needed", self.clear_cache),
            ("Downloaded models", "Nirvash's own copies; Collection Sorter's CLIP is never touched", self._reset_models),
            ("Logs", "Empty the log files (they name the videos that were scanned)", self._clear_logs),
        ]
        g = QGridLayout()
        g.setHorizontalSpacing(10)
        g.setVerticalSpacing(6)
        for i, (t, why, fn) in enumerate(rows):
            b = button(t, "refresh", "dangerGhost")
            b.clicked.connect(fn)
            g.addWidget(b, i, 0)
            g.addWidget(label(why, "faint"), i, 1)
        g.setColumnStretch(1, 1)
        l.addLayout(g)
        return c

    def _reset_ignored(self) -> None:
        nv, ns = get_db().ignore_counts()
        if dialogs.ask(self, "Forget skipped videos and songs?", f"{nv} videos and {ns} songs you deleted are skipped "
                       "by scans. After this, scans can add their music again. No files change.",
                       "Forget them", danger=True):
            get_db().clear_ignored()
            scanjob.reset_ignored_index()
            self.build()
            hub.toast.emit("Scans will no longer skip them.", "ok")

    def _reset_moves(self) -> None:
        if dialogs.ask(self, "Forget the move log?", "The list of renames is cleared, so they can't be undone any more. "
                       "The files stay as they are.", "Forget the move log", danger=True):
            get_db().clear_moves()
            hub.movesChanged.emit()
            self.build()

    def _clear_logs(self) -> None:
        if dialogs.ask(self, "Empty the logs?", "The log files list the videos that were scanned and what was found. "
                       "Crash logs are emptied too.", "Empty the logs", danger=True):
            from ..core import log as _log
            _log.clear()
            w = self.window()
            if hasattr(w, "drawer"):
                w.drawer.log.clear()
            hub.toast.emit("Logs cleared.", "ok")

    def _reset_settings(self) -> None:
        if dialogs.ask(self, "Reset settings?", "Every setting goes back to its default, including folders and keys. "
                       "The look, saved themes, the library and your files stay.", "Reset settings", danger=True):
            self.s.reset()
            self.build()

    def _reset_library(self) -> None:
        if dialogs.ask(self, "Reset the library?", "Song info, favourites, categories, listens and scan history are "
                       "forgotten. The song files themselves stay in the Songs folder.", "Reset the library",
                       danger=True):
            get_db().reset_library()
            scanjob.reset_fp_index()
            hub.songsReset.emit()
            hub.categoriesChanged.emit()

    def _reset_models(self) -> None:
        if dialogs.ask(self, "Delete downloaded models?", f"Nirvash's own copies in {self.s.folder('models')} are "
                       f"deleted. Collection Sorter's CLIP ({collection_sorter_clip_dir()}) is never touched.",
                       "Delete the models", danger=True):
            encoders.reset()
            for spec in (models.AST, models.CLIP):
                shutil.rmtree(models.own_dir(spec), ignore_errors=True)
            hub.modelsChanged.emit()
            self.build()
