"""Song library: browse, listen, favourite, categorise, delete."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from PySide6.QtCore import (QAbstractTableModel, QItemSelectionModel, QModelIndex, QRect, QSize,
                            QSortFilterProxyModel, Qt, QTimer)
from PySide6.QtGui import QColor, QFont, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QColorDialog, QDialog, QDialogButtonBox, QFormLayout, QFrame,
                               QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMenu, QMessageBox, QScrollArea, QSizePolicy, QSplitter,
                               QStyledItemDelegate, QTableView, QTextBrowser, QToolButton, QVBoxLayout, QWidget)

from ..analysis.labels import top_moods
from ..core.db import get_db
from ..core.settings import get_settings
from ..jobs import scan as scanjob
from ..jobs.library_files import to_recycle_bin
from ..media.ff import fmt_time
from . import theme
from .hub import hub
from .player import Player
from .theme import C, CATEGORY_COLOURS, icon
from .widgets import (PlayerBar, button, card, chip, clear_layout, cover_pixmap, hline, label, open_in_explorer, tool)

COLS = ["", "Title", "Artist", "Length", "Full song", "BPM", "Genre", "Mood", "Categories", "Status"]
STATUS = {
    "pending": ("Identifying…", C["violet"]),
    "not_found": ("Not identified", C["faint"]),
    "error": ("Lookup failed", C["amber"]),
}
ROLE_SORT = Qt.UserRole + 1
ROLE_ROW = Qt.UserRole + 2


def song_row(r) -> dict:
    mood = json.loads(r["mood"]) if r["mood"] else None
    return {
        "id": r["id"], "path": r["file_path"],
        "title": r["title"] or Path(r["file_path"]).stem, "artist": r["artist"] or "",
        "identified": bool(r["title"]),
        "duration": r["duration"] or 0.0, "full": r["full_length"], "bpm": r["bpm"], "genre": r["genre"] or "",
        "mood": mood, "mood_txt": ", ".join(top_moods(mood)),
        "favourite": bool(r["favourite"]), "partial": bool(r["partial"]), "checked": bool(r["checked"]),
        "status": r["id_status"], "added": r["added_at"], "album": r["album"] or "", "year": r["year"] or "",
        "energy": r["energy"],
    }


class LibraryModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self.rows: list[dict] = []
        self.cats: dict[int, set[int]] = {}
        self.cat_info: dict[int, tuple[str, str]] = {}
        self.index_of: dict[int, int] = {}

    def reload(self) -> None:
        db = get_db()
        self.beginResetModel()
        self.rows = [song_row(r) for r in db.songs()]
        self.cats = db.all_song_categories()
        self.cat_info = {c["id"]: (c["name"], c["color"] or C["violet"]) for c in db.categories()}
        self.index_of = {r["id"]: i for i, r in enumerate(self.rows)}
        self.endResetModel()

    def refresh_song(self, sid: int) -> bool:
        db = get_db()
        r = db.song(sid)
        if r is None:
            if sid in self.index_of:
                self.reload()
            return True
        self.cats[sid] = db.song_category_ids(sid)
        if sid not in self.index_of:
            self.beginInsertRows(QModelIndex(), 0, 0)
            self.rows.insert(0, song_row(r))
            self.index_of = {x["id"]: i for i, x in enumerate(self.rows)}
            self.endInsertRows()
            return True
        i = self.index_of[sid]
        self.rows[i] = song_row(r)
        self.dataChanged.emit(self.index(i, 0), self.index(i, len(COLS) - 1))
        return False

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(COLS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return COLS[section]
        return None

    def data(self, idx: QModelIndex, role=Qt.DisplayRole):
        if not idx.isValid():
            return None
        r = self.rows[idx.row()]
        c = idx.column()
        if role == ROLE_ROW:
            return r
        if role == Qt.DisplayRole:
            if c == 1:
                return r["title"]
            if c == 2:
                return r["artist"]
            if c == 3:
                return fmt_time(r["duration"])
            if c == 4:
                return fmt_time(r["full"]) if r["full"] else ""
            if c == 5:
                return f"{r['bpm']:.0f}" if r["bpm"] else ""
            if c == 6:
                return r["genre"]
            if c == 7:
                return r["mood_txt"]
            return None
        if role == ROLE_SORT:
            return [r["favourite"], r["title"].lower(), r["artist"].lower(), r["duration"], r["full"] or 0,
                    r["bpm"] or 0, r["genre"].lower(), r["mood_txt"], len(self.cats.get(r["id"], ())),
                    r["status"]][c]
        if role == Qt.ForegroundRole and c in (2, 3, 4, 5, 6, 7):
            return QColor(C["muted"])
        if role == Qt.ToolTipRole and c == 1:
            return r["path"]
        if role == Qt.DecorationRole and c == 0:
            return icon("star_on", C["amber"], 16) if r["favourite"] else icon("star", C["faint"], 16)
        if role == Qt.TextAlignmentRole and c in (3, 4, 5):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None


class LibraryFilter(QSortFilterProxyModel):
    def __init__(self, model: LibraryModel):
        super().__init__()
        self.setSourceModel(model)
        self.m = model
        self.setSortRole(ROLE_SORT)
        self.text = ""
        self.view = "all"       # all | fav | partial | unid
        self.category: int | None = None

    def set(self, **kw) -> None:
        for k, v in kw.items():
            setattr(self, k, v)
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):
        r = self.m.rows[row]
        if self.view == "fav" and not r["favourite"]:
            return False
        if self.view == "partial" and not (r["partial"] and not r["checked"]):
            return False
        if self.view == "unid" and r["identified"]:
            return False
        if self.category is not None and self.category not in self.m.cats.get(r["id"], set()):
            return False
        if self.text:
            hay = " ".join([r["title"], r["artist"], r["genre"], r["album"], r["mood_txt"], Path(r["path"]).name]).lower()
            return all(w in hay for w in self.text.lower().split())
        return True


class ChipDelegate(QStyledItemDelegate):
    """Paints the Categories and Status columns as coloured pills."""

    def __init__(self, model: LibraryModel, kind: str):
        super().__init__()
        self.m, self.kind = model, kind

    def paint(self, p: QPainter, opt, idx):
        super().paint(p, opt, idx)
        r = idx.data(ROLE_ROW)
        if r is None:
            return
        chips: list[tuple[str, str]] = []
        if self.kind == "cats":
            for cid in sorted(self.m.cats.get(r["id"], ())):
                if cid in self.m.cat_info:
                    chips.append(self.m.cat_info[cid])
        else:
            if r["partial"] and not r["checked"]:
                chips.append(("Partial — check", C["pink"]))
            if r["status"] in STATUS:
                chips.append(STATUS[r["status"]])
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        f = QFont(opt.font)
        f.setPointSizeF(8.3)
        f.setBold(True)
        p.setFont(f)
        x = opt.rect.x() + 6
        h = 18
        y = opt.rect.y() + (opt.rect.height() - h) // 2
        for text, col in chips:
            w = p.fontMetrics().horizontalAdvance(text) + 16
            if x + w > opt.rect.right() - 2:
                break
            qc = QColor(col)
            bg = QColor(qc)
            bg.setAlpha(40)
            p.setPen(Qt.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(QRect(x, y, w, h), 9, 9)
            p.setPen(qc)
            p.drawText(QRect(x, y, w, h), Qt.AlignCenter, text)
            x += w + 5
        p.restore()


class EditInfoDialog(QDialog):
    def __init__(self, row, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit song info")
        self.setMinimumWidth(420)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.fields = {}
        for key, name in (("title", "Title"), ("artist", "Artist"), ("album", "Album"), ("genre", "Genre"),
                          ("year", "Year")):
            e = QLineEdit(row[key] or "")
            form.addRow(name, e)
            self.fields[key] = e
        lay.addLayout(form)
        note = label("Saved info isn't replaced by later lookups.", "muted")
        lay.addWidget(note)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def values(self) -> dict:
        return {k: (e.text().strip() or None) for k, e in self.fields.items()}


def strip_lrc(text: str) -> str:
    return "\n".join(re.sub(r"^\s*(\[[0-9:.]+\])+\s*", "", line) for line in text.splitlines())


class DetailPanel(QScrollArea):
    def __init__(self, page: "LibraryPage"):
        super().__init__()
        self.page = page
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setMinimumWidth(330)
        self.setMaximumWidth(420)
        inner = QWidget()
        inner.setObjectName("page")
        self.setWidget(inner)
        self.lay = QVBoxLayout(inner)
        self.lay.setContentsMargins(4, 0, 4, 0)
        self.lay.setSpacing(10)
        self.sid: int | None = None
        self.show_empty()

    def show_empty(self) -> None:
        clear_layout(self.lay)
        self.sid = None
        box = card()
        b = QVBoxLayout(box)
        b.setContentsMargins(20, 30, 20, 30)
        ic = QLabel()
        ic.setPixmap(theme.pixmap("library", 40, C["faint"]))
        ic.setAlignment(Qt.AlignCenter)
        b.addWidget(ic)
        t = label("Pick a song to see its details", "muted")
        t.setAlignment(Qt.AlignCenter)
        b.addWidget(t)
        self.lay.addWidget(box)
        self.lay.addStretch(1)

    def show_song(self, sid: int) -> None:
        db = get_db()
        r = db.song(sid)
        if r is None:
            self.show_empty()
            return
        self.sid = sid
        clear_layout(self.lay)
        top = card()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(16, 16, 16, 16)
        tl.setSpacing(8)
        cover = QLabel()
        cpath = get_settings().cache("covers") / f"{sid}.jpg"
        cover.setPixmap(cover_pixmap(str(cpath), 200, r["title"] or r["file_path"]))
        cover.setAlignment(Qt.AlignCenter)
        tl.addWidget(cover)
        title = label(r["title"] or Path(r["file_path"]).stem, "h2", wrap=True)
        tl.addWidget(title)
        if r["artist"]:
            tl.addWidget(label(r["artist"], "muted", wrap=True))
        chips = QHBoxLayout()
        chips.setSpacing(5)
        if r["partial"] and not r["checked"]:
            chips.addWidget(chip("Partial — check", C["pink"]))
        if r["genre"]:
            chips.addWidget(chip(r["genre"].split(",")[0], C["teal"]))
        if r["bpm"]:
            chips.addWidget(chip(f"{r['bpm']:.0f} BPM", C["blue"]))
        mood = json.loads(r["mood"]) if r["mood"] else None
        for m in top_moods(mood):
            chips.addWidget(chip(m, C["violet"]))
        chips.addStretch(1)
        tl.addLayout(chips)
        # Category chips
        cats = db.song_category_ids(sid)
        if cats:
            cl = QHBoxLayout()
            cl.setSpacing(5)
            for c in db.categories():
                if c["id"] in cats:
                    cl.addWidget(chip(c["name"], c["color"] or C["violet"], filled=True))
            cl.addStretch(1)
            tl.addLayout(cl)
        # Actions
        acts = QHBoxLayout()
        acts.setSpacing(6)
        fav = button("Favourite" if not r["favourite"] else "Favourited", "star_on" if r["favourite"] else "star",
                     color=C["amber"] if r["favourite"] else C["text"])
        fav.clicked.connect(lambda: self.page.toggle_fav([sid]))
        acts.addWidget(fav)
        catb = button("Categories", "tag")
        catb.clicked.connect(lambda: self.page.category_menu([sid], catb))
        acts.addWidget(catb)
        more = tool("edit", "More", C["text"])
        more.setPopupMode(QToolButton.InstantPopup)
        mm = QMenu(more)
        mm.addAction(icon("edit"), "Edit info…", lambda: self.page.edit_info(sid))
        mm.addAction(icon("refresh"), "Look it up again", lambda: self.page.reidentify([sid]))
        mm.addAction(icon("folder"), "Show file", lambda: open_in_explorer(r["file_path"]))
        mm.addSeparator()
        mm.addAction(icon("trash", C["red"]), "Delete (Recycle Bin)", lambda: self.page.delete([sid]))
        more.setMenu(mm)
        acts.addWidget(more)
        acts.addStretch(1)
        tl.addLayout(acts)
        if r["partial"] and not r["checked"]:
            ok = button("Sounds fine — mark checked", "check", color=C["teal"])
            ok.clicked.connect(lambda: self.page.mark_checked([sid]))
            tl.addWidget(ok)
        self.lay.addWidget(top)

        info = card()
        il = QFormLayout(info)
        il.setContentsMargins(16, 12, 16, 12)
        il.setHorizontalSpacing(14)
        il.setVerticalSpacing(6)

        def row(name, val):
            if val:
                k = label(name, "muted")
                v = label(str(val), wrap=True)
                v.setTextInteractionFlags(Qt.TextSelectableByMouse)
                il.addRow(k, v)

        full = r["full_length"]
        row("Clip length", fmt_time(r["duration"]))
        row("Full song", fmt_time(full) if full else ("Unknown" if r["title"] else ""))
        if r["clip_offset"] is not None and full:
            row("Clip starts", f"{fmt_time(r['clip_offset'])} into the song")
        row("Album", r["album"])
        row("Released", r["year"])
        codec = (r["codec"] or "").upper()
        if r["bitrate"]:
            codec += f" · {r['bitrate'] // 1000} kbps"
        if r["sample_rate"]:
            codec += f" · {r['sample_rate'] / 1000:.1f} kHz"
        row("Audio", codec + " (copied, not re-encoded)")
        status_txt = {"found": f"Identified by {r['id_source'] or 'lookup'}", "pending": "Waiting to be identified",
                      "not_found": "Not found by Shazam / AcoustID", "error": "Lookup failed (will retry)"}
        row("Song info", ("Edited by you" if r["manual_info"] else status_txt.get(r["id_status"], "")))
        row("Added", time.strftime("%d %b %Y %H:%M", time.localtime(r["added_at"])))
        self.lay.addWidget(info)

        srcs = db.sources(sid)
        if srcs:
            sc = card()
            sl = QVBoxLayout(sc)
            sl.setContentsMargins(16, 12, 16, 12)
            sl.setSpacing(4)
            sl.addWidget(label(f"From {len(srcs)} video{'s' if len(srcs) > 1 else ''}", "h2"))
            for s in srcs[:12]:
                rowl = QHBoxLayout()
                name = label(Path(s["video_path"]).name)
                name.setToolTip(s["video_path"])
                name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
                rowl.addWidget(name, 1)
                span = label(f"{fmt_time(s['start'])}–{fmt_time(s['end'])}", "muted")
                rowl.addWidget(span)
                ob = tool("open", "Show video in its folder", C["muted"], 15)
                ob.clicked.connect(lambda _=False, p=s["video_path"]: open_in_explorer(p))
                rowl.addWidget(ob)
                sl.addLayout(rowl)
            if len(srcs) > 12:
                sl.addWidget(label(f"…and {len(srcs) - 12} more", "muted"))
            self.lay.addWidget(sc)

        lyr = r["lyrics_plain"] or (strip_lrc(r["lyrics_synced"]) if r["lyrics_synced"] else "")
        if lyr:
            lc = card()
            ll = QVBoxLayout(lc)
            ll.setContentsMargins(16, 12, 16, 12)
            head = QHBoxLayout()
            head.addWidget(label("Lyrics", "h2"))
            head.addStretch(1)
            tgl = button("Show")
            head.addWidget(tgl)
            ll.addLayout(head)
            tb = QTextBrowser()
            tb.setPlainText(lyr)
            tb.setMinimumHeight(260)
            tb.setVisible(False)
            ll.addWidget(tb)
            tgl.clicked.connect(lambda: (tb.setVisible(not tb.isVisible()), tgl.setText("Hide" if tb.isVisible() else "Show")))
            self.lay.addWidget(lc)
        self.lay.addStretch(1)


class LibraryPage(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("page")
        self.model = LibraryModel()
        self.proxy = LibraryFilter(self.model)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 14)
        root.setSpacing(12)

        head = QHBoxLayout()
        head.addWidget(label("Song library", "h1"))
        self.count_lbl = label("", "muted")
        head.addWidget(self.count_lbl)
        head.addStretch(1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search title, artist, genre, mood…")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(320)
        self.search.textChanged.connect(lambda t: (self.proxy.set(text=t.strip()), self._count()))
        head.addWidget(self.search)
        root.addLayout(head)

        split = QSplitter(Qt.Horizontal)
        split.setHandleWidth(12)
        # Sidebar
        side = QWidget()
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(8)
        self.views = QListWidget()
        self.views.setFixedHeight(4 * 36 + 10)
        for key, text, ic, col in (("all", "All songs", "library", C["teal"]), ("fav", "Favourites", "star_on", C["amber"]),
                                    ("partial", "Partial — check", "info", C["pink"]),
                                    ("unid", "Not identified", "scan", C["violet"])):
            it = QListWidgetItem(icon(ic, col, 16), text)
            it.setData(Qt.UserRole, key)
            it.setSizeHint(QSize(0, 34))
            self.views.addItem(it)
        self.views.setCurrentRow(0)
        self.views.currentItemChanged.connect(self._view_changed)
        sl.addWidget(self.views)
        ch = QHBoxLayout()
        ch.addWidget(label("Categories", "h2"))
        ch.addStretch(1)
        addc = tool("plus", "New category", C["teal"])
        addc.clicked.connect(self.new_category)
        ch.addWidget(addc)
        sl.addLayout(ch)
        self.cat_list = QListWidget()
        self.cat_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.cat_list.customContextMenuRequested.connect(self._cat_menu)
        self.cat_list.currentItemChanged.connect(self._cat_changed)
        sl.addWidget(self.cat_list, 1)
        tip = label("Right-click a category to rename, recolour or delete it.", "faint", wrap=True)
        sl.addWidget(tip)
        side.setMinimumWidth(190)
        side.setMaximumWidth(240)
        split.addWidget(side)

        # Table
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(1, Qt.AscendingOrder)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)
        hh = self.table.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(QHeaderView.Interactive)
        for i, w in enumerate((34, 200, 130, 56, 70, 46, 90, 110, 120, 128)):
            self.table.setColumnWidth(i, w)
        hh.setSectionResizeMode(0, QHeaderView.Fixed)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setMinimumSectionSize(30)
        hh.setContextMenuPolicy(Qt.CustomContextMenu)
        hh.customContextMenuRequested.connect(self._header_menu)
        for col in get_settings().get("ui.hidden_columns", [4]) or []:
            if 1 < col < len(COLS):
                self.table.setColumnHidden(col, True)
        self.table.setItemDelegateForColumn(8, ChipDelegate(self.model, "cats"))
        self.table.setItemDelegateForColumn(9, ChipDelegate(self.model, "status"))
        self.table.clicked.connect(self._clicked)
        self.table.doubleClicked.connect(lambda idx: self.play(self._sid(idx)))
        self.table.selectionModel().currentRowChanged.connect(lambda cur, _p: self._select(cur))
        split.addWidget(self.table)

        self.detail = DetailPanel(self)
        split.addWidget(self.detail)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setStretchFactor(2, 0)
        split.setSizes([210, 900, 360])
        root.addWidget(split, 1)

        self.player = Player(self, video=False)
        self.bar = PlayerBar(self.player)
        root.addWidget(self.bar)
        self.playing_sid: int | None = None

        for keys, fn in (("Space", self._space), ("Delete", lambda: self.delete(self.selected())),
                         ("F", lambda: self.toggle_fav(self.selected())), ("Return", lambda: self.play(self.current()))):
            sc = QShortcut(QKeySequence(keys), self.table)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(fn)
        sc = QShortcut(QKeySequence("Ctrl+F"), self)
        sc.activated.connect(self.search.setFocus)

        hub.songChanged.connect(self._song_changed)
        hub.songsReset.connect(self.reload)
        hub.categoriesChanged.connect(self._reload_categories)
        self._pending_refresh: set[int] = set()
        self._refresh_timer = QTimer(self, singleShot=True, interval=400, timeout=self._flush_refresh)
        self.reload()

    # ---- data ---------------------------------------------------------------
    def reload(self) -> None:
        cur = self.current()
        self.model.reload()
        self._reload_categories()
        self._count()
        if cur is not None:
            self.select_song(cur)
        elif self.detail.sid is not None:
            self.detail.show_empty()

    def _reload_categories(self) -> None:
        db = get_db()
        self.model.cats = db.all_song_categories()
        self.model.cat_info = {c["id"]: (c["name"], c["color"] or C["violet"]) for c in db.categories()}
        keep = self.proxy.category
        self.cat_list.blockSignals(True)
        self.cat_list.clear()
        counts: dict[int, int] = {}
        for cs in self.model.cats.values():
            for c in cs:
                counts[c] = counts.get(c, 0) + 1
        for c in db.categories():
            it = QListWidgetItem(f"{c['name']}   {counts.get(c['id'], 0)}")
            it.setData(Qt.UserRole, c["id"])
            it.setIcon(self._dot(c["color"] or C["violet"]))
            it.setSizeHint(QSize(0, 30))
            self.cat_list.addItem(it)
            if c["id"] == keep:
                self.cat_list.setCurrentItem(it)
        self.cat_list.blockSignals(False)
        if keep is not None and not any(self.cat_list.item(i).data(Qt.UserRole) == keep for i in range(self.cat_list.count())):
            self.proxy.set(category=None)
        self.table.viewport().update()
        if self.detail.sid is not None:
            self.detail.show_song(self.detail.sid)

    @staticmethod
    def _dot(color: str):
        from PySide6.QtGui import QIcon, QPixmap
        pm = QPixmap(28, 28)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor(color))
        p.setPen(Qt.NoPen)
        p.drawEllipse(7, 7, 14, 14)
        p.end()
        pm.setDevicePixelRatio(2)
        return QIcon(pm)

    def _song_changed(self, sid: int) -> None:
        self._pending_refresh.add(sid)
        if not self._refresh_timer.isActive():
            self._refresh_timer.start()

    def _flush_refresh(self) -> None:
        ids, self._pending_refresh = self._pending_refresh, set()
        for sid in ids:
            self.model.refresh_song(sid)
        self.proxy.invalidateFilter()
        self._count()
        if self.detail.sid in ids:
            self.detail.show_song(self.detail.sid)

    def _count(self) -> None:
        total = len(self.model.rows)
        shown = self.proxy.rowCount()
        self.count_lbl.setText(f"{total} songs" if shown == total else f"{shown} of {total} songs")

    # ---- selection ------------------------------------------------------------
    def _sid(self, idx: QModelIndex) -> int | None:
        if not idx.isValid():
            return None
        r = self.proxy.data(idx, ROLE_ROW)
        return r["id"] if r else None

    def current(self) -> int | None:
        return self._sid(self.table.currentIndex())

    def selected(self) -> list[int]:
        rows = self.table.selectionModel().selectedRows()
        ids = [self._sid(i) for i in rows]
        return [i for i in ids if i is not None] or ([self.current()] if self.current() is not None else [])

    def select_song(self, sid: int) -> None:
        i = self.model.index_of.get(sid)
        if i is None:
            return
        pidx = self.proxy.mapFromSource(self.model.index(i, 1))
        if pidx.isValid():
            self.table.selectionModel().setCurrentIndex(
                pidx, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
            self.table.scrollTo(pidx)

    def _select(self, idx: QModelIndex) -> None:
        sid = self._sid(idx)
        if sid is None:
            self.detail.show_empty()
        else:
            self.detail.show_song(sid)

    def _clicked(self, idx: QModelIndex) -> None:
        if idx.column() == 0:
            sid = self._sid(idx)
            if sid is not None:
                self.toggle_fav([sid])

    def _view_changed(self, cur, _prev) -> None:
        if cur is None:
            return
        self.cat_list.blockSignals(True)
        self.cat_list.clearSelection()
        self.cat_list.setCurrentItem(None)
        self.cat_list.blockSignals(False)
        self.proxy.set(view=cur.data(Qt.UserRole), category=None)
        self._count()

    def _cat_changed(self, cur, _prev) -> None:
        if cur is None:
            return
        self.views.blockSignals(True)
        self.views.setCurrentRow(-1)
        self.views.blockSignals(False)
        self.proxy.set(view="all", category=cur.data(Qt.UserRole))
        self._count()

    # ---- playback -------------------------------------------------------------
    def play(self, sid: int | None) -> None:
        if sid is None:
            return
        r = get_db().song(sid)
        if r is None or not Path(r["file_path"]).exists():
            hub.toast.emit("That song's file is missing.", "warn")
            return
        self.playing_sid = sid
        name = r["title"] or Path(r["file_path"]).stem
        self.bar.load(r["file_path"], f"song{sid}-{int(r['duration'] * 1000)}", r["duration"],
                      f"{name} — {r['artist']}" if r["artist"] else name)

    def _space(self) -> None:
        cur = self.current()
        if cur is not None and cur != self.playing_sid:
            self.play(cur)
        else:
            self.player.toggle()

    def stop(self) -> None:
        self.player.pause()

    # ---- actions --------------------------------------------------------------
    def toggle_fav(self, ids: list[int]) -> None:
        db = get_db()
        rows = [db.song(i) for i in ids]
        rows = [r for r in rows if r is not None]
        if not rows:
            return
        target = 0 if all(r["favourite"] for r in rows) else 1
        for r in rows:
            db.update_song(r["id"], favourite=target)
            hub.songChanged.emit(r["id"])

    def mark_checked(self, ids: list[int]) -> None:
        for i in ids:
            get_db().update_song(i, checked=1)
            hub.songChanged.emit(i)

    def reidentify(self, ids: list[int]) -> None:
        for i in ids:
            get_db().update_song(i, id_status="pending")
            hub.songChanged.emit(i)
        w = self.window()
        if hasattr(w, "identify"):
            w.identify.poke()

    def edit_info(self, sid: int) -> None:
        r = get_db().song(sid)
        if r is None:
            return
        d = EditInfoDialog(r, self)
        if d.exec():
            get_db().update_song(sid, manual_info=1, **d.values())
            hub.songChanged.emit(sid)

    def delete(self, ids: list[int]) -> None:
        ids = [i for i in ids if i is not None]
        if not ids:
            return
        db = get_db()
        names = [(db.song(i)["title"] or Path(db.song(i)["file_path"]).stem) for i in ids if db.song(i)]
        txt = f"Delete “{names[0]}”?" if len(ids) == 1 else f"Delete {len(ids)} songs?"
        if QMessageBox.question(self, "Delete", txt + "\n\nThe files go to the Recycle Bin.") != QMessageBox.Yes:
            return
        if self.playing_sid in ids:
            self.player.unload()
        for i in ids:
            r = db.song(i)
            if r is None:
                continue
            p = Path(r["file_path"])
            if p.exists() and not to_recycle_bin(p):
                hub.toast.emit(f"Couldn't move {p.name} to the Recycle Bin; left it in place.", "warn")
                continue
            scanjob.forget_fingerprint(i)
            db.delete_song(i)
        self.reload()
        hub.toast.emit(f"Deleted {len(ids)} song{'s' if len(ids) > 1 else ''}.", "ok")

    def new_category(self) -> int | None:
        name, ok = QInputDialog.getText(self, "New category", "Name:")
        if not ok or not name.strip():
            return None
        db = get_db()
        n = len(db.categories())
        try:
            cid = db.add_category(name.strip(), CATEGORY_COLOURS[n % len(CATEGORY_COLOURS)])
        except Exception:
            hub.toast.emit("A category with that name already exists.", "warn")
            return None
        hub.categoriesChanged.emit()
        return cid

    def category_menu(self, ids: list[int], anchor: QWidget | None = None) -> None:
        db = get_db()
        m = QMenu(self)
        have = [db.song_category_ids(i) for i in ids]
        for c in db.categories():
            a = m.addAction(self._dot(c["color"] or C["violet"]), c["name"])
            a.setCheckable(True)
            a.setChecked(all(c["id"] in h for h in have))
            a.toggled.connect(lambda on, cid=c["id"]: self._set_cat(ids, cid, on))
        if db.categories():
            m.addSeparator()
        m.addAction(icon("plus", C["teal"]), "New category…", lambda: self._new_and_add(ids))
        pos = anchor.mapToGlobal(anchor.rect().bottomLeft()) if anchor else self.cursor().pos()
        m.exec(pos)

    def _new_and_add(self, ids: list[int]) -> None:
        cid = self.new_category()
        if cid is not None:
            self._set_cat(ids, cid, True)

    def _set_cat(self, ids: list[int], cid: int, on: bool) -> None:
        for i in ids:
            get_db().set_song_category(i, cid, on)
        hub.categoriesChanged.emit()

    def _cat_menu(self, pos) -> None:
        it = self.cat_list.itemAt(pos)
        if it is None:
            return
        cid = it.data(Qt.UserRole)
        db = get_db()
        m = QMenu(self)
        m.addAction(icon("edit"), "Rename…", lambda: self._rename_cat(cid))
        m.addAction(icon("tag"), "Change colour…", lambda: self._recolor_cat(cid))
        m.addSeparator()
        m.addAction(icon("trash", C["red"]), "Delete category", lambda: self._delete_cat(cid))
        m.exec(self.cat_list.mapToGlobal(pos))

    def _rename_cat(self, cid: int) -> None:
        cur = next((c["name"] for c in get_db().categories() if c["id"] == cid), "")
        name, ok = QInputDialog.getText(self, "Rename category", "Name:", text=cur)
        if ok and name.strip():
            try:
                get_db().rename_category(cid, name.strip())
            except Exception:
                hub.toast.emit("A category with that name already exists.", "warn")
            hub.categoriesChanged.emit()

    def _recolor_cat(self, cid: int) -> None:
        cur = next((c["color"] for c in get_db().categories() if c["id"] == cid), C["violet"])
        col = QColorDialog.getColor(QColor(cur), self, "Category colour")
        if col.isValid():
            get_db().recolor_category(cid, col.name())
            hub.categoriesChanged.emit()

    def _delete_cat(self, cid: int) -> None:
        if QMessageBox.question(self, "Delete category", "Delete this category? Songs stay in the library.") == QMessageBox.Yes:
            get_db().delete_category(cid)
            if self.proxy.category == cid:
                self.proxy.set(category=None)
                self.views.setCurrentRow(0)
            hub.categoriesChanged.emit()

    def _header_menu(self, pos) -> None:
        m = QMenu(self)
        for col in range(2, len(COLS)):
            a = m.addAction(COLS[col])
            a.setCheckable(True)
            a.setChecked(not self.table.isColumnHidden(col))
            a.toggled.connect(lambda on, c=col: self._show_col(c, on))
        m.exec(self.table.horizontalHeader().mapToGlobal(pos))

    def _show_col(self, col: int, on: bool) -> None:
        self.table.setColumnHidden(col, not on)
        hidden = [c for c in range(2, len(COLS)) if self.table.isColumnHidden(c)]
        get_settings().set("ui.hidden_columns", hidden)

    def _table_menu(self, pos) -> None:
        ids = self.selected()
        if not ids:
            return
        m = QMenu(self)
        n = len(ids)
        m.addAction(icon("play"), "Play", lambda: self.play(ids[0]))
        m.addAction(icon("star", C["amber"]), "Favourite / unfavourite", lambda: self.toggle_fav(ids))
        m.addAction(icon("tag"), "Categories…", lambda: self.category_menu(ids))
        m.addAction(icon("check", C["teal"]), "Mark checked", lambda: self.mark_checked(ids))
        m.addAction(icon("refresh"), "Look up again", lambda: self.reidentify(ids))
        if n == 1:
            m.addAction(icon("edit"), "Edit info…", lambda: self.edit_info(ids[0]))
            r = get_db().song(ids[0])
            if r:
                m.addAction(icon("folder"), "Show file", lambda p=r["file_path"]: open_in_explorer(p))
        m.addSeparator()
        m.addAction(icon("trash", C["red"]), f"Delete {n} song{'s' if n > 1 else ''}", lambda: self.delete(ids))
        m.exec(self.table.viewport().mapToGlobal(pos))
