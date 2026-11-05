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
                      id_status=rng.choice(st), partial=i % 4 == 0, favourite=i % 7 == 0,
                      lyrics_plain="歌詞 🎤\nline" if i % 5 == 0 else None)
    db.add_source(sid, f"E:\\vids\\{s(rng)[:80]}.mp4", 0, 10)
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

theme.apply_font(app)
theme.check_svg_resource()
app.setStyleSheet(theme.fixed_stylesheet())
from nirvash.ui.main_window import MainWindow  # noqa: E402

MainWindow._check_models = lambda self: None
w = MainWindow()
w.identify.pause(True)
w.show()


def pump(sec):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


pump(1)
print("window shown", flush=True)

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
        hub.identifyStatus.emit(r.randint(0, 300), s(r))
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
for col in range(1, 10):
    t.sortByColumn(col, Qt.AscendingOrder)
    pump(0.05)
print("sorted", flush=True)
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
