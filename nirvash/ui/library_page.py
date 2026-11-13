"""Song library: browse, listen, filter, favourite, approve, categorise, rename, unlink and delete."""
from __future__ import annotations

import html
import json
import re
import time
from pathlib import Path, PurePath

from PySide6.QtCore import (QAbstractTableModel, QEvent, QItemSelectionModel, QModelIndex, QRect, QSize,
                            QSortFilterProxyModel, Qt, QTimer, Signal)
from PySide6.QtGui import QColor, QFont, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox,
                               QFormLayout, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMenu, QPushButton, QScrollArea,
                               QSizePolicy, QSplitter, QStyle, QStyledItemDelegate, QTableView, QTextBrowser,
                               QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from ..analysis.labels import top_moods
from ..core.db import get_db
from ..core.settings import get_settings
from ..jobs import file_ops
from ..jobs import scan as scanjob  # noqa: F401  (kept for callers that reset the fingerprint index)
from ..jobs.library_files import to_recycle_bin
from ..media.ff import fmt_time
from . import dialogs, theme, workers
from .hub import hub
from .jobqueue import get_queue
from .player import Player
from .theme import C, CATEGORY_COLOURS, icon
from .widgets import (PlayerBar, button, card, chip, clear_layout, cover_pixmap, flow_row, label, open_in_explorer,
                      open_with_default, page_header, panel, tool)

# (key, header). Hidden columns are saved by key, so adding columns never shuffles them.
COLS = [("play", ""), ("fav", ""), ("title", "Title"), ("artist", "Artist"), ("length", "Length"),
        ("full", "Full song"), ("bpm", "BPM"), ("genre", "Genre"), ("mood", "Mood"), ("folder", "Folder"),
        ("plays", "Listens"), ("cats", "Categories"), ("status", "Status")]
KEYS = [k for k, _ in COLS]
COL = {k: i for i, k in enumerate(KEYS)}
OPTIONAL = KEYS[3:]                 # columns you can hide (Title always shows)
STATUS = {                          # status -> (text, colour token, read when drawn so themes apply)
    "pending": ("Identifying…", "violet"),
    "not_found": ("Not identified", "faint"),
    "error": ("Lookup failed", "warn"),
}
ROLE_SORT = Qt.UserRole + 1
ROLE_ROW = Qt.UserRole + 2

# Tri-state filters: key -> (label when on, label for the opposite, colour, test)
FILTERS = {
    "fav": ("Favourite", "Not favourite", "amber", lambda r: r["favourite"]),
    "approved": ("Approved", "Not approved", "ok", lambda r: r["approved"]),
    "played": ("Played", "Never played", "blue", lambda r: r["played"]),
    "identified": ("Identified", "Not identified", "violet", lambda r: r["identified"]),
    "check": ("To check", "Nothing to check", "warn", lambda r: r["partial"] and not r["checked"]),
    "linked": ("Linked to videos", "No video links", "teal", lambda r: bool(r["folders"])),
    "tagged": ("Renamed & tagged", "Not renamed", "sec", lambda r: r["tagged"]),
}
LISTENS = [("Listened through: any", None), ("Never listened through", ("eq", 0)), ("Listened once", ("eq", 1)),
           ("Listened twice", ("eq", 2)), ("Listened 3 times", ("eq", 3)), ("Listened 4 times", ("eq", 4)),
           ("Listened 5+ times", ("ge", 5)), ("At least once", ("ge", 1)), ("At least twice", ("ge", 2)),
           ("At least 3 times", ("ge", 3))]
NO_SOURCE = ("<none>",)             # folder filter: songs with no source video


def folder_of(video_path: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(casefolded parts for matching, original parts for display) of the folder a video is in."""
    parts = PurePath(video_path).parent.parts
    return tuple(p.casefold() for p in parts), tuple(parts)


def song_row(r, videos: list[str] | None = None) -> dict:
    mood = json.loads(r["mood"]) if r["mood"] else None
    folders = []
    for v in videos or ():
        f = folder_of(v)
        if f not in folders:
            folders.append(f)
    first = folders[0][1] if folders else ()
    row = {
        "id": r["id"], "path": r["file_path"],
        "title": r["title"] or Path(r["file_path"]).stem, "artist": r["artist"] or "",
        "identified": bool(r["title"]),
        "duration": r["duration"] or 0.0, "full": r["full_length"], "bpm": r["bpm"], "genre": r["genre"] or "",
        "mood": mood, "mood_txt": ", ".join(top_moods(mood)),
        "favourite": bool(r["favourite"]), "partial": bool(r["partial"]), "checked": bool(r["checked"]),
        "status": r["id_status"], "added": r["added_at"], "album": r["album"] or "", "year": r["year"] or "",
        "energy": r["energy"],
        "played": bool(r["played"]), "listens": int(r["listen_count"] or 0), "last_played": r["last_played"],
        "approved": bool(r["approved"]), "unlinked": bool(r["unlinked"]), "tagged": bool(r["tagged"]),
        "folders": folders,
        "folder_txt": first[-1] if first else "",
        "folder_full": str(PurePath(*first)) if first else "",
    }
    row["texts"] = row_texts(row)
    return row


def wave_key(r) -> str:
    """Cache name for a song's waveform: changes whenever the audio is edited."""
    return f"song{r['id']}-{int(r['duration'] * 1000)}-{r['gain_db'] or 0:+.1f}-{r['trim_start'] or 0:.2f}"


def row_texts(r: dict) -> list:
    """What each column shows, worked out once per row rather than on every paint."""
    t = []
    for k in KEYS:
        if k == "title":
            t.append(r["title"])
        elif k == "artist":
            t.append(r["artist"])
        elif k == "length":
            t.append(fmt_time(r["duration"]))
        elif k == "full":
            t.append(fmt_time(r["full"]) if r["full"] else "")
        elif k == "bpm":
            t.append(f"{r['bpm']:.0f}" if r["bpm"] else "")
        elif k == "genre":
            t.append(r["genre"])
        elif k == "mood":
            t.append(r["mood_txt"])
        elif k == "folder":
            t.append(r["folder_txt"])
        elif k == "plays":
            t.append(str(r["listens"]) if r["played"] else "")
        else:
            t.append(None)
    return t


class ListenTracker:
    """Counts a listen when a song really plays through: at least 90% of it heard (seeking doesn't count)
    and playback reaching the end. Starting it again from the top can count again."""

    def __init__(self):
        self.sid: int | None = None
        self.duration = 0.0
        self.heard = 0.0
        self.last: float | None = None
        self.counted = False

    def start(self, sid: int, duration: float) -> None:
        self.sid, self.duration = sid, float(duration or 0)
        self.heard, self.last, self.counted = 0.0, None, False

    def stop(self) -> None:
        self.sid = None

    def on_pos(self, pos: float, playing: bool) -> bool:
        """Feed the playback position. Returns True once when the listen should be counted."""
        if self.sid is None:
            return False
        if self.counted and pos < 1.0 and (self.last is None or self.last > 2.0):
            self.heard, self.counted = 0.0, False          # played again from the start
        if playing and self.last is not None:
            d = pos - self.last
            if 0 < d < 1.5:                                 # normal playback, not a seek
                self.heard += d
        self.last = pos
        return self._check(pos >= self.duration - 1.5)

    def on_end(self) -> bool:
        return self._check(True)

    def _check(self, at_end: bool) -> bool:
        if self.counted or self.duration <= 0 or not at_end:
            return False
        if self.heard >= 0.9 * self.duration - 0.5:
            self.counted = True
            return True
        return False


_ROLES = {ROLE_SORT, Qt.ForegroundRole, Qt.ToolTipRole, Qt.DecorationRole, Qt.TextAlignmentRole}


class LibraryModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: list[dict] = []
        self.cats: dict[int, set[int]] = {}
        self.cat_info: dict[int, tuple[str, str]] = {}
        self.index_of: dict[int, int] = {}
        self.playing_sid: int | None = None
        self.playing = False

    def reload(self) -> None:
        db = get_db()
        self.beginResetModel()
        vids = db.source_videos()
        self.rows = [song_row(r, vids.get(r["id"])) for r in db.songs()]
        self.cats = db.all_song_categories()
        self.cat_info = {c["id"]: (c["name"], c["color"] or C["violet"]) for c in db.categories()}
        self.index_of = {r["id"]: i for i, r in enumerate(self.rows)}
        self.endResetModel()

    def refresh_song(self, sid: int) -> bool:
        """Update one row. Returns False when the song is gone (caller reloads)."""
        db = get_db()
        r = db.song(sid)
        if r is None:
            return sid not in self.index_of
        self.cats[sid] = db.song_category_ids(sid)
        row = song_row(r, [s["video_path"] for s in db.sources(sid)])
        if sid not in self.index_of:
            self.beginInsertRows(QModelIndex(), 0, 0)
            self.rows.insert(0, row)
            self.index_of = {x["id"]: i for i, x in enumerate(self.rows)}
            self.endInsertRows()
            return True
        i = self.index_of[sid]
        self.rows[i] = row
        self.dataChanged.emit(self.index(i, 0), self.index(i, len(COLS) - 1))
        return True

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(COLS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal:
            if role == Qt.DisplayRole:
                return COLS[section][1]
            if role == Qt.ToolTipRole and KEYS[section] == "plays":
                return "Times listened all the way through"
        return None

    def data(self, idx: QModelIndex, role=Qt.DisplayRole):
        # Called for every cell on every paint: the common cases come first and do as little as possible.
        if role == ROLE_ROW:
            return self.rows[idx.row()]
        if role == Qt.DisplayRole:
            return self.rows[idx.row()]["texts"][idx.column()]
        if role not in _ROLES:
            return None
        r = self.rows[idx.row()]
        k = KEYS[idx.column()]
        if role == ROLE_SORT:
            return {
                "play": int(r["id"] == self.playing_sid), "fav": r["favourite"], "title": r["title"].casefold(),
                "artist": r["artist"].casefold(), "length": r["duration"], "full": r["full"] or 0,
                "bpm": r["bpm"] or 0, "genre": r["genre"].casefold(), "mood": r["mood_txt"],
                "folder": r["folder_full"].casefold(), "plays": r["listens"] * 2 + int(r["played"]),
                "cats": len(self.cats.get(r["id"], ())), "status": f"{int(r['approved'])}{r['status']}",
            }[k]
        if role == Qt.ForegroundRole and k not in ("title", "play", "fav"):
            return QColor(C["muted"])
        if role == Qt.ToolTipRole:
            if k == "title":
                return r["path"]
            if k == "folder" and r["folders"]:
                return "\n".join(str(PurePath(*f[1])) for f in r["folders"])
            if k == "plays":
                return (f"Listened all the way through {r['listens']} time{'s' if r['listens'] != 1 else ''}"
                        if r["played"] else "Never played")
            if k == "play":
                return "Pause" if (r["id"] == self.playing_sid and self.playing) else "Play"
        if role == Qt.DecorationRole and k == "fav":
            return icon("star_on", C["amber"], 16) if r["favourite"] else icon("star", C["faint"], 16)
        if role == Qt.TextAlignmentRole and k in ("length", "full", "bpm", "plays"):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None


class LibraryFilter(QSortFilterProxyModel):
    def __init__(self, model: LibraryModel, parent=None):
        super().__init__(parent)
        self.setSourceModel(model)
        self.m = model
        self.setSortRole(ROLE_SORT)
        self.text = ""
        self.tri: dict[str, int] = {k: 0 for k in FILTERS}      # 0 off, 1 yes, 2 the opposite
        self.listens: tuple[str, int] | None = None
        self.category: int | None = None
        self.folder: tuple[str, ...] | None = None

    def set(self, **kw) -> None:
        for k, v in kw.items():
            setattr(self, k, v)
        self.invalidateFilter()

    def set_tri(self, key: str, state: int) -> None:
        self.tri[key] = state
        self.invalidateFilter()

    def active(self) -> bool:
        return any(self.tri.values()) or self.listens is not None

    def filterAcceptsRow(self, row, parent):
        r = self.m.rows[row]
        for k, st in self.tri.items():
            if st and bool(FILTERS[k][3](r)) != (st == 1):
                return False
        if self.listens is not None:
            op, n = self.listens
            if (op == "eq" and r["listens"] != n) or (op == "ge" and r["listens"] < n):
                return False
        if self.category is not None and self.category not in self.m.cats.get(r["id"], set()):
            return False
        if self.folder is not None:
            if self.folder == NO_SOURCE:
                if r["folders"]:
                    return False
            elif not any(f[0][:len(self.folder)] == self.folder for f in r["folders"]):
                return False
        if self.text:
            hay = " ".join([r["title"], r["artist"], r["genre"], r["album"], r["mood_txt"], Path(r["path"]).name,
                            r["folder_full"]]).casefold()
            return all(w in hay for w in self.text.casefold().split())
        return True


def _pill(p: QPainter, opt, chips: list[tuple[str, str]]) -> None:
    """20 px round tags with the colour on the text and border (DESIGN.md, Tags)."""
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    f = QFont(opt.font)
    f.setPixelSize(11)
    f.setWeight(QFont.DemiBold)
    p.setFont(f)
    x = opt.rect.x() + 6
    h = min(18, opt.rect.height() - 4)
    y = opt.rect.y() + (opt.rect.height() - h) // 2
    for text, col in chips:
        col = C.get(col, col)
        w = p.fontMetrics().horizontalAdvance(text) + 14
        if x + w > opt.rect.right() - 2:
            break
        qc = QColor(col)
        bg = QColor(qc)
        bg.setAlpha(34)
        bd = QColor(qc)
        bd.setAlpha(140)
        p.setPen(bd)
        p.setBrush(bg)
        p.drawRoundedRect(QRect(x, y, w, h), h / 2, h / 2)
        p.setPen(qc)
        p.drawText(QRect(x, y, w, h), Qt.AlignCenter, text)
        x += w + 4
    p.restore()


_PM: dict[tuple, QPixmap] = {}


def _cached_pixmap(name: str, size: int, colour: str) -> QPixmap:
    """Icons drawn in table rows, rendered once per name/size/colour instead of on every paint."""
    key = (name, size, colour)
    pm = _PM.get(key)
    if pm is None:
        if len(_PM) > 200:
            _PM.clear()
        pm = _PM[key] = theme.pixmap(name, size, colour)
    return pm


class CellDelegate(QStyledItemDelegate):
    """Paints every cell of the library table itself.

    The stock delegate asks the model for a dozen things per cell on every paint, each a trip from Qt into Python,
    which made scrolling and clicking choppy with a big library. This asks once per cell for the row (already
    worked out) and draws the text, star, play button and tags directly."""

    def __init__(self, model: "LibraryModel", parent):
        # Must have a parent (and a Python reference): the view only keeps a raw pointer, so a delegate
        # Python frees would be painted through a dangling pointer - that crashed Qt6Widgets on Windows.
        super().__init__(parent)
        self.m = model
        self.view = parent
        # Cells are painted a row at a time, so the row looked up for the first cell serves the rest.
        # Forgotten whenever the rows could have moved.
        self._row: tuple[int, dict | None] = (-1, None)
        pm = parent.model()
        for sig in (pm.modelReset, pm.layoutChanged, pm.rowsInserted, pm.rowsRemoved, pm.rowsMoved,
                    pm.dataChanged, model.modelReset, model.dataChanged):
            sig.connect(self.forget)

    def forget(self, *_a) -> None:
        self._row = (-1, None)

    def paint(self, p: QPainter, opt, idx):
        row = idx.row()
        if self._row[0] == row:
            r = self._row[1]
        else:
            r = idx.data(ROLE_ROW)
            self._row = (row, r)
        # background, hover and selection exactly as the theme styles them (no text: opt carries none)
        self.view.style().drawControl(QStyle.CE_ItemViewItem, opt, p, self.view)
        if r is None:
            return
        col = idx.column()
        k = KEYS[col]
        rect = opt.rect
        if k == "play":
            self._play(p, rect, r, bool(opt.state & QStyle.State_MouseOver))
        elif k == "fav":
            s = 16
            pm = (_cached_pixmap("star_on", s, C["amber"]) if r["favourite"] else _cached_pixmap("star", s, C["faint"]))
            p.drawPixmap(rect.x() + (rect.width() - s) // 2, rect.y() + (rect.height() - s) // 2, s, s, pm)
        elif k == "cats":
            _pill(p, opt, [self.m.cat_info[c] for c in sorted(self.m.cats.get(r["id"], ())) if c in self.m.cat_info])
        elif k == "status":
            chips: list[tuple[str, str]] = []
            if r["approved"]:
                chips.append(("Approved", "ok"))
            if r["partial"] and not r["checked"]:
                chips.append(("Check", "warn"))
            if r["status"] in STATUS:
                chips.append(STATUS[r["status"]])
            _pill(p, opt, chips)
        else:
            text = r["texts"][col]
            if not text:
                return
            p.setFont(opt.font)
            p.setPen(QColor(C["text"] if (k == "title" or opt.state & QStyle.State_Selected) else C["muted"]))
            tr = rect.adjusted(6, 0, -6, 0)
            align = (Qt.AlignRight if k in _RIGHT else Qt.AlignLeft) | Qt.AlignVCenter
            p.drawText(tr, align, opt.fontMetrics.elidedText(text, Qt.ElideRight, tr.width()))

    def _play(self, p: QPainter, rect, r, hover: bool) -> None:
        cur = r["id"] == self.m.playing_sid
        playing = cur and self.m.playing
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        d = max(14, min(20, rect.height() - 4))
        rc = QRect(rect.x() + (rect.width() - d) // 2, rect.y() + (rect.height() - d) // 2, d, d)
        if cur or hover:
            bg = QColor(C["acc"])
            bg.setAlpha(255 if playing else 55)
            p.setPen(Qt.NoPen)
            p.setBrush(bg)
            p.drawEllipse(rc)
        colour = C["on_acc"] if playing else (C["acc"] if (cur or hover) else C["sec"])
        s = max(8, d - 10)
        pm = _cached_pixmap("pause" if playing else "play", s, colour)
        p.drawPixmap(rc.x() + (d - s) // 2 + (0 if playing else 1), rc.y() + (d - s) // 2, s, s, pm)
        p.restore()


_RIGHT = {"length", "full", "bpm", "plays"}


class TriChip(QPushButton):
    """A filter pill that's off, on ('Approved') or the opposite ('Not approved'). Click to cycle."""

    stateChanged = Signal(str, int)

    def __init__(self, key: str, parent=None):
        super().__init__(parent)
        self.key = key
        self.yes, self.no, self.color, _ = FILTERS[key]
        self.state = 0
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(f"Click: {self.yes} → {self.no} → off")
        self.clicked.connect(lambda: self.set_state((self.state + 1) % 3, emit=True))
        self.set_state(0)

    def set_state(self, st: int, emit: bool = False) -> None:
        self.state = st
        acc = QColor(C["acc"] if st == 1 else C["danger"])
        tint = f"rgba({acc.red()},{acc.green()},{acc.blue()},0.20)"
        edge = f"rgba({acc.red()},{acc.green()},{acc.blue()},0.60)"
        bg, bd, fg = (("transparent", C["line"], C["sec"]) if st == 0 else (tint, edge, C["text"]))
        self.setText({0: self.yes, 1: f"✓ {self.yes}", 2: f"✕ {self.no}"}[st].replace("&", "&&"))
        self.setStyleSheet(f"QPushButton {{ background:{bg}; color:{fg}; border:1px solid {bd}; border-radius:11px;"
                           f" padding:0 10px; min-height:22px; max-height:22px; font-size:12px;"
                           f" font-weight:{600 if st else 400}; }}"
                           f"QPushButton:hover {{ border-color:{edge if st else C['line2']}; color:{C['text']}; }}")
        if emit:
            self.stateChanged.emit(self.key, st)


class EditInfoDialog(dialogs.Dialog):
    def __init__(self, row, parent=None):
        super().__init__(parent, "Edit song info", "Saved info isn't replaced by later lookups.", 440)
        form = QFormLayout()
        form.setSpacing(6)
        self.fields = {}
        for key, name in (("title", "Title"), ("artist", "Artist"), ("album", "Album"), ("genre", "Genre"),
                          ("year", "Year")):
            e = QLineEdit(row[key] or "")
            form.addRow(label(name, "cap"), e)
            self.fields[key] = e
        self.body.addLayout(form)
        self.add_button("Cancel", "cancel")
        self.add_button("Save info", "primary")

    def values(self) -> dict:
        return {k: (e.text().strip() or None) for k, e in self.fields.items()}


class LookupDialog(dialogs.Dialog):
    """Pick which services to look songs up with this time."""

    def __init__(self, durations: list[float], parent=None):
        n = len(durations)
        super().__init__(parent, "Look up again", f"Look up {n} song{'s' if n != 1 else ''} again with:", 540)
        from ..jobs.identify import lookup_count
        self._count = lookup_count
        self.durs = durations
        self.s = get_settings()
        lay = self.body
        self.boxes: dict[str, QCheckBox] = {}

        def add(key, text, hint, checked):
            b = QCheckBox(text)
            b.setChecked(checked)
            b.toggled.connect(self._update)
            lay.addWidget(b)
            if hint:
                h = label(hint, "faint", wrap=True)
                h.setContentsMargins(24, 0, 0, 2)
                lay.addWidget(h)
            self.boxes[key] = b
            return b

        add("shazam", "Shazam", "Free, about 18 lookups a minute. Tries 3 parts of each song.", True)
        add("deep", "Shazam, trying harder",
            "Tries more parts of each song, then sped-up and slowed-down versions of it - edits often change "
            "the speed, which stops Shazam matching. Free, but up to about 3× as many lookups.", False)
        self.key = QLineEdit(self.s.get("identify.acoustid_key", ""))
        self.key.setPlaceholderText("AcoustID key - free from acoustid.org")
        add("acoustid", "AcoustID", "Free. Matches whole songs, so it rarely finds short clips.", False)
        lay.addWidget(self.key)
        self.tok = QLineEdit(self.s.get("identify.audiotag_key", ""))
        self.tok.setPlaceholderText("AudioTag key - free from audiotag.info (API → API keys)")
        self.tok.setEchoMode(QLineEdit.PasswordEchoOnEdit)
        add("audiotag", "AudioTag", "3 hours of audio a month free; uses 15 s per song (about 700 songs a month). "
            "Check what's left in Settings.", False)
        lay.addWidget(self.tok)
        self.key.textChanged.connect(self._update)
        self.tok.textChanged.connect(self._update)
        self.est = label("", "muted", wrap=True)
        lay.addWidget(self.est)
        self.add_button("Cancel", "cancel")
        self.ok = self.add_button(f"Look up {n} song{'s' if n != 1 else ''}", "primary")
        self.ok.clicked.disconnect()
        self.ok.clicked.connect(self._accept)
        self._update()

    def methods(self) -> list[str]:
        out = [k for k, b in self.boxes.items() if b.isChecked()]
        if "deep" in out and "shazam" in out:
            out.remove("shazam")          # trying harder includes the normal tries
        return out

    def _update(self) -> None:
        self.key.setVisible(self.boxes["acoustid"].isChecked())
        self.tok.setVisible(self.boxes["audiotag"].isChecked())
        m = self.methods()
        missing = ("acoustid" in m and not self.key.text().strip()) or ("audiotag" in m and not self.tok.text().strip())
        self.ok.setEnabled(bool(m) and not missing)
        if not m:
            self.est.setText("Pick at least one.")
            return
        if missing:
            self.est.setText("Enter the key above first.")
            return
        sh = [x for x in m if x in ("shazam", "deep")]
        n_sh = sum(self._count(sh, d) for d in self.durs) if sh else 0
        n_all = sum(self._count(m, d) for d in self.durs)
        txt = f"Up to {n_all:,} lookups"
        if n_sh:
            mins = n_sh / 18
            txt += (f" - at most about {mins / 60:.1f} hours" if mins >= 90 else f" - at most about {max(1, round(mins))} min")
            txt += " at Shazam's limit. It stops early for each song as soon as one finds it."
        if "audiotag" in m:
            secs = 15 * len(self.durs)
            txt += f" Uses up to {secs // 60:,} min of your AudioTag free time (3 hours a month)."
        self.est.setText(txt)
        if self.isVisible():
            self.adjustSize()

    def _accept(self) -> None:
        if self.boxes["acoustid"].isChecked():
            self.s.set("identify.acoustid_key", self.key.text().strip())
        if self.boxes["audiotag"].isChecked():
            self.s.set("identify.audiotag_key", self.tok.text().strip())
            from ..jobs.identify import reset_audiotag
            reset_audiotag()
        self.accept()


class DeleteDialog(dialogs.Dialog):
    def __init__(self, text: str, n_videos: int, n_songs: int = 1, parent=None):
        super().__init__(parent, text, "The files go to the Recycle Bin.", 460)
        self.skip_videos = QCheckBox(f"Skip the video{'s' if n_videos != 1 else ''} they came from in future scans"
                                     + (f" ({n_videos})" if n_videos else ""))
        self.skip_videos.setChecked(True)
        self.skip_videos.setEnabled(n_videos > 0)
        self.body.addWidget(self.skip_videos)
        self.skip_songs = QCheckBox("Don't add the same music again if it turns up in other videos")
        self.skip_songs.setChecked(False)       # off unless asked for: the same music in another video is often wanted
        self.body.addWidget(self.skip_songs)
        self.body.addWidget(label("You can undo both in Settings → Reset → Skipped videos & songs.", "faint", wrap=True))
        self.add_button("Cancel", "cancel")
        self.add_button(f"Send {n_songs} song{'s' if n_songs != 1 else ''} to the Recycle Bin", "danger")


def strip_lrc(text: str) -> str:
    return "\n".join(re.sub(r"^\s*(\[[0-9:.]+\])+\s*", "", line) for line in text.splitlines())


def _link(text: str, target: str, color: str = C["text"]) -> QLabel:
    l = QLabel(f'<a href="{html.escape(target, quote=True)}" style="color:{color}; text-decoration:none;">'
               f'{html.escape(text)}</a>')
    l.setTextFormat(Qt.RichText)
    l.setTextInteractionFlags(Qt.LinksAccessibleByMouse)
    l.setOpenExternalLinks(False)
    l.setToolTip(target)
    l.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
    return l


class DetailPanel(QScrollArea):
    def __init__(self, page: "LibraryPage"):
        super().__init__()
        self.page = page
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setMinimumWidth(260)
        self.setMaximumWidth(460)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        inner.setObjectName("page")
        self.setWidget(inner)
        self.lay = QVBoxLayout(inner)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(8)
        self.sid: int | None = None
        self.play_btn: QPushButton | None = None
        self.lyrics_open = False
        self.show_empty()

    def show_empty(self) -> None:
        clear_layout(self.lay)
        self.sid = None
        self.play_btn = None
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

    def update_play(self) -> None:
        if self.play_btn is None or self.sid is None:
            return
        on = self.sid == self.page.playing_sid and self.page.player.is_playing()
        self.play_btn.setText("Pause" if on else "Play")
        self.play_btn.setIcon(icon("pause" if on else "play", C["on_acc"], 15))

    def show_song(self, sid: int) -> None:
        db = get_db()
        r = db.song(sid)
        if r is None:
            self.show_empty()
            return
        if sid != self.sid:
            self.lyrics_open = False
        self.sid = sid
        clear_layout(self.lay)
        top = card()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(12, 12, 12, 12)
        tl.setSpacing(7)
        cover = QLabel()
        cpath = get_settings().cache("covers") / f"{sid}.jpg"
        cover.setPixmap(cover_pixmap(str(cpath), 168, r["title"] or r["file_path"], 8))
        cover.setAlignment(Qt.AlignCenter)
        tl.addWidget(cover)
        title = label(r["title"] or Path(r["file_path"]).stem, "h2", wrap=True)
        tl.addWidget(title)
        if r["artist"]:
            tl.addWidget(label(r["artist"], "muted", wrap=True))
        chips_w, chips = flow_row(5)
        if r["approved"]:
            chips.addWidget(chip("Approved", C["ok"]))
        if r["partial"] and not r["checked"]:
            chips.addWidget(chip("To check", C["warn"]))
        if r["genre"]:
            chips.addWidget(chip(r["genre"].split(",")[0], C["teal"]))
        if r["bpm"]:
            chips.addWidget(chip(f"{r['bpm']:.0f} BPM", C["blue"]))
        mood = json.loads(r["mood"]) if r["mood"] else None
        for m in top_moods(mood):
            chips.addWidget(chip(m, C["violet"]))
        if chips.count():
            tl.addWidget(chips_w)
        cats = db.song_category_ids(sid)
        if cats:
            cw, cl = flow_row(5)
            for c in db.categories():
                if c["id"] in cats:
                    cl.addWidget(chip(c["name"], c["color"] or C["violet"], filled=True))
            tl.addWidget(cw)
        # Actions
        acts_w, acts = flow_row(6)
        self.play_btn = button("Play", "play", "primary")
        self.play_btn.clicked.connect(lambda: self.page.toggle_play(sid))
        acts.addWidget(self.play_btn)
        fav = button("Favourite" if not r["favourite"] else "Favourited", "star_on" if r["favourite"] else "star",
                     color=C["amber"] if r["favourite"] else C["text"])
        fav.clicked.connect(lambda: self.page.toggle_fav([sid]))
        appr = button("Approved" if r["approved"] else "Approve", "badge",
                      color=C["ok"] if r["approved"] else C["text"])
        appr.setToolTip("Approved = you've decided this song is fine to use. Click again to remove.")
        appr.clicked.connect(lambda: self.page.set_approved([sid], not r["approved"]))
        acts.addWidget(appr)
        acts.addWidget(fav)
        catb = button("Categories", "tag")
        catb.clicked.connect(lambda: self.page.category_menu([sid], catb))
        acts.addWidget(catb)
        more = QToolButton()
        more.setObjectName("menuBtn")
        more.setText("More ▾")
        more.setToolButtonStyle(Qt.ToolButtonTextOnly)
        more.setPopupMode(QToolButton.InstantPopup)
        mm = QMenu(more)
        self.page.fill_actions(mm, [sid])
        more.setMenu(mm)
        acts.addWidget(more)
        tl.addWidget(acts_w)
        if r["partial"] and not r["checked"]:
            ok = button("Sounds fine — mark checked", "check", color=C["acc"])
            ok.setToolTip("Marks it checked and approved")
            ok.clicked.connect(lambda: self.page.mark_checked([sid]))
            tl.addWidget(ok)
        self.lay.addWidget(top)
        self.update_play()

        info = card()
        il = QFormLayout(info)
        il.setContentsMargins(12, 10, 12, 10)
        il.setHorizontalSpacing(12)
        il.setVerticalSpacing(4)

        def row(name, val):
            if val:
                k = label(name, "muted")
                v = val if isinstance(val, QWidget) else label(str(val), wrap=True)
                if isinstance(v, QLabel) and not isinstance(val, QWidget):
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
        fl = _link(Path(r["file_path"]).name, r["file_path"], C["acc"])
        fl.setWordWrap(True)
        fl.setToolTip("Show the file in Explorer")
        fl.linkActivated.connect(lambda p: open_in_explorer(p))
        row("File", fl)
        status_txt = {"found": f"Identified by {r['id_source'] or 'lookup'}", "pending": "Waiting to be identified",
                      "not_found": "Not found by Shazam / AcoustID", "error": "Lookup failed (will retry)"}
        row("Song info", ("Edited by you" if r["manual_info"] else status_txt.get(r["id_status"], "")))
        if r["played"]:
            when = time.strftime("%d %b %Y %H:%M", time.localtime(r["last_played"])) if r["last_played"] else ""
            n = r["listen_count"] or 0
            row("Listened", f"All the way through {n} time{'s' if n != 1 else ''}")
            row("Last played", when)
        else:
            row("Listened", "Never played")
        row("Added", time.strftime("%d %b %Y %H:%M", time.localtime(r["added_at"])))
        if r["unlinked"]:
            row("Source videos", "Links removed by you")
        self.lay.addWidget(info)

        srcs = db.sources(sid)
        if srcs:
            sc = card()
            sl = QVBoxLayout(sc)
            sl.setContentsMargins(12, 10, 12, 10)
            sl.setSpacing(4)
            sl.addWidget(label(f"From {len(srcs)} video{'s' if len(srcs) > 1 else ''}", "h2"))
            for s in srcs[:12]:
                rowl = QHBoxLayout()
                rowl.setSpacing(4)
                name = _link(Path(s["video_path"]).name, s["video_path"])
                name.setToolTip(f"{s['video_path']}\nClick to show it in its folder")
                name.linkActivated.connect(lambda p: open_in_explorer(p))
                rowl.addWidget(name, 1)
                span = label(f"{fmt_time(s['start'])}–{fmt_time(s['end'])}", "muted")
                rowl.addWidget(span)
                pb = tool("play", "Open the video in your video player", C["acc"], 14)
                pb.clicked.connect(lambda _=False, p=s["video_path"]: self.page.open_video(p))
                rowl.addWidget(pb)
                ob = tool("folder", "Show video in its folder", C["muted"], 15)
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
            ll.setContentsMargins(12, 10, 12, 10)
            head = QHBoxLayout()
            head.addWidget(label("Lyrics", "h2"))
            head.addStretch(1)
            tgl = button("Hide" if self.lyrics_open else "Show")
            head.addWidget(tgl)
            ll.addLayout(head)
            tb = QTextBrowser()
            tb.setPlainText(lyr)
            tb.setMinimumHeight(260)
            ll.addWidget(tb)
            if not self.lyrics_open:
                tb.hide()

            def flip():
                self.lyrics_open = not tb.isVisible()
                tb.setVisible(self.lyrics_open)
                tgl.setText("Hide" if self.lyrics_open else "Show")

            tgl.clicked.connect(flip)
            self.lay.addWidget(lc)
        self.lay.addStretch(1)


class LibraryPage(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("page")
        self.model = LibraryModel(self)
        self.proxy = LibraryFilter(self.model, self)
        self.batch = None
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 12)
        root.setSpacing(8)

        head, self.head_desc = page_header("Song library", "")
        root.addLayout(head)

        # Toolbar: search, the library's actions, then the filters
        self.toolbar = panel()
        tl = QVBoxLayout(self.toolbar)
        tl.setContentsMargins(10, 8, 10, 8)
        tl.setSpacing(8)
        r1 = QHBoxLayout()
        r1.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search title, artist, genre, mood, folder…   ( / )")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(240)
        self.search.setMaximumWidth(380)
        self.search.textChanged.connect(lambda t: (self.proxy.set(text=t.strip()), self._count()))
        r1.addWidget(self.search, 1)
        self.lookup_btn = button("Look up unidentified", "refresh")
        self.lookup_btn.setToolTip("Look up every song that wasn't identified again - pick Shazam, AcoustID or AudioTag")
        self.lookup_btn.clicked.connect(self.lookup_unidentified)
        r1.addWidget(self.lookup_btn)
        self.sel_btn = QToolButton()
        self.sel_btn.setObjectName("menuBtn")
        self.sel_btn.setText("Selected ▾")
        self.sel_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.sel_btn.setPopupMode(QToolButton.InstantPopup)
        self.sel_btn.setToolTip("Actions for the selected songs (or right-click them)")
        self.sel_menu = QMenu(self.sel_btn)
        self.sel_menu.aboutToShow.connect(lambda: (self.sel_menu.clear(), self.fill_actions(self.sel_menu, self.selected())))
        self.sel_btn.setMenu(self.sel_menu)
        r1.addWidget(self.sel_btn)
        r1.addStretch(1)
        self.play_btn = button("Play", "play", "primary")
        self.play_btn.setToolTip("Play the selected song (Enter); Space pauses")
        self.play_btn.clicked.connect(lambda: self.toggle_play(self.current()))
        r1.addWidget(self.play_btn)
        tl.addLayout(r1)
        fb = QHBoxLayout()
        fb.setSpacing(6)
        fl = label("Filters", "cap")
        fl.setFixedWidth(88)
        fl.setToolTip("Filters combine. Click a filter once for yes, again for the opposite, again for off.")
        fb.addWidget(fl)
        self.chips: dict[str, TriChip] = {}
        for k in FILTERS:
            c = TriChip(k, self)
            c.stateChanged.connect(self._tri_changed)
            fb.addWidget(c)
            self.chips[k] = c
        self.listen_box = QComboBox()
        for text, val in LISTENS:
            self.listen_box.addItem(text, val)
        self.listen_box.activated.connect(self._listens_changed)
        fb.addWidget(self.listen_box)
        self.clear_btn = button("Clear filters", "x", "ghost")
        self.clear_btn.clicked.connect(self.clear_filters)
        self.clear_btn.setVisible(False)
        fb.addWidget(self.clear_btn)
        fb.addStretch(1)
        tl.addLayout(fb)
        root.addWidget(self.toolbar)

        self.split = split = QSplitter(Qt.Horizontal)
        split.setHandleWidth(8)
        split.setChildrenCollapsible(False)
        # Side panel: categories and the folders songs came from
        side = panel()
        self.side = side
        sl = QVBoxLayout(side)
        sl.setContentsMargins(8, 8, 8, 8)
        sl.setSpacing(4)
        ch = QHBoxLayout()
        ch.addWidget(label("Categories", "cap"))
        ch.addStretch(1)
        addc = tool("plus", "New category", C["acc"], 14)
        addc.clicked.connect(self.new_category)
        ch.addWidget(addc)
        sl.addLayout(ch)
        self.cat_list = QListWidget()
        self.cat_list.setObjectName("sideList")
        self.cat_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.cat_list.customContextMenuRequested.connect(self._cat_menu)
        self.cat_list.currentItemChanged.connect(self._cat_changed)
        self.cat_list.setToolTip("Right-click a category to rename, recolour or delete it.")
        sl.addWidget(self.cat_list, 2)
        sl.addSpacing(4)
        sl.addWidget(label("Folders", "cap"))
        self.folder_tree = QTreeWidget()
        self.folder_tree.setObjectName("sideList")
        self.folder_tree.setHeaderHidden(True)
        self.folder_tree.setColumnCount(2)
        self.folder_tree.setIndentation(14)
        self.folder_tree.setUniformRowHeights(True)
        self.folder_tree.setToolTip("Where the songs' videos are. Picking a folder includes its subfolders.")
        self.folder_tree.header().setStretchLastSection(False)
        self.folder_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.folder_tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.folder_tree.currentItemChanged.connect(self._folder_changed)
        self.folder_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.folder_tree.customContextMenuRequested.connect(self._folder_menu)
        sl.addWidget(self.folder_tree, 3)
        self._folder_sig = None
        side.setMinimumWidth(190)
        split.addWidget(side)

        # Table
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(COL["title"], Qt.AscendingOrder)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setMouseTracking(True)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setMinimumSectionSize(18)
        self.table.verticalHeader().setDefaultSectionSize(theme.T["rh"])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)
        hh = self.table.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        for k, w in self.WIDTHS.items():
            self.table.setColumnWidth(COL[k], w)
        for k in ("play", "fav"):
            hh.setSectionResizeMode(COL[k], QHeaderView.Fixed)
        # Title and Artist share whatever room is left, so nothing scrolls sideways.
        hh.setSectionResizeMode(COL["title"], QHeaderView.Stretch)
        hh.setSectionResizeMode(COL["artist"], QHeaderView.Stretch)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.setTextElideMode(Qt.ElideRight)
        hh.setMinimumSectionSize(24)
        hh.setContextMenuPolicy(Qt.CustomContextMenu)
        hh.customContextMenuRequested.connect(self._header_menu)
        hh.sectionResized.connect(self._col_resized)
        self.user_hidden = self._load_hidden()
        self.auto_hidden: set[str] = set()
        self.table.viewport().installEventFilter(self)
        self._restore_widths()
        self._apply_columns()
        # The delegate is a child of the table and kept referenced (see CellDelegate).
        self.cell_delegate = CellDelegate(self.model, self.table)
        self.table.setItemDelegate(self.cell_delegate)
        self.table.clicked.connect(self._clicked)
        self.table.doubleClicked.connect(lambda idx: self.play(self._sid(idx)) if idx.column() > COL["fav"] else None)
        self.table.selectionModel().currentRowChanged.connect(lambda cur, _p: self._select(cur))
        split.addWidget(self.table)

        self.detail = DetailPanel(self)
        self._pending_sid: int | None = None
        self._row_was_current = False
        self._detail_timer = QTimer(self, singleShot=True, interval=60, timeout=self._show_selected)
        split.addWidget(self.detail)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setStretchFactor(2, 0)
        sizes = get_settings().get("ui.library_split")
        split.setSizes(sizes if isinstance(sizes, list) and len(sizes) == 3 else [230, 900, 292])
        split.splitterMoved.connect(lambda *_: get_settings().set("ui.library_split", split.sizes()))
        root.addWidget(split, 1)

        self.player = Player(self, video=False)
        self.bar = PlayerBar(self.player)
        root.addWidget(self.bar)
        self.playing_sid: int | None = None
        self.tracker = ListenTracker()
        self.player.positionChanged.connect(self._pos)
        self.player.ended.connect(self._ended)
        self.player.playingChanged.connect(self._playing_changed)
        self.bar.wave.menuRequested.connect(self._wave_menu)

        # Page keys (the shared ones - Space, arrows, / - come from the main window)
        for keys, fn in (("Delete", lambda: self.delete(self.selected())),
                         ("V", lambda: self.toggle_fav(self.selected())),
                         ("A", lambda: self.toggle_approved(self.selected())),
                         ("E", lambda: self.edit_audio(self.current())),
                         ("Return", lambda: self.play(self.current()))):
            sc = QShortcut(QKeySequence(keys), self.table)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(fn)

        hub.songChanged.connect(self._song_changed)
        hub.songsReset.connect(self.reload)
        hub.categoriesChanged.connect(self._reload_categories)
        self._pending_refresh: set[int] = set()
        self._refresh_timer = QTimer(self, singleShot=True, interval=400, timeout=self._flush_refresh)
        self.reload()

    # ---- shared keys and theme -------------------------------------------------
    def focus_search(self) -> None:
        self.search.setFocus()
        self.search.selectAll()

    def key_volume_up(self) -> None:
        self.bar.nudge_volume(5)

    def key_volume_down(self) -> None:
        self.bar.nudge_volume(-5)

    def key_mute(self) -> None:
        self.bar.toggle_mute()

    def key_play_pause(self) -> None:
        cur = self.current()
        if self.playing_sid is None or (cur is not None and cur != self.playing_sid and not self.player.is_playing()):
            if cur is not None:
                self.play(cur)
            return
        self.player.toggle()

    def _step(self, to: str) -> None:
        n = self.proxy.rowCount()
        if not n:
            return
        cur = self.table.currentIndex().row()
        row = {"prev": max(0, cur - 1), "next": min(n - 1, cur + 1), "first": 0, "last": n - 1}[to]
        idx = self.proxy.index(row, COL["title"])
        self.table.selectionModel().setCurrentIndex(idx, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
        self.table.scrollTo(idx)
        if self.player.is_playing():
            self.play(self._sid(idx))

    def key_prev(self) -> None:
        self._step("prev")

    def key_next(self) -> None:
        self._step("next")

    def key_first(self) -> None:
        self._step("first")

    def key_last(self) -> None:
        self._step("last")

    def set_panels_hidden(self, hide: bool) -> None:
        for w in (self.toolbar, self.side, self.detail):
            w.setVisible(not hide)

    def theme_changed(self) -> None:
        self.table.verticalHeader().setDefaultSectionSize(theme.T["rh"])
        for c in self.chips.values():
            c.set_state(c.state)
        self.bar._restyle()
        self._reload_categories()
        self.table.viewport().update()

    def _col_resized(self, i: int, _old: int, new: int) -> None:
        k = KEYS[i]
        if k in ("title", "artist", "play", "fav") or new <= 0:
            return
        w = dict(get_settings().get("ui.library_widths") or {})
        w[k] = new
        get_settings().set("ui.library_widths", w)

    def _restore_widths(self) -> None:
        self.WIDTHS = dict(self.WIDTHS)
        w = get_settings().get("ui.library_widths") or {}
        for k, v in w.items():
            if k in COL and isinstance(v, int) and v >= 24:
                self.WIDTHS[k] = v
                self.table.setColumnWidth(COL[k], v)

    # ---- data ---------------------------------------------------------------
    def reload(self) -> None:
        cur = self.current()
        self.model.reload()
        self._reload_categories()
        self._reload_folders()
        self._count()
        if cur is not None and cur in self.model.index_of:
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
        allit = QListWidgetItem(icon("library", C["acc"], 15), f"All songs   {len(self.model.rows)}")
        allit.setData(Qt.UserRole, None)
        allit.setSizeHint(QSize(0, 30))
        self.cat_list.addItem(allit)
        found = keep is None
        for c in db.categories():
            it = QListWidgetItem(f"{c['name']}   {counts.get(c['id'], 0)}")
            it.setData(Qt.UserRole, c["id"])
            it.setIcon(self._dot(c["color"] or C["violet"]))
            it.setSizeHint(QSize(0, 30))
            self.cat_list.addItem(it)
            if c["id"] == keep:
                self.cat_list.setCurrentItem(it)
                found = True
        if keep is None or not found:
            self.cat_list.setCurrentRow(0)
        self.cat_list.blockSignals(False)
        if not found:
            self.proxy.set(category=None)
            self._count()
        self.table.viewport().update()
        if self.detail.sid is not None:
            self.detail.show_song(self.detail.sid)

    def _folder_counts(self) -> tuple[dict, dict, int]:
        counts: dict[tuple, int] = {}
        names: dict[tuple, str] = {}
        none = 0
        for r in self.model.rows:
            if not r["folders"]:
                none += 1
                continue
            seen = set()
            for key, disp in r["folders"]:
                for i in range(1, len(key) + 1):
                    k = key[:i]
                    if k not in seen:
                        seen.add(k)
                        counts[k] = counts.get(k, 0) + 1
                        names.setdefault(k, disp[i - 1])
        return counts, names, none

    def _reload_folders(self, force: bool = True) -> None:
        counts, names, none = self._folder_counts()
        sig = (tuple(sorted(counts.items())), none)
        if not force and sig == self._folder_sig:
            return
        self._folder_sig = sig
        tree = self.folder_tree
        expanded = set()
        it_iter = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
        while it_iter:
            it = it_iter.pop()
            if it.isExpanded():
                expanded.add(it.data(0, Qt.UserRole))
            it_iter += [it.child(i) for i in range(it.childCount())]
        keep = self.proxy.folder
        tree.blockSignals(True)
        tree.clear()
        children: dict[tuple, list[tuple]] = {}
        for k in counts:
            children.setdefault(k[:-1], []).append(k)

        def make(parent, key: tuple, text: str):
            # Folders holding only one subfolder (and no songs of their own) are skipped over, so the tree
            # starts where the folders actually split. The full path is in the tooltip.
            while len(children.get(key, [])) == 1 and counts[children[key][0]] == counts[key]:
                key = children[key][0]
                text = names[key]
            it = QTreeWidgetItem(parent, [text, str(counts[key])])
            it.setData(0, Qt.UserRole, key)
            it.setIcon(0, icon("folder", C["amber"], 14))
            it.setForeground(1, QColor(C["faint"]))
            it.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            it.setToolTip(0, str(PurePath(*[names[key[:i + 1]] for i in range(len(key))])))
            for ck in sorted(children.get(key, []), key=lambda x: names[x].casefold()):
                make(it, ck, names[ck])
            if key in expanded:
                it.setExpanded(True)
            return it

        allit = QTreeWidgetItem(tree, ["All folders", str(len(self.model.rows))])
        allit.setData(0, Qt.UserRole, None)
        allit.setIcon(0, icon("library", C["acc"], 14))
        allit.setForeground(1, QColor(C["faint"]))
        for k in sorted(children.get((), []), key=lambda x: names[x].casefold()):
            top = make(tree, k, names[k])
            if not expanded and keep is None:
                top.setExpanded(True)
        if none:
            ns = QTreeWidgetItem(tree, ["No source video", str(none)])
            ns.setData(0, Qt.UserRole, NO_SOURCE)
            ns.setIcon(0, icon("unlink", C["faint"], 15))
            ns.setForeground(0, QColor(C["muted"]))
            ns.setForeground(1, QColor(C["faint"]))
        # Keep the picked folder (or the nearest one still there).
        target = allit
        if keep is not None:
            best = None
            stack = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
            while stack:
                it = stack.pop()
                k = it.data(0, Qt.UserRole)
                if k is not None and k[:len(keep)] == keep or k == keep:
                    if best is None or len(k) < len(best.data(0, Qt.UserRole)):
                        best = it
                stack += [it.child(i) for i in range(it.childCount())]
            if best is not None:
                target = best
            else:
                self.proxy.set(folder=None)
        tree.setCurrentItem(target)
        tree.blockSignals(False)

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
        gone = False
        for sid in ids:
            if not self.model.refresh_song(sid):
                gone = True
        if gone:
            self.reload()
            return
        self.proxy.invalidateFilter()
        self._reload_folders(force=False)
        self._count()
        if self.detail.sid in ids:
            self.detail.show_song(self.detail.sid)

    def _count(self) -> None:
        total = len(self.model.rows)
        shown = self.proxy.rowCount()
        n_check = sum(1 for r in self.model.rows if r["partial"] and not r["checked"])
        txt = f"{total:,} songs" if shown == total else f"{shown:,} of {total:,} songs"
        self.head_desc.setText(txt + (f" · {n_check} to check" if n_check else "") +
                               " · listen, check, tidy and tag the music found in your videos")
        n = self._unidentified_count()
        self.lookup_btn.setText(f"Look up unidentified ({n})")
        self.lookup_btn.setEnabled(n > 0)
        self.clear_btn.setVisible(self.proxy.active())

    def _unidentified_count(self) -> int:
        return sum(1 for r in self.model.rows if not r["identified"] and r["status"] in ("not_found", "error"))

    # ---- filters --------------------------------------------------------------
    def _tri_changed(self, key: str, state: int) -> None:
        self.proxy.set_tri(key, state)
        self._count()

    def _listens_changed(self) -> None:
        self.proxy.set(listens=self.listen_box.currentData())
        self._count()

    def set_filter(self, key: str, state: int) -> None:
        self.chips[key].set_state(state)
        self._tri_changed(key, state)

    def clear_filters(self) -> None:
        for c in self.chips.values():
            c.set_state(0)
        self.listen_box.setCurrentIndex(0)
        self.proxy.tri = {k: 0 for k in FILTERS}
        self.proxy.set(listens=None)
        self._count()

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
        pidx = self.proxy.mapFromSource(self.model.index(i, COL["title"]))
        if pidx.isValid():
            self.table.selectionModel().setCurrentIndex(
                pidx, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
            self.table.scrollTo(pidx)

    def _select(self, idx: QModelIndex) -> None:
        # The details panel is rebuilt a moment after the selection settles, so holding an arrow key (or clicking
        # quickly down the list) moves the highlight at once instead of rebuilding the panel for every row passed.
        self._pending_sid = self._sid(idx)
        self._detail_timer.start()

    def _show_selected(self) -> None:
        sid = self._pending_sid
        if sid is None:
            self.detail.show_empty()
        else:
            self.detail.show_song(sid)

    def _clicked(self, idx: QModelIndex) -> None:
        sid = self._sid(idx)
        if sid is None:
            return
        if idx.column() == COL["play"]:
            self.toggle_play(sid)
        elif idx.column() == COL["fav"]:
            self.toggle_fav([sid])
        elif sid == self.playing_sid and not (QApplication.keyboardModifiers() & (Qt.ControlModifier | Qt.ShiftModifier)):
            # A plain click anywhere on the playing song's row pauses it; clicking it again carries on.
            if self.player.is_playing():
                self.player.pause()
            elif self._row_was_current:
                self.player.play()
            self._repaint_play()

    def _cat_changed(self, cur, _prev) -> None:
        if cur is None:
            return
        self.proxy.set(category=cur.data(Qt.UserRole))
        self._count()

    def _folder_changed(self, cur, _prev) -> None:
        if cur is None:
            return
        self.proxy.set(folder=cur.data(0, Qt.UserRole))
        self._count()

    def _folder_menu(self, pos) -> None:
        it = self.folder_tree.itemAt(pos)
        if it is None:
            return
        key = it.data(0, Qt.UserRole)
        if key is None or key == NO_SOURCE:
            return
        path = it.toolTip(0)
        m = QMenu(self)
        m.addAction(icon("folder"), "Open folder", lambda: open_in_explorer(path))
        m.exec(self.folder_tree.viewport().mapToGlobal(pos))

    # ---- playback -------------------------------------------------------------
    def play(self, sid: int | None) -> None:
        if sid is None:
            return
        r = get_db().song(sid)
        if r is None or not Path(r["file_path"]).exists():
            hub.toast.emit("That song's file is missing.", "warn")
            return
        self.playing_sid = sid
        self.model.playing_sid = sid
        from ..jobs import audio_edit
        audio_edit.playing["sid"] = sid
        name = r["title"] or Path(r["file_path"]).stem
        self.bar.load(r["file_path"], wave_key(r), r["duration"],
                      f"{name} — {r['artist']}" if r["artist"] else name)
        self.tracker.start(sid, r["duration"])
        get_db().mark_played(sid)
        hub.songChanged.emit(sid)
        self._repaint_play()

    def toggle_play(self, sid: int | None) -> None:
        if sid is None:
            return
        if sid == self.playing_sid:
            self.player.toggle()
        else:
            self.play(sid)
        self._repaint_play()

    def stop(self) -> None:
        self.player.pause()

    def stop_playing(self) -> None:
        """Let go of the file (before renaming or deleting it)."""
        self.player.unload()
        self.tracker.stop()
        self.playing_sid = None
        self.model.playing_sid = None
        self.model.playing = False
        from ..jobs import audio_edit
        audio_edit.playing["sid"] = None
        self._repaint_play()

    def _pos(self, pos: float) -> None:
        if self.tracker.on_pos(pos, self.player.is_playing()):
            self._count_listen()

    def _ended(self) -> None:
        if self.tracker.on_end():
            self._count_listen()
        self._repaint_play()

    def _count_listen(self) -> None:
        sid = self.tracker.sid
        if sid is not None:
            get_db().add_listen(sid)
            hub.songChanged.emit(sid)

    def _playing_changed(self, on: bool) -> None:
        self.model.playing = on
        self._repaint_play()

    def _repaint_play(self) -> None:
        self.model.playing = self.player.is_playing() if self.playing_sid is not None else False
        # only the play buttons change: repaint that column, not the whole table
        vp = self.table.viewport()
        x = self.table.columnViewportPosition(COL["play"])
        vp.update(QRect(x, 0, self.table.columnWidth(COL["play"]), vp.height()))
        self.detail.update_play()

    def open_file(self, path: str) -> None:
        """Open the song in whatever Windows plays it with (like double-clicking it in Explorer)."""
        if self.playing_sid is not None and self.player.is_playing():
            self.player.pause()
        if not open_with_default(path):
            hub.toast.emit("That song's file is missing.", "warn")

    def open_video(self, path: str) -> None:
        if not open_with_default(path):
            hub.toast.emit("That video isn't there any more.", "warn")

    # ---- actions --------------------------------------------------------------
    def fill_actions(self, m: QMenu, ids: list[int]) -> None:
        """Everything you can do to songs: used by the right-click menu, the Selected button and the More button."""
        ids = [i for i in ids if i is not None]
        if not ids:
            a = m.addAction("Select some songs first")
            a.setEnabled(False)
            return
        db = get_db()
        rows = [r for r in (db.song(i) for i in ids) if r is not None]
        n = len(ids)
        s = "s" if n > 1 else ""
        if n == 1:
            m.addAction(icon("play"), "Play", lambda: self.play(ids[0]))
        m.addAction(icon("star", C["amber"]), "Favourite / unfavourite", lambda: self.toggle_fav(ids))
        all_ok = rows and all(r["approved"] for r in rows)
        m.addAction(icon("badge", C["ok"]), "Remove approval" if all_ok else "Approve",
                    lambda: self.set_approved(ids, not all_ok))
        m.addAction(icon("check", C["acc"]), "Sounds fine — mark checked", lambda: self.mark_checked(ids))
        m.addAction(icon("tag"), "Categories…", lambda: self.category_menu(ids))
        m.addAction(icon("refresh"), "Look up again…", lambda: self.lookup(ids))
        m.addSeparator()
        n_id = sum(1 for r in rows if r["title"])
        a = m.addAction(icon("edit"), f"Rename to song title & tag file{s}" + (f" ({n_id})" if n > 1 else ""),
                        lambda: self.rename_files(ids))
        a.setEnabled(n_id > 0)
        if not n_id:
            a.setToolTip("Only identified songs have a title to use")
        m.addAction(icon("unlink", C["pink"]), "Remove video links…", lambda: self.unlink(ids))
        m.addSeparator()
        if n == 1 and rows:
            m.addAction(icon("cut", C["acc"]), "Trim or change volume…\tE", lambda: self.edit_audio(ids[0]))
            a = m.addAction(icon("film", C["acc"]), "Re-cut from its video…", lambda: self.edit_audio(ids[0], "video"))
            if not any(Path(x["video_path"]).exists() for x in db.sources(ids[0])):
                a.setEnabled(False)
                a.setToolTip("The video this song came from isn't there any more")
            from ..jobs.audio_edit import is_edited
            if is_edited(rows[0]):
                m.addAction(icon("undo"), "Back to the original audio", lambda: self.reset_audio(ids[0]))
        m.addAction(icon("volume", C["acc"]), f"Even out volume" + (f" ({n} songs)" if n > 1 else ""),
                    lambda: self.level(ids))
        if n == 1 and rows:
            r = rows[0]
            m.addAction(icon("edit"), "Edit info…", lambda: self.edit_info(ids[0]))
            m.addAction(icon("play", C["acc"]), "Open in your music player",
                        lambda p=r["file_path"]: self.open_file(p))
            m.addAction(icon("folder"), "Show file", lambda p=r["file_path"]: open_in_explorer(p))
            srcs = db.sources(ids[0])
            if srcs:
                m.addAction(icon("film", C["acc"]), "Play source video", lambda p=srcs[0]["video_path"]: self.open_video(p))
        m.addSeparator()
        m.addAction(icon("trash", C["danger"]), f"Delete {n} song{s}…", lambda: self.delete(ids))

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

    def set_approved(self, ids: list[int], on: bool) -> None:
        for i in ids:
            if i is not None:
                get_db().update_song(i, approved=int(on))
                hub.songChanged.emit(i)

    def toggle_approved(self, ids: list[int]) -> None:
        rows = [r for r in (get_db().song(i) for i in ids if i is not None) if r is not None]
        if rows:
            self.set_approved([r["id"] for r in rows], not all(r["approved"] for r in rows))

    def mark_checked(self, ids: list[int]) -> None:
        for i in ids:
            get_db().update_song(i, checked=1, approved=1)
            hub.songChanged.emit(i)

    def reidentify(self, ids: list[int], methods: list[str] | None = None) -> None:
        """Queue songs for another lookup. methods: one-off choice of services (None = the usual ones)."""
        ids = [i for i in ids if i is not None]
        db = get_db()
        val = ",".join(methods) if methods else None
        for k in range(0, len(ids), 400):
            chunk = ids[k:k + 400]
            db.x(f"UPDATE songs SET id_status='pending', id_methods=? WHERE id IN ({','.join('?' * len(chunk))})",
                 (val, *chunk))
        if len(ids) > 50:
            hub.songsReset.emit()
        else:
            for i in ids:
                hub.songChanged.emit(i)
        w = self.window()
        if hasattr(w, "identify"):
            w.identify.poke()

    def ask_lookup(self, ids: list[int]) -> list[str] | None:
        """Which services to use, or None if cancelled. Tests replace this."""
        db = get_db()
        durs = [r["duration"] or 0 for r in (db.song(i) for i in ids) if r is not None]
        d = LookupDialog(durs, self)
        return d.methods() if d.exec() else None

    def lookup(self, ids: list[int]) -> None:
        ids = [i for i in ids if i is not None]
        if not ids:
            return
        methods = self.ask_lookup(ids)
        if not methods:
            return
        self.reidentify(ids, methods)
        names = {"shazam": "Shazam", "deep": "Shazam (trying harder)", "acoustid": "AcoustID", "audiotag": "AudioTag"}
        hub.toast.emit(f"Looking up {len(ids)} song{'s' if len(ids) != 1 else ''} with "
                       f"{' + '.join(names[m] for m in methods)} in the background.", "ok")

    def lookup_unidentified(self) -> None:
        self.lookup([r["id"] for r in self.model.rows if not r["identified"] and r["status"] in ("not_found", "error")])

    def edit_info(self, sid: int) -> None:
        r = get_db().song(sid)
        if r is None:
            return
        d = EditInfoDialog(r, self)
        if d.exec():
            get_db().update_song(sid, manual_info=1, **d.values())
            hub.songChanged.emit(sid)
            if r["tagged"] and get_db().song(sid)["title"]:
                self.rename_files([sid], ask=False)       # keep the file's name and tags in step

    def rename_files(self, ids: list[int], ask: bool = True) -> None:
        db = get_db()
        named = [i for i in ids if (r := db.song(i)) is not None and r["title"]]
        if not named:
            hub.toast.emit("Only identified songs can be named after their title.", "warn")
            return
        if ask:
            names = file_ops.library_names(named[:3])
            eg = "\n".join(f"• {v}" for v in names.values())
            skipped = len(ids) - len(named)
            txt = (f"Rename {len(named)} song file{'s' if len(named) > 1 else ''} after the song and write the song "
                   f"info (title, artist, album, genre, year, cover) into {'them' if len(named) > 1 else 'it'}?"
                   f"\n\nFor example:\n{eg}\n\nCopies of the same song get what's different about them in the name "
                   "(length, BPM…). No file is ever overwritten, and the audio isn't changed.")
            if skipped:
                txt += f"\n\n{skipped} unidentified song{'s are' if skipped > 1 else ' is'} left as they are."
            if not dialogs.ask(self, "Rename & tag", txt, f"Rename {len(named)} file{'s' if len(named) > 1 else ''}"):
                return
        self._run_batch(file_ops.rename_batch, named, "Renaming",
                        lambda res: f"Renamed and tagged {len(res['done'])} song{'s' if len(res['done']) != 1 else ''}.")

    def unlink(self, ids: list[int], ask: bool = True) -> None:
        ids = [i for i in ids if i is not None]
        if not ids:
            return
        n = len(ids)
        if ask:
            txt = (f"Remove everything linking {'these ' + str(n) + ' songs' if n > 1 else 'this song'} to the videos "
                   f"{'they' if n > 1 else 'it'} came from?\n\n"
                   "• The list of source videos is deleted\n"
                   "• The file is renamed after the song (or “Unidentified song N” if it isn't identified)\n"
                   "• Any other info in the file is cleared\n"
                   "• Later scans never link it to a video again\n\nThis can't be undone.")
            if not dialogs.ask(self, "Remove video links", txt, "Remove the links", danger=True):
                return
        self._run_batch(file_ops.unlink_batch, ids, "Removing video links",
                        lambda res: f"Removed the video links from {len(res['done'])} song"
                                    f"{'s' if len(res['done']) != 1 else ''}.")

    def ask_delete(self, ids: list[int]) -> tuple[bool, bool] | None:
        """(skip videos, skip songs), or None if cancelled. Tests replace this."""
        db = get_db()
        rows = [r for r in (db.song(i) for i in ids) if r is not None]
        if not rows:
            return None
        txt = (f"Delete “{rows[0]['title'] or Path(rows[0]['file_path']).stem}”?" if len(rows) == 1
               else f"Delete {len(rows)} songs?")
        n_videos = len({s["video_path"] for r in rows for s in db.sources(r["id"])})
        d = DeleteDialog(txt, n_videos, len(rows), self)
        if not d.exec():
            return None
        return d.skip_videos.isChecked() and n_videos > 0, d.skip_songs.isChecked()

    def delete(self, ids: list[int]) -> None:
        ids = [i for i in ids if i is not None]
        if not ids:
            return
        ans = self.ask_delete(ids)
        if ans is None:
            return
        skip_videos, skip_songs = ans
        self._run_batch(file_ops.delete_songs, ids, "Deleting",
                        lambda res: f"Deleted {len(res['done'])} song{'s' if len(res['done']) != 1 else ''}.",
                        skip_videos, skip_songs, lambda p: to_recycle_bin(p))

    def _run_batch(self, fn, ids: list[int], verb: str, done_text, *extra) -> None:
        """Rename / unlink / delete in the background. Joins the queue if something else is running."""
        n = len(ids)

        def started_check(*_a):
            pass

        if self.playing_sid in ids:
            self.stop_playing()
        q = get_queue()
        self.batch = q.add(f"{verb} {n} song{'s' if n != 1 else ''}", fn, list(ids), *extra,
                           on_event=lambda k, p: hub.songChanged.emit(int(p)) if k == "song_changed" else None,
                           on_done=lambda res: self._batch_done(res, done_text),
                           on_failed=lambda msg: hub.toast.emit(f"{verb} failed: {msg}", "error"),
                           on_stopped=lambda: hub.toast.emit(f"Stopped {verb.lower()}. Finished songs are kept.", "warn"),
                           on_finished=self._batch_finished,
                           done_text=lambda res: done_text(res).rstrip("."), pausable=True)
        if q.current is not self.batch:
            hub.toast.emit(f"{verb} {n} song{'s' if n != 1 else ''} is queued after "
                           f"{q.current.name.lower() if q.current else 'the current job'}.", "info")

    def _batch_done(self, res: dict, done_text) -> None:
        msg = done_text(res)
        fails = res.get("failed") or []
        if fails:
            msg += f" {len(fails)} couldn't be done: {fails[0][1]}" + (" (and others)" if len(fails) > 1 else "")
        hub.toast.emit(msg, "warn" if fails else "ok")
        hub.movesChanged.emit()

    def _batch_finished(self) -> None:
        self.batch = None
        self.reload()

    # ---- audio edits ------------------------------------------------------------
    def edit_audio(self, sid: int | None, mode: str = "song") -> None:
        """Trim / volume / re-cut: the editor dialog, then a job in the queue."""
        if sid is None:
            return
        from ..jobs import audio_edit as ae
        from .audio_editor import AudioEditDialog
        try:
            d = AudioEditDialog(self, sid, mode)
        except ae.EditError as e:
            hub.toast.emit(str(e), "warn")
            return
        was_playing = sid == self.playing_sid
        if was_playing:
            self.player.pause()
        if not d.exec() or d.request is None:
            return
        req = d.request
        if req.mode == "reset":
            self.reset_audio(sid)
            return
        if req.mode == "video":
            self._edit_job("Re-cutting the song", sid, ae.recut, req.video, req.start, req.end, req.gain_db,
                           done=lambda res: f"Re-cut from the video · now {fmt_time(res['duration'])}")
        else:
            self._edit_job("Editing the song", sid, ae.render, trim_start=req.trim_start, trim_end=req.trim_end,
                           gain_db=req.gain_db,
                           done=lambda res: f"Saved · {fmt_time(res['duration'])}, {res['how']}")

    def _wave_menu(self, gpos) -> None:
        sid = self.playing_sid
        if sid is None:
            return
        m = QMenu(self)
        m.addAction(icon("cut", C["acc"]), "Trim or change volume…", lambda: self.edit_audio(sid))
        a = m.addAction(icon("film", C["acc"]), "Re-cut from its video…", lambda: self.edit_audio(sid, "video"))
        a.setEnabled(any(Path(x["video_path"]).exists() for x in get_db().sources(sid)))
        m.exec(gpos)

    def reset_audio(self, sid: int) -> None:
        from ..jobs import audio_edit as ae
        self._edit_job("Putting the original back", sid, ae.reset,
                       done=lambda res: f"Back to the original · {fmt_time(res['duration'])}")

    def _edit_job(self, name: str, sid: int, fn, *args, done, **kw) -> None:
        if self.playing_sid == sid:
            self.stop_playing()              # let go of the file so it can be swapped
        get_db().update_song(sid, level_done=1)    # a volume you set (or kept) isn't changed by evening out

        def run(ctx, *a):
            return fn(ctx, sid, *a, **kw)

        def finished(res):
            hub.songChanged.emit(sid)
            hub.toast.emit(done(res), "ok")
            if self.detail.sid == sid:
                self.detail.show_song(sid)

        get_queue().add(name, run, *args, on_done=finished, done_text=done,
                        on_failed=lambda msg: hub.toast.emit(f"{name} failed: {msg}", "error"))

    def level(self, ids: list[int]) -> None:
        """Even out the volume of these songs now, even ones whose volume was set by hand."""
        from ..jobs import audio_edit as ae
        n = len(ids)
        if n > 1 and not dialogs.ask(self, f"Even out the volume of {n} songs?",
                                     "Songs well away from the library's loudness are made louder or quieter; the "
                                     "rest are left as they are. Originals are kept, so Back to the original "
                                     "undoes it for any song.", f"Even out {n} songs"):
            return
        if self.playing_sid in ids:
            self.stop_playing()
        get_queue().add(f"Evening out {n} song{'s' if n != 1 else ''}", ae.level_songs, list(ids),
                        ae._is_playing, True, pausable=True,
                        on_event=lambda k, p: hub.songChanged.emit(int(p)) if k == "song_changed" else None,
                        done_text=lambda res: f"{res['changed']} of {res['measured']} songs changed",
                        on_done=lambda res: hub.toast.emit(
                            f"Evened out: {res['changed']} of {res['measured']} songs changed.", "ok"))

    # ---- categories -----------------------------------------------------------
    def new_category(self) -> int | None:
        name = dialogs.ask_text(self, "New category", "Name:", ok="Add category")
        if not name:
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
        m.addAction(icon("plus", C["acc"]), "New category…", lambda: self._new_and_add(ids))
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
        if it is None or it.data(Qt.UserRole) is None:
            return
        cid = it.data(Qt.UserRole)
        m = QMenu(self)
        m.addAction(icon("edit"), "Rename…", lambda: self._rename_cat(cid))
        m.addAction(icon("tag"), "Change colour…", lambda: self._recolor_cat(cid))
        m.addSeparator()
        m.addAction(icon("trash", C["danger"]), "Delete category", lambda: self._delete_cat(cid))
        m.exec(self.cat_list.mapToGlobal(pos))

    def _rename_cat(self, cid: int) -> None:
        cur = next((c["name"] for c in get_db().categories() if c["id"] == cid), "")
        name = dialogs.ask_text(self, "Rename category", "Name:", cur, ok="Rename")
        if name:
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
        nm = next((c["name"] for c in get_db().categories() if c["id"] == cid), "this category")
        if dialogs.ask(self, "Delete category", f"Delete the category “{nm}”? Its songs stay in the library.",
                       "Delete category", danger=True):
            get_db().delete_category(cid)
            if self.proxy.category == cid:
                self.proxy.set(category=None)
            hub.categoriesChanged.emit()

    # ---- columns --------------------------------------------------------------
    WIDTHS = {"play": 36, "fav": 30, "title": 200, "artist": 140, "length": 60, "full": 64, "bpm": 44, "genre": 92,
              "mood": 112, "folder": 120, "plays": 58, "cats": 110, "status": 130}
    # When the table is narrow, drop the least important columns so titles stay readable.
    AUTO_HIDE_ORDER = ("mood", "bpm", "full", "genre", "length", "plays", "cats", "folder")
    MIN_TEXT = 300                              # Title + Artist together
    OLD_COLS = [None, "title", "artist", "length", "full", "bpm", "genre", "mood", "cats", "status"]

    def _load_hidden(self) -> set[str]:
        s = get_settings()
        names = s.get("ui.library_hidden")
        if names is None:
            old = s.get("ui.hidden_columns", [4]) or []
            names = [self.OLD_COLS[i] for i in old if isinstance(i, int) and 0 <= i < len(self.OLD_COLS)
                     and self.OLD_COLS[i]]
        return {k for k in names if k in OPTIONAL}

    def _apply_columns(self) -> None:
        avail = self.table.viewport().width()
        if avail <= 50:
            avail = 900
        hidden = set(self.user_hidden)
        fixed = lambda: sum(w for k, w in self.WIDTHS.items() if k not in hidden and k not in ("title", "artist"))
        auto = set()
        for k in self.AUTO_HIDE_ORDER:
            if avail - fixed() >= self.MIN_TEXT:
                break
            if k not in hidden:
                hidden.add(k)
                auto.add(k)
        self.auto_hidden = auto
        for k in OPTIONAL:
            self.table.setColumnHidden(COL[k], k in hidden)

    def eventFilter(self, obj, ev):
        if obj is self.table.viewport():
            t = ev.type()
            if t == QEvent.Resize:
                QTimer.singleShot(0, self._apply_columns)
            elif t == QEvent.MouseButtonPress:
                # remembered for _clicked: a click only resumes a paused song if its row was already chosen
                cur = self.table.currentIndex()
                hit = self.table.indexAt(ev.position().toPoint())
                self._row_was_current = cur.isValid() and hit.isValid() and cur.row() == hit.row()
        return super().eventFilter(obj, ev)

    def _header_menu(self, pos) -> None:
        m = QMenu(self)
        for k in OPTIONAL:
            name = dict(COLS)[k]
            a = m.addAction(name)
            a.setCheckable(True)
            a.setChecked(k not in self.user_hidden)
            if k in self.auto_hidden:
                a.setText(f"{name}  (hidden — window too narrow)")
            a.toggled.connect(lambda on, key=k: self._show_col(key, on))
        m.exec(self.table.horizontalHeader().mapToGlobal(pos))

    def _show_col(self, key: str, on: bool) -> None:
        (self.user_hidden.discard if on else self.user_hidden.add)(key)
        get_settings().set("ui.library_hidden", sorted(self.user_hidden))
        self._apply_columns()

    def _table_menu(self, pos) -> None:
        ids = self.selected()
        if not ids:
            return
        m = QMenu(self)
        self.fill_actions(m, ids)
        m.exec(self.table.viewport().mapToGlobal(pos))
