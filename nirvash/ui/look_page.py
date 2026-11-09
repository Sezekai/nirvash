"""Appearance (Look in the rail): backgrounds, accent, font, corners, density, buttons, saved themes and theme files.
Everything applies as it changes; saved themes and the current look live in appearance.json in the data folder."""
from __future__ import annotations

import copy
from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath
from PySide6.QtWidgets import (QApplication, QColorDialog, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QLineEdit, QMenu, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSlider,
                               QToolButton, QVBoxLayout, QWidget)

from ..core import appearance as ap
from . import dialogs, theme
from .hub import hub
from .theme import C, icon
from .widgets import FlowLayout, Segmented, button, chip, clear_layout, flow_row, label, page_header, panel

GROUPS = [
    ("Space", ["starglass", "starfield"]),
    ("Aubergine and its variations", ["aubergine", "auberginegreen", "garnet", "violethaze", "tealdepth"]),
    ("Dark", ["obsidian", "carbon", "graphite", "midnight", "ink", "slate", "plum", "rose", "petrol", "nightsky",
              "olive", "wine", "forest", "teal", "mocha", "indigo"]),
    ("Light", ["daylight", "linen"]),
]


class Swatch(QPushButton):
    """A clickable preview of a background or an accent."""

    def __init__(self, kind: str, key: str, parent=None):
        super().__init__(parent)
        self.kind, self.key = kind, key
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        w, h = (92, 46) if kind == "bg" else (40, 24)
        self.setToolTip((ap.BACKGROUNDS if kind == "bg" else ap.ACCENTS)[key]["name"])
        # the size goes in the style too: the app's button style would otherwise override a fixed size
        self.setStyleSheet(f"QPushButton {{ border: none; background: transparent; padding: 0; min-height: {h}px; "
                           f"max-height: {h}px; min-width: {w}px; max-width: {w}px; }}")
        self.setFixedSize(w, h)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        path = QPainterPath()
        path.addRoundedRect(r, 6, 6)
        p.setClipPath(path)
        if self.kind == "bg":
            b = ap.BACKGROUNDS[self.key]
            w = r.width()
            if (b.get("scene") or {}).get("image") == "starfield":
                import random
                p.fillRect(QRectF(r.x(), r.y(), w * 0.5, r.height()), QColor(b["ground"]))
                rnd = random.Random(3)
                p.setPen(Qt.NoPen)
                for _ in range(26):
                    c = QColor("#ffffff")
                    c.setAlphaF(0.35 + rnd.random() * 0.6)
                    p.setBrush(c)
                    p.drawEllipse(QRectF(r.x() + rnd.random() * w * 0.5, r.y() + rnd.random() * r.height(), 1.4, 1.4))
            else:
                p.fillRect(QRectF(r.x(), r.y(), w * 0.5, r.height()), QColor(b["ground"]))
            p.fillRect(QRectF(r.x() + w * 0.5, r.y(), w * 0.25, r.height()), QColor(b["panel"]))
            p.fillRect(QRectF(r.x() + w * 0.75, r.y(), w * 0.25, r.height()), QColor(b["raised2"]))
            p.setClipping(False)
            p.setPen(QColor(b["text"]))
            f = QFont(self.font())
            f.setPixelSize(10)
            p.setFont(f)
            p.drawText(r.adjusted(5, 0, -2, -3), Qt.AlignLeft | Qt.AlignBottom, b["name"].split(",")[0])
        else:
            stops = ap.ACCENTS[self.key]["stops"]
            if len(stops) == 1:
                p.fillRect(r, QColor(stops[0]))
            else:
                g = QLinearGradient(r.topLeft(), r.topRight())
                for i, s in enumerate(stops):
                    g.setColorAt(i / (len(stops) - 1), QColor(s))
                p.fillRect(r, g)
        p.setClipping(False)
        if self.isChecked():
            p.setPen(QColor(C["text"]))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 7, 7)
        elif self.underMouse():
            p.setPen(QColor(C["line2"]))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 7, 7)


class ColorButton(QPushButton):
    """A colour chip that opens a picker, plus a hex field next to it."""

    picked = Signal(str)

    def __init__(self, color: str, parent=None):
        super().__init__(parent)
        self.setFixedSize(28, 22)
        self.setCursor(Qt.PointingHandCursor)
        self.color = color
        self.clicked.connect(self._pick)
        self._paint()

    def set_color(self, c: str) -> None:
        self.color = c
        self._paint()

    def _paint(self) -> None:
        self.setStyleSheet(f"QPushButton {{ background: {self.color}; border: 1px solid {C['line2']}; border-radius: 5px;"
                           " min-height: 22px; max-height: 22px; min-width: 28px; max-width: 28px; padding: 0; }")

    def _pick(self) -> None:
        c = QColorDialog.getColor(QColor(self.color), self.window(), "Choose a colour")
        if c.isValid():
            self.set_color(c.name())
            self.picked.emit(c.name())


class ThemeCard(QFrame):
    clicked = Signal(str)

    def __init__(self, t: dict, active: bool, page: "LookPage"):
        super().__init__()
        self.setObjectName("card2")
        self.setProperty("chosen", "true" if active else "false")
        self.name = t["name"]
        self.t = t
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(5)
        strip = QLabel()
        strip.setFixedHeight(12)
        b, s = t["background"], t["accent"]["stops"]
        grad = s[0] if len(s) == 1 else f"qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {s[0]}, stop:1 {s[-1]})"
        strip.setStyleSheet("border-radius: 3px; background: qlineargradient(x1:0,y1:0,x2:1,y2:0,"
                            f" stop:0 {b['ground']}, stop:0.25 {b['ground']}, stop:0.2501 {b['panel']}, stop:0.5 {b['panel']},"
                            f" stop:0.5001 {b['raised']}, stop:0.65 {b['raised']}, stop:0.6501 {s[0]}, stop:1 {s[-1]});")
        lay.addWidget(strip)
        row = QHBoxLayout()
        row.setSpacing(4)
        nm = QLabel(f"<b>{t['name']}</b> <span style='color:{C['sec']}'>· {t['font']['family']}</span>")
        nm.setTextFormat(Qt.RichText)
        row.addWidget(nm, 1)
        if t["name"] != "Default":
            more = QToolButton()
            more.setIcon(icon("edit", C["sec"], 14))
            more.setToolTip("Rename, duplicate, export or delete")
            more.setPopupMode(QToolButton.InstantPopup)
            m = QMenu(more)
            if active and page.app.edited():
                m.addAction(icon("check"), "Update with the current look", lambda: page.update_theme(self.name))
            m.addAction(icon("edit"), "Rename…", lambda: page.rename_theme(self.name))
            m.addAction(icon("copy"), "Duplicate", lambda: page.duplicate_theme(self.name))
            m.addAction(icon("export"), "Export…", lambda: page.export_theme(self.name))
            m.addSeparator()
            m.addAction(icon("trash", C["danger"]), "Delete", lambda: page.delete_theme(self.name))
            more.setMenu(m)
            row.addWidget(more)
        lay.addLayout(row)
        if active and page.app.edited():
            ed = label("Edited · Update saves the changes", "faint")
            ed.setStyleSheet("font-size: 11px;")
            lay.addWidget(ed)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit(self.name)


class LookPage(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("page")
        self.app = ap.get_appearance()
        self.t = copy.deepcopy(self.app.current)
        self._apply_timer = QTimer(self, singleShot=True, interval=90, timeout=self._apply_now)
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 12)
        root.setSpacing(8)
        head, self.desc = page_header("Appearance", "How Nirvash looks. Changes apply straight away; save them as a "
                                                    "theme to keep them or use them in your other apps")
        root.addLayout(head)
        body = QHBoxLayout()
        body.setSpacing(8)
        # themes
        tp = panel()
        tp.setFixedWidth(250)
        tl = QVBoxLayout(tp)
        tl.setContentsMargins(8, 8, 8, 8)
        tl.setSpacing(6)
        tl.addWidget(label("Themes", "cap"))
        self.theme_list = QVBoxLayout()
        self.theme_list.setSpacing(6)
        tl.addLayout(self.theme_list)
        tl.addStretch(1)
        save = button("Save as theme…", "plus")
        save.clicked.connect(self.save_as)
        tl.addWidget(save)
        r = QHBoxLayout()
        imp = button("Import…", "download")
        imp.clicked.connect(self.import_theme)
        exp = button("Export…", "export")
        exp.clicked.connect(lambda: self.export_theme(None))
        r.addWidget(imp)
        r.addWidget(exp)
        tl.addLayout(r)
        body.addWidget(tp)
        # settings
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.NoFrame)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        inner.setObjectName("transparent")
        self.form = QVBoxLayout(inner)
        self.form.setContentsMargins(0, 0, 4, 0)
        self.form.setSpacing(8)
        sc.setWidget(inner)
        body.addWidget(sc, 1)
        # preview
        pv = panel()
        pv.setFixedWidth(300)
        self._build_preview(pv)
        body.addWidget(pv)
        self.preview_panel = pv
        root.addLayout(body, 1)
        self._build_form()
        self._fill_themes()
        self._sync()

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        # on a small window the settings need the room more than the preview (the whole app is the preview anyway)
        self.preview_panel.setVisible(self.width() >= 1180)

    # ---- building ------------------------------------------------------------------
    def _section(self, title: str, sub: str = "") -> QVBoxLayout:
        p = panel()
        l = QVBoxLayout(p)
        l.setContentsMargins(12, 10, 12, 12)
        l.setSpacing(8)
        h = QHBoxLayout()
        h.addWidget(label(title, "h2"))
        if sub:
            s = label(sub, "faint")
            h.addWidget(s, 1)
        else:
            h.addStretch(1)
        l.addLayout(h)
        self.form.addWidget(p)
        return l

    def _row(self, l: QVBoxLayout, name: str) -> QHBoxLayout:
        r = QHBoxLayout()
        r.setSpacing(8)
        lb = label(name, "cap")
        lb.setFixedWidth(88)
        r.addWidget(lb)
        l.addLayout(r)
        return r

    def _build_form(self) -> None:
        # Background
        l = self._section("Background", "Every colour can be changed below")
        self.bg_swatches: dict[str, Swatch] = {}
        for title, keys in GROUPS:
            l.addWidget(label(title, "faint"))
            w, fl = flow_row(6)
            for k in keys:
                s = Swatch("bg", k)
                s.clicked.connect(lambda _=False, k=k: self.set_background(k))
                fl.addWidget(s)
                self.bg_swatches[k] = s
            l.addWidget(w)
        r = self._row(l, "Scene")
        self.scene = Segmented([("none", "Plain"), ("starfield", "Starfield")])
        self.scene.changed.connect(self._scene_changed)
        r.addWidget(self.scene)
        r.addSpacing(16)
        r.addWidget(label("See-through", "cap"))
        self.opacity = QSlider(Qt.Horizontal)
        self.opacity.setRange(40, 100)
        self.opacity.setFixedWidth(160)
        self.opacity.setToolTip("How solid the panels are over the stars")
        self.opacity.valueChanged.connect(self._opacity_changed)
        r.addWidget(self.opacity)
        self.opacity_lbl = label("", "muted")
        r.addWidget(self.opacity_lbl)
        r.addStretch(1)
        self.edit_btn = button("Edit colours", "edit", "ghost")
        self.edit_btn.setCheckable(True)
        self.edit_btn.toggled.connect(lambda on: self.colours.setVisible(on))
        l.addWidget(self.edit_btn, 0, Qt.AlignLeft)
        self.colours = QWidget()
        self.colours.setObjectName("transparent")
        g = QGridLayout(self.colours)
        g.setContentsMargins(0, 0, 0, 0)
        g.setHorizontalSpacing(8)
        g.setVerticalSpacing(5)
        self.col_btns: dict[str, tuple[ColorButton, QLineEdit]] = {}
        keys = [("background", k, ap.BG_LABELS[k]) for k in ap.BG_KEYS] + \
               [("states", k, ap.STATE_LABELS[k]) for k in ap.STATE_KEYS]
        for i, (grp, k, name) in enumerate(keys):
            cb = ColorButton(self.t[grp][k])
            hx = QLineEdit(self.t[grp][k])
            hx.setObjectName("monoField")
            hx.setFixedWidth(90)
            hx.setStyleSheet('font-family: "JetBrains Mono", Consolas;')
            cb.picked.connect(lambda c, grp=grp, k=k: self._set_color(grp, k, c))
            hx.editingFinished.connect(lambda grp=grp, k=k, hx=hx: self._set_color(grp, k, hx.text().strip()))
            row, col = divmod(i, 2)
            g.addWidget(label(name, "muted"), row, col * 3)
            g.addWidget(cb, row, col * 3 + 1)
            g.addWidget(hx, row, col * 3 + 2)
            self.col_btns[f"{grp}.{k}"] = (cb, hx)
        g.setColumnStretch(2, 1)
        g.setColumnStretch(5, 1)
        self.colours.setVisible(False)
        l.addWidget(self.colours)

        # Accent
        l = self._section("Accent", "Main buttons, progress bars, ticks and the selected item")
        w, fl = flow_row(6)
        self.acc_swatches: dict[str, Swatch] = {}
        for k in ap.ACCENTS:
            s = Swatch("acc", k)
            s.clicked.connect(lambda _=False, k=k: self.set_accent(k))
            fl.addWidget(s)
            self.acc_swatches[k] = s
        l.addWidget(w)
        r = self._row(l, "Colours")
        self.stop_row = QHBoxLayout()
        self.stop_row.setSpacing(6)
        r.addLayout(self.stop_row)
        r.addStretch(1)
        r = self._row(l, "Direction")
        self.angle = QSlider(Qt.Horizontal)
        self.angle.setRange(0, 360)
        self.angle.setFixedWidth(200)
        self.angle.valueChanged.connect(self._angle_changed)
        r.addWidget(self.angle)
        self.angle_lbl = label("", "muted")
        r.addWidget(self.angle_lbl)
        r.addStretch(1)
        r = self._row(l, "Text on it")
        self.on_acc = Segmented([("auto", "Auto"), ("dark", "Dark"), ("light", "Light")])
        self.on_acc.changed.connect(self._on_acc_changed)
        r.addWidget(self.on_acc)
        self.contrast_lbl = label("", "muted")
        r.addWidget(self.contrast_lbl)
        r.addStretch(1)

        # Font
        l = self._section("Font")
        r = self._row(l, "Font")
        self.font_box = QComboBox()
        self.font_box.setMinimumWidth(200)
        self.font_box.activated.connect(self._font_changed)
        r.addWidget(self.font_box)
        add = button("Add font…", "plus")
        add.setToolTip("Pick the font's files (all its weights at once)")
        add.clicked.connect(self.add_font)
        r.addWidget(add)
        r.addStretch(1)
        r = self._row(l, "Text size")
        self.size = Segmented([(n, f"{n}") for n in ap.TEXT_SIZES])
        self.size.changed.connect(lambda v: self._set(("font", "size"), int(v)))
        r.addWidget(self.size)
        r.addStretch(1)

        # Shape
        l = self._section("Shape and spacing")
        r = self._row(l, "Corners")
        self.corners = Segmented([(k, v[0]) for k, v in ap.CORNERS.items()])
        self.corners.changed.connect(self._corners_preset)
        r.addWidget(self.corners)
        r.addStretch(1)
        r = self._row(l, "")
        self.rc = self._mini_slider(r, "Controls", 0, 16, ("corners", "control"))
        r.addSpacing(12)
        self.rp = self._mini_slider(r, "Panels", 0, 24, ("corners", "panel"))
        r.addStretch(1)
        r = self._row(l, "Density")
        self.density = Segmented([(k, v[0]) for k, v in ap.DENSITY.items()])
        self.density.changed.connect(self._density_preset)
        r.addWidget(self.density)
        r.addStretch(1)
        r = self._row(l, "")
        self.ch = self._mini_slider(r, "Controls", 24, 44, ("density", "control"))
        r.addSpacing(12)
        self.rh = self._mini_slider(r, "Rows", 20, 40, ("density", "row"))
        r.addStretch(1)
        r = self._row(l, "Buttons")
        self.btn_style = Segmented([("flat", "Flat"), ("glow", "Glow")])
        self.btn_style.changed.connect(lambda v: self._set(("buttons", "style"), v))
        r.addWidget(self.btn_style)
        r.addWidget(label("Glow adds a soft accent glow under the main buttons.", "faint"))
        r.addStretch(1)
        reset = button("Reset to default", "refresh", "ghost")
        reset.setToolTip("Starfield, see-through · Teal → Violet · Inter · Standard · Compact · Flat. Saved themes stay.")
        reset.clicked.connect(self.reset)
        self.form.addWidget(reset, 0, Qt.AlignLeft)
        self.form.addStretch(1)

    def _mini_slider(self, r: QHBoxLayout, name: str, lo: int, hi: int, key: tuple) -> QSlider:
        r.addWidget(label(name, "faint"))
        s = QSlider(Qt.Horizontal)
        s.setRange(lo, hi)
        s.setFixedWidth(90)
        v = label("", "muted")
        v.setFixedWidth(34)
        s.valueChanged.connect(lambda x, v=v: (v.setText(f"{x} px"), self._set(key, x)))
        r.addWidget(s)
        r.addWidget(v)
        s.value_lbl = v
        return s

    def _build_preview(self, pv: QFrame) -> None:
        l = QVBoxLayout(pv)
        l.setContentsMargins(12, 10, 12, 12)
        l.setSpacing(10)
        l.addWidget(label("Preview", "cap"))
        w, fl = flow_row(6)
        fl.addWidget(button("Find music", "sparkle", "primary"))
        fl.addWidget(button("Add folder", "plus"))
        fl.addWidget(button("Cancel", None, "ghost"))
        l.addWidget(w)
        e = QLineEdit("Blue Bird")
        l.addWidget(e)
        r = QHBoxLayout()
        cb = QComboBox()
        cb.addItems(["Chain a second song", "Loop the song"])
        r.addWidget(cb, 1)
        from PySide6.QtWidgets import QCheckBox
        ck = QCheckBox("Smart start")
        ck.setChecked(True)
        r.addWidget(ck)
        l.addLayout(r)
        r = QHBoxLayout()
        pb = QProgressBar()
        pb.setRange(0, 100)
        pb.setValue(58)
        r.addWidget(pb, 1)
        r.addWidget(label("58%"))
        l.addLayout(r)
        w, fl = flow_row(5)
        for t, c in (("Approved", C["ok"]), ("Check", C["warn"]), ("Failed", C["danger"]), ("Chill", "#2fe0c0"),
                     ("Hype", "#ff6fb1")):
            fl.addWidget(chip(t, c))
        l.addWidget(w)
        r = QHBoxLayout()
        r.addWidget(button("Remove", "trash", "dangerGhost"))
        r.addStretch(1)
        l.addLayout(r)
        self.preview_note = label("", "faint", wrap=True)
        l.addWidget(self.preview_note)
        l.addStretch(1)

    # ---- themes list -------------------------------------------------------------------
    def _fill_themes(self) -> None:
        clear_layout(self.theme_list)
        allt = [ap.normalize(None)] + self.app.saved
        for t in allt:
            c = ThemeCard(t, t["name"] == self.app.active, self)
            c.clicked.connect(self.use_theme)
            self.theme_list.addWidget(c)

    def use_theme(self, name: str) -> None:
        t = self.app.theme(name)
        if t is None:
            return
        self.t = t
        self.app.set_current(t, active=name)
        self._apply_now(save=False)
        self._sync()
        self._fill_themes()

    def save_as(self) -> None:
        name = dialogs.ask_text(self, "Save as theme", "Name for this look:", "My theme", ok="Save theme")
        if name:
            self.app.save_as(name)
            self._fill_themes()
            hub.toast.emit(f"Saved the theme {self.app.active}.", "ok")

    def update_theme(self, name: str) -> None:
        self.app.update(name)
        self._fill_themes()
        hub.toast.emit(f"Updated {name}.", "ok")

    def rename_theme(self, name: str) -> None:
        new = dialogs.ask_text(self, "Rename theme", "New name:", name, ok="Rename")
        if new and new != name:
            self.app.rename(name, new)
            self._fill_themes()

    def duplicate_theme(self, name: str) -> None:
        self.app.duplicate(name)
        self._fill_themes()

    def delete_theme(self, name: str) -> None:
        if dialogs.ask(self, "Delete theme", f"Delete the theme “{name}”? The current look doesn't change.",
                       "Delete theme", danger=True):
            self.app.delete(name)
            self._fill_themes()

    def export_theme(self, name: str | None) -> None:
        t = self.app.theme(name) if name else dict(self.app.current, name=self.app.active or "My theme")
        if t is None:
            return
        fn = (t["name"] or "theme").replace("/", "-").replace("\\", "-")
        path, _ = QFileDialog.getSaveFileName(self, "Export theme", str(Path.home() / f"{fn}.theme.json"),
                                              "Theme files (*.theme.json *.json)")
        if path:
            self.app.export_file(t, Path(path))
            hub.toast.emit(f"Exported {Path(path).name}. Import it in any of your apps.", "ok")

    def import_theme(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import theme", str(Path.home()), "Theme files (*.json)")
        if not path:
            return
        try:
            name = self.app.import_file(Path(path))
        except Exception as e:
            dialogs.info(self, "Couldn't import", f"{Path(path).name} isn't a theme Nirvash can read: {e}")
            return
        self.use_theme(name)
        hub.toast.emit(f"Imported {name}.", "ok")

    # ---- changes -----------------------------------------------------------------------
    def _set(self, key: tuple, value) -> None:
        if getattr(self, "_syncing", False):
            return
        d = self.t
        for k in key[:-1]:
            d = d[k]
        if d.get(key[-1]) == value:
            return
        d[key[-1]] = value
        self._changed()

    def _changed(self) -> None:
        self.t = ap.normalize(self.t)
        self.app.set_current(self.t, active=self.app.active, save=False)
        self._apply_timer.start()
        self._sync(light=True)

    def _apply_now(self, save: bool = True) -> None:
        if save:
            self.app.save()
        theme.apply(QApplication.instance(), self.app.current)
        hub.themeChanged.emit()
        self._fill_themes()

    def set_background(self, key: str) -> None:
        self.t = ap.with_background(self.t, key)
        self._changed()
        self._sync()

    def _scene_changed(self, v) -> None:
        sc = dict(self.t["scene"])
        sc["image"] = v
        if v == "starfield" and sc.get("opacity", 100) >= 100:
            sc["opacity"] = 66
        self.t["scene"] = sc
        self._changed()

    def _opacity_changed(self, v: int) -> None:
        self.opacity_lbl.setText(f"{100 - v}% see-through" if v < 100 else "Solid")
        if getattr(self, "_syncing", False):
            return
        self.t["scene"] = dict(self.t["scene"], opacity=v)
        self._changed()

    def _set_color(self, grp: str, k: str, c: str) -> None:
        if not ap._HEX.match(c or ""):
            self._sync()
            return
        self._set((grp, k), ap._color(c, c))

    def set_accent(self, key: str) -> None:
        self.t["accent"] = dict(self.t["accent"], stops=list(ap.ACCENTS[key]["stops"]))
        self._changed()
        self._fill_stops()

    def _fill_stops(self) -> None:
        clear_layout(self.stop_row)
        stops = self.t["accent"]["stops"]
        for i, s in enumerate(stops):
            cb = ColorButton(s)
            cb.picked.connect(lambda c, i=i: self._stop_color(i, c))
            self.stop_row.addWidget(cb)
            if len(stops) > 1:
                x = QToolButton()
                x.setIcon(icon("x", C["sec"], 12))
                x.setToolTip("Remove this colour")
                x.clicked.connect(lambda _=False, i=i: self._stop_remove(i))
                self.stop_row.addWidget(x)
        if len(stops) < 4:
            add = QToolButton()
            add.setIcon(icon("plus", C["acc"], 14))
            add.setToolTip("Add a colour (up to 4)")
            add.clicked.connect(self._stop_add)
            self.stop_row.addWidget(add)

    def _stop_color(self, i: int, c: str) -> None:
        st = list(self.t["accent"]["stops"])
        st[i] = c
        self.t["accent"] = dict(self.t["accent"], stops=st)
        self._changed()

    def _stop_remove(self, i: int) -> None:
        st = list(self.t["accent"]["stops"])
        if len(st) > 1:
            st.pop(i)
            self.t["accent"] = dict(self.t["accent"], stops=st)
            self._changed()
            self._fill_stops()

    def _stop_add(self) -> None:
        st = list(self.t["accent"]["stops"])
        st.append(st[-1])
        self.t["accent"] = dict(self.t["accent"], stops=st[:4])
        self._changed()
        self._fill_stops()

    def _angle_changed(self, v: int) -> None:
        self.angle_lbl.setText(f"{v}°")
        self._set(("accent", "angle"), v)

    def _on_acc_changed(self, v) -> None:
        self._set(("accent", "onAccent"), v)

    def _font_changed(self) -> None:
        fam = self.font_box.currentData()
        if fam:
            fid = theme.font_id(fam)
            if fid:
                theme.load_font(fid)
            self._set(("font", "family"), fam)

    def _corners_preset(self, k) -> None:
        _n, c, p = ap.CORNERS[k]
        self.t["corners"] = {"control": c, "panel": p}
        self._changed()
        self._sync()

    def _density_preset(self, k) -> None:
        _n, c, r = ap.DENSITY[k]
        self.t["density"] = {"control": c, "row": r}
        self._changed()
        self._sync()

    def reset(self) -> None:
        self.t = ap.normalize(None)
        self.app.set_current(self.t, active="Default")
        self._apply_now()
        self._sync()

    def add_font(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Pick the font's files (all weights)", str(Path.home()),
                                                "Fonts (*.ttf *.otf)")
        if not files:
            return
        fam, _w = ap.guess_font([Path(f) for f in files])
        name = dialogs.ask_text(self, "Add font", f"{len(files)} file{'s' if len(files) != 1 else ''}. "
                                "What's the font called?", fam, ok="Add font")
        if not name:
            return
        entry = ap.add_user_font([Path(f) for f in files], name)
        theme.load_font(entry["id"])
        self._fill_fonts()
        self.font_box.setCurrentIndex(max(0, self.font_box.findData(name)))
        self._set(("font", "family"), name)
        hub.toast.emit(f"Added {name}.", "ok")

    # ---- showing the current values -----------------------------------------------------
    def _fill_fonts(self) -> None:
        from PySide6.QtGui import QFontDatabase
        theme.load_all_fonts()
        self.font_box.blockSignals(True)
        self.font_box.clear()
        fams = set(QFontDatabase.families())
        for f in ap.bundled_fonts():
            if f.get("mono"):
                continue
            self.font_box.addItem(f["family"], f["family"])
            self.font_box.setItemData(self.font_box.count() - 1, QFont(f["family"], 10), Qt.FontRole)
        for f in ap.user_fonts():
            self.font_box.addItem(f"{f['family']}  (added by you)", f["family"])
            self.font_box.setItemData(self.font_box.count() - 1, QFont(f["family"], 10), Qt.FontRole)
        for fam in ap.SYSTEM_FONTS:
            ok = fam in fams
            self.font_box.addItem(f"{fam}  ({'installed' if ok else 'not installed'})", fam if ok else None)
            if ok:
                self.font_box.setItemData(self.font_box.count() - 1, QFont(fam, 10), Qt.FontRole)
        i = self.font_box.findData(self.t["font"]["family"])
        self.font_box.setCurrentIndex(max(0, i))
        self.font_box.blockSignals(False)

    def _sync(self, light: bool = False) -> None:
        """Show the current values without rebuilding anything."""
        self._syncing = True
        try:
            t = self.t
            d = ap.describe(t)
            for k, s in self.bg_swatches.items():
                s.setChecked(k == d["background"])
            for k, s in self.acc_swatches.items():
                s.setChecked(k == d["accent"])
            self.scene.set_value(t["scene"]["image"])
            self.opacity.setValue(t["scene"]["opacity"])
            self.opacity.setEnabled(t["scene"]["image"] != "none")
            self.opacity_lbl.setText(f"{100 - t['scene']['opacity']}% see-through" if t["scene"]["opacity"] < 100 else "Solid")
            for key, (cb, hx) in self.col_btns.items():
                grp, k = key.split(".")
                cb.set_color(t[grp][k])
                if not hx.hasFocus():
                    hx.setText(t[grp][k])
            self.angle.setValue(t["accent"]["angle"])
            self.angle_lbl.setText(f"{t['accent']['angle']}°")
            self.on_acc.set_value(t["accent"]["onAccent"])
            cr = ap.accent_contrast(t)
            self.contrast_lbl.setText(f"{cr:.1f} : 1" + ("" if cr >= 4.5 else " · hard to read"))
            self.contrast_lbl.setStyleSheet(f"color: {C['ok'] if cr >= 4.5 else C['warn']};")
            self.size.set_value(t["font"]["size"])
            self.corners.set_value(d["corners"])
            if d["corners"] is None:
                for b in self.corners.buttons.values():
                    b.setChecked(False)
            self.density.set_value(d["density"])
            if d["density"] is None:
                for b in self.density.buttons.values():
                    b.setChecked(False)
            for s, v in ((self.rc, t["corners"]["control"]), (self.rp, t["corners"]["panel"]),
                         (self.ch, t["density"]["control"]), (self.rh, t["density"]["row"])):
                s.setValue(v)
                s.value_lbl.setText(f"{v} px")
            self.btn_style.set_value(t["buttons"]["style"])
            if not light:
                self._fill_stops()
                self._fill_fonts()
            self.desc.setText(f"{d['text']} · {'Glow' if t['buttons']['style'] == 'glow' else 'Flat'} buttons")
            lt = ap.is_light(t)
            self.preview_note.setText("Light backgrounds: some icons pick up the new text colour after Nirvash "
                                      "restarts." if lt else "")
        finally:
            self._syncing = False
