"""Settings: folders, AI models, song lookups, defaults, resets."""
from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLineEdit,
                               QMessageBox, QProgressBar, QScrollArea, QSlider, QVBoxLayout, QWidget)

from ..analysis import encoders, models
from ..analysis.runtime import available_providers
from ..core.db import get_db
from ..core.paths import collection_sorter_clip_dir, data_dir
from ..core.settings import SHORT_SONG_MODES, get_settings
from ..jobs import scan as scanjob
from . import workers
from .hub import hub
from .theme import C
from .widgets import TaskProgress, Toggle, button, card, chip, clear_layout, hline, label, open_in_explorer


class SettingsPage(QScrollArea):
    def __init__(self):
        super().__init__()
        self.s = get_settings()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        inner.setObjectName("page")
        self.setWidget(inner)
        self.root = QVBoxLayout(inner)
        self.root.setContentsMargins(22, 18, 22, 22)
        self.root.setSpacing(14)
        self.dl_jobs: dict[str, workers.Job] = {}
        self.dl_state: dict[str, dict] = {}
        self.build()

    def build(self) -> None:
        clear_layout(self.root)
        self.root.addWidget(label("Settings", "h1"))
        cols = QHBoxLayout()
        cols.setSpacing(14)
        left = QVBoxLayout()
        left.setSpacing(14)
        right = QVBoxLayout()
        right.setSpacing(14)
        left.addWidget(self._models_card())
        left.addWidget(self._folders_card())
        left.addWidget(self._reset_card())
        left.addStretch(1)
        right.addWidget(self._identify_card())
        right.addWidget(self._auto_card())
        right.addWidget(self._scan_export_card())
        right.addStretch(1)
        cols.addLayout(left, 1)
        cols.addLayout(right, 1)
        self.root.addLayout(cols)
        self.root.addStretch(1)

    def _section(self, title: str, sub: str = "") -> tuple[QFrame, QVBoxLayout]:
        c = card()
        l = QVBoxLayout(c)
        l.setContentsMargins(16, 14, 16, 14)
        l.setSpacing(10)
        l.addWidget(label(title, "h2"))
        if sub:
            l.addWidget(label(sub, "muted", wrap=True))
        return c, l

    def _toggle_row(self, l: QVBoxLayout, text: str, key: str, tip: str = "") -> Toggle:
        row = QHBoxLayout()
        t = Toggle(bool(self.s.get(key)))
        t.toggledOn.connect(lambda v: self.s.set(key, v))
        row.addWidget(t)
        lb = label(text)
        lb.setToolTip(tip)
        row.addWidget(lb, 1)
        l.addLayout(row)
        return t

    def _spin(self, key: str, lo: float, hi: float, step: float, suffix: str, dec: int = 1) -> QDoubleSpinBox:
        sp = QDoubleSpinBox()
        sp.setRange(lo, hi)
        sp.setSingleStep(step)
        sp.setDecimals(dec)
        sp.setSuffix(suffix)
        sp.setValue(float(self.s.get(key)))
        sp.setFixedWidth(100)
        sp.valueChanged.connect(lambda v: self.s.set(key, v))
        return sp

    # ---- AI models --------------------------------------------------------------
    def _models_card(self) -> QFrame:
        c, l = self._section("AI models", "Run on your PC. Graphics card used when available.")
        self.model_rows: dict[str, dict] = {}
        for spec in (models.AST, models.CLIP):
            st = models.status(spec)
            box = QVBoxLayout()
            top = QHBoxLayout()
            top.addWidget(label(spec.label))
            if st["present"]:
                top.addWidget(chip("Ready", C["teal"]))
            elif spec.key in self.dl_jobs:
                top.addWidget(chip("Downloading", C["violet"]))
            else:
                top.addWidget(chip("Not downloaded", C["amber"]))
            top.addStretch(1)
            btn = button(f"Download (~{spec.approx_mb} MB)", "download")
            btn.setVisible(not st["present"])
            btn.clicked.connect(lambda _=False, sp=spec: self.download(sp))
            top.addWidget(btn)
            box.addLayout(top)
            where = st["dir"] if st["present"] else str(models.own_dir(spec))
            note = spec.use + ". "
            if spec.key == "clip":
                note += ("Using Collection Sorter's copy." if st["shared"] else
                         "Collection Sorter's copy wasn't found." if not st["present"] else "Using Nirvash's own copy.")
            box.addWidget(label(note, "muted", wrap=True))
            path_l = label(where, "faint", wrap=True)
            path_l.setTextInteractionFlags(Qt.TextSelectableByMouse)
            box.addWidget(path_l)
            task = TaskProgress()
            task.setVisible(spec.key in self.dl_jobs)
            box.addWidget(task)
            if spec.key in self.dl_jobs:
                btn.setEnabled(False)
                stt = self.dl_state.get(spec.key, {})
                task.start(stt.get("msg", "Starting download…"), t0=stt.get("t0"))
                task.update(stt.get("frac", 0.0), stt.get("msg"))
            self.model_rows[spec.key] = {"btn": btn, "task": task}
            l.addLayout(box)
            l.addWidget(hline())
        row = QHBoxLayout()
        row.addWidget(label("Run models on"))
        dev = QComboBox()
        for k, t in (("auto", "Graphics card if possible"), ("gpu", "Graphics card"), ("cpu", "Processor only")):
            dev.addItem(t, k)
        dev.setCurrentIndex(max(0, dev.findData(self.s.get("models.device", "auto"))))
        dev.activated.connect(lambda: (self.s.set("models.device", dev.currentData()), encoders.reset()))
        row.addWidget(dev)
        row.addStretch(1)
        l.addLayout(row)
        provs = [p.replace("ExecutionProvider", "") for p in available_providers()]
        l.addWidget(label("Available: " + (", ".join(provs) or "none"), "faint"))
        return c

    def download(self, spec: models.ModelSpec) -> None:
        if spec.key in self.dl_jobs:
            return
        import time
        job = workers.Job(lambda ctx: models.download(spec, ctx.progress, ctx.cancel))
        self.dl_jobs[spec.key] = job
        self.dl_state[spec.key] = {"t0": time.time(), "frac": 0.0, "msg": "Starting download…"}
        job.sig.progress.connect(lambda f, m, k=spec.key: self._dl_progress(k, f, m))

        def fin(ok: bool, msg: str = ""):
            self.dl_jobs.pop(spec.key, None)
            self.dl_state.pop(spec.key, None)
            encoders.reset()
            hub.modelsChanged.emit()
            hub.toast.emit(f"{spec.label} downloaded." if ok else f"{spec.label} download failed: {msg}",
                           "ok" if ok else "error")
            self.build()

        job.sig.done.connect(lambda _r: fin(True))
        job.sig.failed.connect(lambda e: fin(False, e))
        job.sig.cancelled.connect(lambda: fin(False, "cancelled"))
        workers.start(job)
        self.build()

    def _dl_progress(self, key: str, frac: float, msg: str) -> None:
        stt = self.dl_state.get(key)
        if stt is None:
            return
        stt["frac"], stt["msg"] = frac, msg
        row = self.model_rows.get(key)
        if row:
            try:
                row["task"].update(frac, msg)
            except RuntimeError:  # the row was rebuilt
                pass

    # ---- folders -------------------------------------------------------------------
    def _folders_card(self) -> QFrame:
        c, l = self._section("Folders", "Big files live on your hard drive. Changing a folder only affects new files.")
        g = QGridLayout()
        g.setHorizontalSpacing(8)
        g.setVerticalSpacing(8)
        rows = [("root", "Nirvash folder", self.s.root()), ("songs", "Songs", self.s.folder("songs")),
                ("overlays", "Overlays", self.s.folder("overlays")), ("models", "AI models", self.s.folder("models")),
                ("cache", "Cache", self.s.folder("cache"))]
        for i, (key, name, path) in enumerate(rows):
            g.addWidget(label(name, "muted"), i, 0)
            e = QLineEdit(str(path))
            e.setReadOnly(True)
            g.addWidget(e, i, 1)
            ch = button("Change")
            ch.clicked.connect(lambda _=False, k=key: self._change_folder(k))
            g.addWidget(ch, i, 2)
            op = button("Open")
            op.clicked.connect(lambda _=False, p=str(path): open_in_explorer(p))
            g.addWidget(op, i, 3)
        g.addWidget(label("Settings & library", "muted"), len(rows), 0)
        e = QLineEdit(str(data_dir()))
        e.setReadOnly(True)
        g.addWidget(e, len(rows), 1)
        op = button("Open")
        op.clicked.connect(lambda: open_in_explorer(str(data_dir())))
        g.addWidget(op, len(rows), 3)
        g.setColumnStretch(1, 1)
        l.addLayout(g)
        return c

    def _change_folder(self, key: str) -> None:
        cur = str(self.s.root() if key == "root" else self.s.folder(key))
        d = QFileDialog.getExistingDirectory(self, "Choose folder", cur)
        if d:
            self.s.set(f"folders.{key}", str(Path(d)))
            hub.modelsChanged.emit()
            self.build()

    # ---- identification ----------------------------------------------------------------
    def _identify_card(self) -> QFrame:
        c, l = self._section("Song identification", "Runs in the background after songs are added.")
        self._toggle_row(l, "Shazam (no key needed, about 20 lookups a minute)", "identify.shazam")
        row = QHBoxLayout()
        row.addWidget(label("AcoustID key"))
        key = QLineEdit(self.s.get("identify.acoustid_key", ""))
        key.setPlaceholderText("Optional backup — free from acoustid.org")
        key.editingFinished.connect(lambda: self.s.set("identify.acoustid_key", key.text().strip()))
        row.addWidget(key, 1)
        l.addLayout(row)
        l.addWidget(label("AcoustID matches whole songs, so it rarely finds short clips. Get a free key: sign in at "
                          "acoustid.org and register an application.", "faint", wrap=True))
        self._toggle_row(l, "AudD as a third backup (300 free lookups per account, then paid)", "identify.audd")
        row = QHBoxLayout()
        row.addWidget(label("AudD token"))
        tok = QLineEdit(self.s.get("identify.audd_token", ""))
        tok.setPlaceholderText("From dashboard.audd.io")
        tok.setEchoMode(QLineEdit.PasswordEchoOnEdit)

        def save_tok():
            self.s.set("identify.audd_token", tok.text().strip())
            from ..jobs.identify import reset_audd
            reset_audd()

        tok.editingFinished.connect(save_tok)
        row.addWidget(tok, 1)
        l.addLayout(row)
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

    def _retry(self, status: str) -> None:
        n = get_db().x("UPDATE songs SET id_status='pending' WHERE id_status=?", (status,))
        hub.songsReset.emit()
        w = self.window()
        if hasattr(w, "identify"):
            w.identify.poke()
        hub.toast.emit(f"Queued {n} song{'s' if n != 1 else ''} for another lookup.", "ok")

    # ---- auto-music -----------------------------------------------------------------------
    def _auto_card(self) -> QFrame:
        c, l = self._section("Auto-music defaults")
        row = QHBoxLayout()
        row.addWidget(label("If a song is shorter than the video"))
        mode = QComboBox()
        for k, t in SHORT_SONG_MODES.items():
            mode.addItem(t, k)
        mode.setCurrentIndex(max(0, mode.findData(self.s.get("automusic.short_mode"))))
        mode.activated.connect(lambda: self.s.set("automusic.short_mode", mode.currentData()))
        row.addWidget(mode, 1)
        l.addLayout(row)
        g = QGridLayout()
        g.setHorizontalSpacing(10)
        g.addWidget(label("Crossfade"), 0, 0)
        g.addWidget(self._spin("automusic.crossfade_s", 0.5, 15, 0.5, " s"), 0, 1)
        g.addWidget(label("Fade in"), 0, 2)
        g.addWidget(self._spin("automusic.fade_in_s", 0, 15, 0.5, " s"), 0, 3)
        g.addWidget(label("Fade out"), 1, 0)
        g.addWidget(self._spin("automusic.fade_out_s", 0, 15, 0.5, " s"), 1, 1)
        g.addWidget(label("Volume"), 1, 2)
        g.addWidget(self._spin("automusic.volume", 0.1, 1.5, 0.05, "×", 2), 1, 3)
        g.setColumnStretch(4, 1)
        l.addLayout(g)
        self._toggle_row(l, "Smart start: skip quiet intros, start on a beat", "automusic.smart_start")
        self._toggle_row(l, "Also give songs to videos that already have sound (replaces their audio)",
                         "automusic.include_with_sound")
        l.addWidget(label("How much each thing matters when picking a song", "muted"))
        wg = QGridLayout()
        weights = dict(self.s.get("automusic.weights"))
        for i, (k, name) in enumerate((("length", "Length fit"), ("pace", "Pace"), ("mood", "Mood"),
                                       ("prefs", "Favourites & categories"))):
            wg.addWidget(label(name), i, 0)
            sl = QSlider(Qt.Horizontal)
            sl.setRange(0, 100)
            sl.setValue(int(round(float(weights.get(k, 0.25)) * 100)))
            val = label(f"{sl.value()}", "muted")
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
        c, l = self._section("Finding music & exporting")
        g = QGridLayout()
        g.setHorizontalSpacing(10)
        g.addWidget(label("Shortest music section kept"), 0, 0)
        g.addWidget(self._spin("scan.min_section_s", 3, 120, 1, " s", 0), 0, 1)
        g.addWidget(label("Counts as all music when at least"), 1, 0)
        pct = QDoubleSpinBox()
        pct.setRange(50, 100)
        pct.setDecimals(0)
        pct.setSuffix(" %")
        pct.setValue(float(self.s.get("scan.full_ratio")) * 100)
        pct.setFixedWidth(100)
        pct.valueChanged.connect(lambda v: self.s.set("scan.full_ratio", v / 100))
        g.addWidget(pct, 1, 1)
        g.addWidget(label("Treat as silent below"), 2, 0)
        g.addWidget(self._spin("scan.silence_db", -80, -20, 1, " dB", 0), 2, 1)
        g.addWidget(label("Talking detection sensitivity"), 4, 0)
        sens = QDoubleSpinBox()
        sens.setRange(10, 90)
        sens.setDecimals(0)
        sens.setSuffix(" %")
        sens.setToolTip("Lower = stricter: less talking is needed to leave a stretch out")
        sens.setValue(float(self.s.get("scan.speech_threshold", 0.5)) * 100)
        sens.setFixedWidth(100)
        sens.valueChanged.connect(lambda v: self.s.set("scan.speech_threshold", v / 100))
        g.addWidget(sens, 4, 1)
        g.addWidget(label("Exported file name ends with"), 3, 0)
        suf = QLineEdit(self.s.get("export.suffix", "_edited"))
        suf.setFixedWidth(140)
        suf.editingFinished.connect(lambda: self.s.set("export.suffix", suf.text().strip() or "_edited"))
        g.addWidget(suf, 3, 1)
        g.setColumnStretch(2, 1)
        l.addLayout(g)
        self._toggle_row(l, "Export faster (GPU) — used when an edit needs a re-encode (Stage 2 editor)",
                         "export.gpu")
        l.addWidget(label("Exports always go next to the originals and never overwrite them. Adding music copies the "
                          "video untouched.", "muted", wrap=True))
        return c

    # ---- resets ---------------------------------------------------------------------------------
    def _reset_card(self) -> QFrame:
        c, l = self._section("Reset", "Each reset asks first.")
        row = QHBoxLayout()
        for text, fn in (("Settings", self._reset_settings), ("Library database", self._reset_library),
                         ("Cache", self._reset_cache), ("Downloaded models", self._reset_models)):
            b = button(text, "refresh")
            b.setObjectName("danger")
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        l.addLayout(row)
        row2 = QHBoxLayout()
        nv, ns = get_db().ignore_counts()
        ign = button(f"Skipped videos & songs ({nv + ns})", "refresh")
        ign.setToolTip(f"{nv} videos and {ns} songs you deleted are skipped by scans. "
                       "Clearing this lets scans add them again.")
        ign.clicked.connect(self._reset_ignored)
        row2.addWidget(ign)
        logs = button("Logs", "trash")
        logs.setToolTip("Empty the log files. They name the videos that were scanned.")
        logs.clicked.connect(self._clear_logs)
        row2.addWidget(logs)
        row2.addStretch(1)
        l.addLayout(row2)
        return c

    def _reset_ignored(self) -> None:
        nv, ns = get_db().ignore_counts()
        if self._ask(f"Forget the {nv} videos and {ns} songs you deleted?\n\n"
                     "Scans will be able to add their music again."):
            get_db().clear_ignored()
            scanjob.reset_ignored_index()
            self.build()
            hub.toast.emit("Scans will no longer skip them.", "ok")

    def _clear_logs(self) -> None:
        if self._ask("Empty the log files? They list the videos that were scanned and what was found."):
            from ..core import log as _log
            _log.clear()
            w = self.window()
            if hasattr(w, "log_view"):
                w.log_view.clear()
            hub.toast.emit("Logs cleared.", "ok")

    def _ask(self, text: str) -> bool:
        return QMessageBox.question(self, "Reset", text) == QMessageBox.Yes

    def _reset_settings(self) -> None:
        if self._ask("Put every setting back to its default?"):
            self.s.reset()
            self.build()

    def _reset_library(self) -> None:
        if self._ask("Clear the library database (song info, favourites, categories, scan history)?\n\n"
                     "The song files themselves stay in the Songs folder."):
            get_db().reset_library()
            scanjob.reset_fp_index()
            hub.songsReset.emit()
            hub.categoriesChanged.emit()

    def _reset_cache(self) -> None:
        if self._ask("Delete the cache (previews, waveforms, cover art)? It's rebuilt as needed."):
            shutil.rmtree(self.s.folder("cache"), ignore_errors=True)
            self.s.folder("cache")
            hub.toast.emit("Cache cleared.", "ok")

    def _reset_models(self) -> None:
        if self._ask(f"Delete the models Nirvash downloaded in {self.s.folder('models')}?\n\n"
                     f"Collection Sorter's CLIP ({collection_sorter_clip_dir()}) is never touched."):
            encoders.reset()
            for spec in (models.AST, models.CLIP):
                shutil.rmtree(models.own_dir(spec), ignore_errors=True)
            hub.modelsChanged.emit()
            self.build()
