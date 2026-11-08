"""The library page's new controls: play buttons, filters (with opposites), folder tree, listens, approval,
rename & tag, removing video links, looking up again. Driven offscreen on synthetic songs."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["NIRVASH_NO_PLAYBACK"] = "1"

from tests.test_ui_flows import app, wait_for  # noqa: E402,F401
from tests.test_pipeline import sh  # noqa: E402


def visible_ids(lib):
    from nirvash.ui.library_page import ROLE_ROW
    return {lib.proxy.index(r, 0).data(ROLE_ROW)["id"] for r in range(lib.proxy.rowCount())}


def test_listen_tracker():
    from nirvash.ui.library_page import ListenTracker
    t = ListenTracker()
    t.start(1, 20)
    # Skipping to the end doesn't count.
    assert not t.on_pos(0.0, True)
    assert not t.on_pos(19.5, True)
    assert not t.on_end()
    # Playing it through does, once.
    t.start(1, 20)
    hits = sum(t.on_pos(i * 0.25, True) for i in range(0, 81))
    assert hits == 1 and not t.on_end()
    # Starting again from the top can count again.
    hits = sum(t.on_pos(i * 0.25, True) for i in range(0, 81))
    assert hits == 1
    # Paused time doesn't count as listening.
    t.start(2, 10)
    for i in range(0, 41):
        t.on_pos(i * 0.25, i % 2 == 0)
    assert not t.counted


def test_library_controls(app, tmp_path):
    from nirvash.core.db import get_db
    from nirvash.core.settings import get_settings
    from nirvash.ui.main_window import MainWindow
    from nirvash.ui.library_page import COL

    db = get_db()
    songs = get_settings().folder("songs")
    vids = tmp_path / "Videos"
    ids = {}
    for name, folder, info in (("a", "Holiday/Beach", {"title": "Sunny", "artist": "Band"}),
                               ("b", "Holiday/Beach", {}),
                               ("c", "Holiday/Mountains", {"title": "Sunny", "artist": "Band"}),
                               ("d", "Work", {})):
        p = songs / f"{name} video.m4a"
        sh("ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=d={4 + len(ids)}", "-c:a", "aac", "-f", "ipod", str(p))
        sid = db.add_song(file_path=str(p), codec="aac", ext=".m4a", duration=4 + len(ids), bitrate=128000,
                          id_status="found" if info else "not_found", **info)
        (vids / folder).mkdir(parents=True, exist_ok=True)
        db.add_source(sid, str(vids / folder / f"{name} video.mp4"), 0, 4)
        ids[name] = sid
    e = db.add_song(file_path=str(songs / "e.m4a"), duration=3, id_status="error")
    ids["e"] = e

    w = MainWindow()
    w.resize(1500, 900)
    w.go("library")
    lib = w.library
    lib.reload()
    assert visible_ids(lib) == set(ids.values())

    # Folder tree: Holiday holds Beach + Mountains (3 songs); picking it includes subfolders.
    tree = lib.folder_tree
    items = []
    stack = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
    while stack:
        it = stack.pop()
        items.append(it)
        stack += [it.child(i) for i in range(it.childCount())]
    by = {it.text(0).replace("\\", "/").split("/")[-1]: it for it in items}
    assert by["Holiday"].text(1) == "3" and by["Beach"].text(1) == "2" and by["Work"].text(1) == "1"
    assert by["No source video"].text(1) == "1"
    tree.setCurrentItem(by["Holiday"])
    assert visible_ids(lib) == {ids["a"], ids["b"], ids["c"]}
    tree.setCurrentItem(by["Beach"])
    assert visible_ids(lib) == {ids["a"], ids["b"]}
    tree.setCurrentItem(by["No source video"])
    assert visible_ids(lib) == {ids["e"]}
    tree.setCurrentItem(by["All folders"])

    # Sorting by folder groups songs by where they came from.
    from PySide6.QtCore import Qt
    lib.table.sortByColumn(COL["folder"], Qt.AscendingOrder)
    from nirvash.ui.library_page import ROLE_ROW
    order = [lib.proxy.index(r, 0).data(ROLE_ROW)["folder_full"] for r in range(lib.proxy.rowCount())]
    assert order == sorted(order, key=str.casefold)

    # Play button: starts, marks played; the Played filter and its opposite.
    lib.toggle_play(ids["a"])
    assert lib.playing_sid == ids["a"] and get_db().song(ids["a"])["played"] == 1
    wait_for(app, lambda: lib.model.rows[lib.model.index_of[ids["a"]]]["played"], 3)
    lib.set_filter("played", 1)
    assert visible_ids(lib) == {ids["a"]}
    lib.set_filter("played", 2)
    assert visible_ids(lib) == set(ids.values()) - {ids["a"]}
    lib.clear_filters()

    # A full listen (simulated positions) counts once.
    for i in range(0, 4 * 4 + 1):
        lib.tracker.on_pos(i * 0.25, True) and lib._count_listen()
    assert get_db().song(ids["a"])["listen_count"] == 1

    # Approved / not approved, combined with identified / not identified.
    lib.set_approved([ids["a"], ids["b"]], True)
    lib.mark_checked([ids["d"]])
    assert get_db().song(ids["d"])["approved"] == 1 and get_db().song(ids["d"])["checked"] == 1
    wait_for(app, lambda: lib.model.rows[lib.model.index_of[ids["d"]]]["approved"], 3)
    lib.set_filter("approved", 1)
    lib.set_filter("identified", 2)
    assert visible_ids(lib) == {ids["b"], ids["d"]}
    lib.set_filter("approved", 2)
    assert visible_ids(lib) == {ids["e"]}
    lib.clear_filters()
    # Times listened through
    lib.listen_box.setCurrentIndex(2)          # Listened once
    lib._listens_changed()
    assert visible_ids(lib) == {ids["a"]}
    lib.clear_filters()

    # Look up the unidentified ones again (not_found + error), not the identified ones.
    assert "(3)" in lib.lookup_btn.text()
    from nirvash.ui.library_page import LookupDialog
    d = LookupDialog([4, 30, 200], lib)
    d.boxes["acoustid"].setChecked(True)
    d.key.setText("")
    assert not d.ok.isEnabled()                       # AcoustID needs a key first
    d.boxes["acoustid"].setChecked(False)
    d.boxes["deep"].setChecked(True)
    assert d.methods() == ["deep"] and d.ok.isEnabled() and "lookups" in d.est.text()
    d.deleteLater()
    lib.ask_lookup = lambda ids_: ["shazam"]
    lib.lookup_unidentified()
    st = {k: get_db().song(v)["id_status"] for k, v in ids.items()}
    assert st == {"a": "found", "b": "pending", "c": "found", "d": "pending", "e": "pending"}
    assert get_db().song(ids["b"])["id_methods"] == "shazam" and get_db().song(ids["a"])["id_methods"] is None
    opened_songs = []
    import nirvash.ui.library_page as lp_
    lp_.open_with_default = lambda p: (opened_songs.append(p), True)[1]
    lib.open_file(get_db().song(ids["a"])["file_path"])
    assert opened_songs

    # Rename & tag the two copies of "Sunny" (playing one is let go first).
    w.identify.stop()
    lib.rename_files([ids["a"], ids["c"], ids["b"]])
    assert wait_for(app, lambda: lib.batch is None, 60)
    na, nc = Path(get_db().song(ids["a"])["file_path"]).name, Path(get_db().song(ids["c"])["file_path"]).name
    assert na == "Band - Sunny (0m 04s).m4a" and nc == "Band - Sunny (0m 06s).m4a", (na, nc)
    assert Path(get_db().song(ids["b"])["file_path"]).name == "b video.m4a"
    assert lib.playing_sid is None

    # Remove video links from b: it moves to "No source video" and is renamed.
    lib.unlink([ids["b"]])
    assert wait_for(app, lambda: lib.batch is None, 60)
    r = get_db().song(ids["b"])
    assert r["unlinked"] and not get_db().sources(ids["b"])
    assert Path(r["file_path"]).name == f"Unidentified song {ids['b']} (0m 05s).m4a"
    lib.set_filter("linked", 2)
    assert visible_ids(lib) == {ids["b"], ids["e"]}
    lib.clear_filters()

    # The detail panel shows sources with play-video buttons; everything paints.
    lib.select_song(ids["c"])
    wait_for(app, lambda: lib.detail.sid == ids["c"], 5)
    opened = []
    import nirvash.ui.library_page as lp
    lp.open_with_default = lambda p: (opened.append(p), True)[1]
    lib.open_video(get_db().sources(ids["c"])[0]["video_path"])
    assert opened
    assert not w.grab().isNull()
    w.close()
