"""Render each page offscreen with sample data and save PNGs (for checking the look)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/nv-shots")
OUT.mkdir(parents=True, exist_ok=True)
tmp = Path(tempfile.mkdtemp())
os.environ["NIRVASH_DATA"] = str(tmp / "data")
os.environ["NIRVASH_ROOT"] = str(tmp / "root")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["NIRVASH_NO_PLAYBACK"] = "1"

import numpy as np  # noqa: E402

from nirvash.core.db import get_db  # noqa: E402
from nirvash.core.settings import get_settings  # noqa: E402

s = get_settings()
songs_dir = s.folder("songs")
vids = tmp / "silent"
vids.mkdir()
src = Path("/tmp/nv-media")
if not (src / "song.m4a").exists():
    src.mkdir(exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=f=220:d=60", "-f", "lavfi", "-i", "sine=f=330:d=60",
                    "-filter_complex", "amix=2,volume=4,tremolo=f=4:d=0.8", "-c:a", "aac", str(src / "song.m4a")],
                   check=True, capture_output=True)
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30", "-t", "20", "-c:v",
                    "libx264", "-preset", "ultrafast", str(src / "clip.mp4")], check=True, capture_output=True)
db = get_db()
cats = [db.add_category(n, c) for n, c in (("Chill", "#2fe0c0"), ("Hype", "#ff6fb1"), ("Anime OPs", "#9b86ff"),
                                            ("Lo-fi", "#ffbf5a"))]
demo = [
    ("Blue Bird", "Ikimonogakari", "J-Pop", 128, 223.0, 94.0, {"energetic": .5, "happy": .3}, "found", 1, 0),
    ("Unravel", "TK from Ling tosite sigure", "Rock", 135, 238.0, 89.5, {"dramatic": .6, "dark": .3}, "found", 0, 0),
    ("Night Drive", "", "", 92, None, 61.2, {"calm": .5, "dreamy": .4}, "not_found", 0, 1),
    ("Gurenge", "LiSA", "Anime", 134, 239.0, 90.0, {"epic": .5, "energetic": .4}, "found", 1, 0),
    (None, None, None, 110, None, 34.0, {"cool": .6, "playful": .2}, "pending", 0, 1),
    ("Snowfall", "øneheart", "Lo-fi", 80, 141.0, 141.0, {"calm": .7, "sad": .2}, "found", 0, 0),
    ("Idol", "YOASOBI", "J-Pop", 166, 213.0, 88.0, {"happy": .5, "energetic": .4}, "found", 1, 0),
]
for i, (t, a, g, bpm, full, dur, mood, st, fav, partial) in enumerate(demo):
    f = songs_dir / f"video_{i:02d}{' [0.12-1.34]' if partial else ''}.m4a"
    shutil.copy(src / "song.m4a", f)
    sid = db.add_song(file_path=str(f), codec="aac", ext=".m4a", sample_rate=44100, channels=2, bitrate=192000,
                      duration=dur, title=t, artist=a, genre=g, bpm=bpm, full_length=full, mood=json.dumps(mood),
                      id_status=st, id_source="Shazam" if st == "found" else None, favourite=fav, partial=partial,
                      energy=0.6, pace=0.6, clip_offset=31.0 if full else None, album="Single" if t else None,
                      year="2021" if t else None,
                      lyrics_plain="Line one of the lyrics\nLine two\n\nChorus line" if st == "found" else None)
    db.add_source(sid, f"E:\\Pictures\\IMGs\\Gifs & Vids\\video_{i:02d}.mp4", 0, dur)
    if i % 2 == 0:
        db.set_song_category(sid, cats[i % 4], True)
    if i == 0:
        db.set_song_category(sid, cats[1], True)
from nirvash.jobs import automusic as am  # noqa: E402

items = []
for i, n in enumerate(["beach walk.mp4", "city timelapse.mp4", "cat zoomies.mp4", "dance practice.mp4",
                       "sunset drone.mp4"]):
    p = vids / n
    shutil.copy(src / "clip.mp4", p)
    it = am.AutoItem(str(p), duration=20.0 + i * 11, width=1280, height=720, fps=30, vcodec="h264",
                     features={"pace": 0.3 + i * 0.1, "mood": {"calm": .5, "happy": .5}}, song_id=(i % 5) + 1,
                     score=0.8, alts=[[2, .78], [4, .71], [6, .69], [7, .6]], status="matched", offset=12.5,
                     ready=i < 2)
    items.append(it)
items[3].status = "exported"
items[3].output = str(vids / "dance practice_edited.mp4")
am.save_session(str(vids), items)

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from nirvash.ui import theme  # noqa: E402

app = QApplication(sys.argv)
app.setStyle("Fusion")
theme.apply_font(app)
theme.check_svg_resource()
app.setStyleSheet(theme.fixed_stylesheet())
from nirvash.ui.main_window import MainWindow  # noqa: E402

MainWindow._check_models = lambda self: None
w = MainWindow()
w.resize(1480, 900)
w.show()


def shoot():
    w.library.select_song(1)
    app.processEvents()
    for key in ("library", "scan", "auto", "settings"):
        w.go(key)
        if key == "auto":
            w.auto_page.table.selectRow(0)
        for _ in range(20):
            app.processEvents()
        w.grab().save(str(OUT / f"{key}.png"))
    w.toast.show_msg("Exported 2 videos next to the originals.", "ok")
    app.processEvents()
    w.grab().save(str(OUT / "toast.png"))
    print("saved to", OUT)
    app.quit()


QTimer.singleShot(1500, shoot)
app.exec()
