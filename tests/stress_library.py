"""Windows stress check: a big library with emoji, flags, CJK, zero-width spaces etc. on a real screen.

Builds a synthetic library (no real data), opens the main window, scrolls the whole library, selects songs,
opens their details, and switches pages. Prints "STRESS OK" at the end; a native crash shows in crash.log."""
from __future__ import annotations

import json
import os
import random
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
tmp = Path(tempfile.mkdtemp())
os.environ["NIRVASH_DATA"] = str(tmp / "data")
os.environ.setdefault("NIRVASH_AUTO_DIALOGS", "accept")
os.environ["NIRVASH_ROOT"] = str(tmp / "root")
N = int(sys.argv[1]) if len(sys.argv) > 1 else 1600

POOL = ["Gurenge", "Я твой номер один", "Dale Pal Piso (feat. Ñengo Flow)", "紅蓮華", "夜に駆ける", "アイドル",
        "Summer 🔥🔥", "Pizza 🍕🍷 night", "🇵🇷 Boricua", "zero\u200bwidth\u200bspace", "「Unravel」",
        "Love 😍😋🤤🥵", "Rocket 🚀💫🖤", "LOL 🤣😭😫😳🤭", "全角\u3000スペース", "Café déjà vu", "Ελληνικά", "עברית",
        "العربية", "हिन्दी", "한국어 노래", "x" * 170, "A" + "\u0301" * 3, "tab\there", "nul-ish \x01 char"]


def s(rng):
    return " ".join(rng.choice(POOL) for _ in range(rng.randint(1, 3)))[:200]


from nirvash.core.db import get_db  # noqa: E402
from nirvash.core.settings import get_settings  # noqa: E402

rng = random.Random(1)
db = get_db()
songs_dir = get_settings().folder("songs")
st = ["found", "pending", "not_found", "error"]
for i in range(N):
    f = songs_dir / f"{s(rng)[:60].replace('/', '_')} {i}.m4a"
    sid = db.add_song(file_path=str(f), codec="aac", ext=".m4a", duration=rng.uniform(5, 400),
                      title=s(rng) if i % 3 else None, artist=s(rng) if i % 2 else None,
                      genre=rng.choice(["J-Pop", "Rock", "", None, "Música 🎶"]), bpm=rng.choice([None, 128.0]),
                      full_length=rng.choice([None, 200.0]), mood=json.dumps({"calm": .6, "happy": .4}),
                      id_status="pending" if i < 300 else rng.choice(st), partial=i % 4 == 0, favourite=i % 7 == 0,
                      lyrics_plain="歌詞 🎤\nline" if i % 5 == 0 else None)
    sub = "\\".join(s(rng)[:30].replace("\\", "_").replace("/", "_").strip() or "x" for _ in range(rng.randint(0, 3)))
    if i % 11:
        db.add_source(sid, f"E:\\vids\\{sub + chr(92) if sub else ''}{s(rng)[:80]}.mp4", 0, 10)
    db.update_song(sid, played=i % 3 == 0, listen_count=i % 4, approved=i % 6 == 0, unlinked=i % 11 == 0)
cid = db.add_category("Chill 🌙", "#2fe0c0")
for i in range(1, N, 5):
    db.set_song_category(i, cid, True)
print("library built", N, flush=True)

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from nirvash.app import _crash_log  # noqa: E402

_crash_log()
app = QApplication(sys.argv)
from nirvash.ui import theme  # noqa: E402

theme.apply(app)
from nirvash.ui.main_window import MainWindow  # noqa: E402

MainWindow._check_models = lambda self: None
w = MainWindow()   # identification runs (song files don't exist, so each lookup fails fast) like at real startup
w.show()


def pump(sec):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


pump(1)
print("window shown", flush=True)
import faulthandler  # noqa: E402
faulthandler.dump_traceback_later(600, repeat=True)   # every 10 min: where it is (slow under page heap, or stuck)

# Background updates like song identification does: DB writes + change signals from another thread.
import threading  # noqa: E402

from nirvash.ui.hub import hub  # noqa: E402

stop = threading.Event()


def churn():
    r = random.Random(2)
    n = 0
    while not stop.is_set():
        sid = r.randint(1, N)
        db.update_song(sid, id_status=r.choice(st), title=s(r), genre=r.choice(["Pop 🎵", "J-Rock", None]))
        hub.songChanged.emit(sid)
        hub.identifyStatus.emit({"pending": r.randint(0, 300), "current": s(r), "done": n, "started": 0, "busy": True})
        n += 1
        if n % 50 == 0:
            new = db.add_song(file_path=str(songs_dir / f"new {n}.m4a"), duration=30.0, title=s(r), id_status="pending")
            hub.songChanged.emit(new)
        time.sleep(0.002)


th = threading.Thread(target=churn, daemon=True)
th.start()
lib = w.library
t = lib.table
sb = t.verticalScrollBar()
step = max(1, sb.pageStep())
v = 0
while v <= sb.maximum():
    sb.setValue(v)
    t.viewport().repaint()
    app.processEvents()
    v += step
print("scrolled", sb.maximum(), flush=True)
for row in range(0, lib.proxy.rowCount(), max(1, lib.proxy.rowCount() // 60)):
    t.selectRow(row)
    pump(0.02)
print("selected rows", flush=True)
from nirvash.ui.library_page import COLS, FILTERS  # noqa: E402

for col in range(len(COLS)):
    t.sortByColumn(col, Qt.AscendingOrder)
    pump(0.05)
print("sorted", flush=True)
for k in FILTERS:
    for st_ in (1, 2, 0):
        lib.set_filter(k, st_)
        pump(0.03)
lib.set_filter("approved", 1)
lib.set_filter("identified", 2)
pump(0.2)
lib.clear_filters()
tree = lib.folder_tree


def tree_items():
    # The tree is rebuilt when new songs arrive (the churn thread adds some), so look items up fresh each time.
    out, stack = [], [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
    while stack:
        it = stack.pop()
        out.append(it)
        stack += [it.child(i) for i in range(it.childCount())]
    return out


keys = [it.data(0, Qt.UserRole) for it in tree_items()][:80]
for k in keys:
    it = next((x for x in tree_items() if x.data(0, Qt.UserRole) == k), None)
    if it is not None:
        tree.setCurrentItem(it)
        it.setExpanded(True)
    pump(0.02)
tree.setCurrentItem(tree.topLevelItem(0))
print("filters and folders", len(keys), flush=True)
for sid in (1, 2, 3):
    lib.select_song(sid)
    pump(0.1)
    lib.toggle_play(sid)
    pump(0.1)
print("detail + play buttons", flush=True)
for key in ("scan", "auto", "settings", "library"):
    w.go(key)
    pump(0.3)
lib.search.setText("🔥")
pump(0.3)
lib.search.setText("")
for _ in range(3):
    for row in range(0, lib.proxy.rowCount(), max(1, lib.proxy.rowCount() // 40)):
        t.selectRow(row)
        pump(0.05)
stop.set()
pump(1)
print("STRESS OK", flush=True)
w.identify.stop()
w.close()
