"""Every delegate and model a view paints from must be owned by Qt (have a parent).

A view keeps only a raw pointer to its delegates/models; if Python owns one and drops it, the view paints through a
dangling pointer. That crashed Qt6Widgets.dll on Windows (library Category/Status columns)."""
from __future__ import annotations

import gc
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["NIRVASH_NO_PLAYBACK"] = "1"


def test_views_own_their_delegates_and_models(tmp_path, monkeypatch):
    monkeypatch.setenv("NIRVASH_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("NIRVASH_ROOT", str(tmp_path / "root"))
    import nirvash.core.db as dbm
    import nirvash.core.settings as sm
    sm._settings = None
    dbm._db = None
    import shiboken6
    from PySide6.QtWidgets import QAbstractItemView, QApplication

    app = QApplication.instance() or QApplication([])
    from nirvash.core.db import get_db
    db = get_db()
    for i in range(30):
        sid = db.add_song(file_path=str(tmp_path / f"s{i}.m4a"), duration=60.0, title=f"Song {i}", id_status="pending",
                          partial=i % 2)
    cid = db.add_category("Chill", "#2fe0c0")
    db.set_song_category(1, cid, True)

    from nirvash.ui.main_window import MainWindow
    from nirvash.ui.song_picker import SongPicker
    from nirvash.jobs.automusic import AutoItem
    monkeypatch.setattr(MainWindow, "_check_models", lambda self: None)
    w = MainWindow()
    w.identify.stop()
    picker = SongPicker(AutoItem(str(tmp_path / "v.mp4"), duration=30.0), w)
    gc.collect()

    problems = []
    roots = [w, picker]
    for root in roots:
        for view in root.findChildren(QAbstractItemView):
            name = f"{type(view.window()).__name__}/{type(view).__name__}"
            m = view.model()
            if m is not None and m.parent() is None:
                problems.append(f"{name}: model {type(m).__name__} has no owner")
            d = view.itemDelegate()
            if d is not None and d.parent() is None and shiboken6.ownedByPython(d):
                problems.append(f"{name}: delegate {type(d).__name__} has no owner")
            if m is not None:
                from PySide6.QtCore import QModelIndex
                try:
                    ncols, nrows = m.columnCount(QModelIndex()), m.rowCount(QModelIndex())
                except TypeError:  # Qt-internal list models hide these
                    ncols = nrows = 1
                for c in range(ncols):
                    cd = view.itemDelegateForColumn(c)
                    if cd is not None and cd.parent() is None:
                        problems.append(f"{name}: column {c} delegate {type(cd).__name__} has no owner")
                for r in range(min(nrows, 3)):
                    rd = view.itemDelegateForRow(r)
                    if rd is not None and rd.parent() is None:
                        problems.append(f"{name}: row {r} delegate {type(rd).__name__} has no owner")
    assert not problems, problems
    from PySide6.QtWidgets import QStyledItemDelegate
    from nirvash.ui.library_page import ChipDelegate
    owned = [d for d in w.library.table.findChildren(QStyledItemDelegate) if isinstance(d, ChipDelegate)]
    assert len(owned) == 2, "library Category/Status delegates must be children of the table"
    for col in (8, 9):
        assert w.library.table.itemDelegateForColumn(col) in owned
    # And it actually paints the rows with those delegates after a collection.
    w.go("library")
    w.resize(1400, 800)
    gc.collect()
    img = w.grab()
    assert not img.isNull()
    picker.player.shutdown()
    w.close()
