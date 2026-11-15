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
os.environ.setdefault("NIRVASH_AUTO_DIALOGS", "reject")   # start-up questions answer "not now"
REAL = sys.platform == "win32"   # on Windows (CI) use real playback and grab the screen
if not REAL:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ["NIRVASH_NO_PLAYBACK"] = "1"

import numpy as np  # noqa: E402

from nirvash.core.db import get_db  # noqa: E402
from nirvash.core.settings import get_settings  # noqa: E402

s = get_settings()
songs_dir = s.folder("songs")
vids = tmp / "silent"
vids.mkdir()
src = Path(tempfile.gettempdir()) / "nv-media"
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
    sub = ["Anime", "Anime/Openings", "Edits", "Anime/Openings", "Edits/2024", "Anime", "Edits"][i]
    if i != 4:
        base = "E:\\Pictures\\IMGs\\Gifs & Vids\\" if REAL else "/media/Pictures/IMGs/Gifs & Vids/"
        db.add_source(sid, base + (sub.replace("/", "\\") if REAL else sub) + f"{'\\' if REAL else '/'}video_{i:02d}.mp4",
                      0, dur)
    db.update_song(sid, played=int(i % 3 != 2), listen_count=[2, 0, 0, 5, 0, 1, 3][i], approved=int(i in (0, 3, 5)),
                   last_played=1.7e9 if i % 3 != 2 else None)
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

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from nirvash.ui import theme  # noqa: E402

app = QApplication(sys.argv)
app.setStyle("Fusion")
theme.apply(app)
from nirvash.ui.main_window import MainWindow  # noqa: E402

MainWindow._check_models = lambda self: None
w = MainWindow()
w.resize(1480, 900)
w.show()


def wait(sec: float):
    import time
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.03)


def shoot():
    if REAL:
        w.go("auto")
        w.auto_page.table.selectRow(0)
        for _ in range(300):
            wait(0.1)
            if w.auto_page.player.duration() > 0 and not w.auto_page.status.text():
                break
        w.auto_page.player.play()
        wait(2.5)
        print("player:", w.auto_page.player.backend, "pos", round(w.auto_page.player.position(), 2),
              "dur", w.auto_page.player.duration(), "playing", w.auto_page.player.is_playing())
        app.primaryScreen().grabWindow(0).scaledToWidth(1100, Qt.SmoothTransformation).save(
            str(OUT / "auto_screen.jpg"), "JPG", 70)
        w.library.select_song(1)
        w.go("library")
        w.library.play(1)
        wait(2.0)
        print("library player pos", round(w.library.player.position(), 2), "playing", w.library.player.is_playing())
        app.primaryScreen().grabWindow(0).scaledToWidth(1100, Qt.SmoothTransformation).save(
            str(OUT / "library_screen.jpg"), "JPG", 70)
        import base64
        for n in ("auto_screen.jpg", "library_screen.jpg"):
            print(f"B64START {n}")
            b = base64.b64encode((OUT / n).read_bytes()).decode()
            for i in range(0, len(b), 3000):
                print(b[i:i + 3000], flush=True)
            print(f"B64END {n}")
    w.library.select_song(1)
    app.processEvents()

    def snap(name):
        for _ in range(25):
            app.processEvents()
        w.grab().save(str(OUT / f"{name}.png"))

    # the editor with a video, a crop, a blur box and an overlay
    import cv2 as _cv2
    import numpy as _np
    from nirvash.edit import overlays as _ovl
    _logo = tmp / "logo.png"
    _img = _np.zeros((120, 240, 4), _np.uint8)
    _cv2.rectangle(_img, (0, 0), (239, 119), (240, 120, 60, 230), -1)
    _cv2.putText(_img, "LOGO", (40, 80), _cv2.FONT_HERSHEY_SIMPLEX, 2, (255, 255, 255, 255), 5)
    _cv2.imwrite(str(_logo), _img)
    _sub = _ovl.new_folder(_ovl.root(), "Logos")
    _lib = _ovl.import_files([str(_logo)], _sub)
    ed = w.editor_page
    ed.open_files([str(src / "clip.mp4")])
    ed.set_tool("crop")
    ed.crop_on.setChecked(True)
    ed.aspect.setCurrentIndex(ed.aspect.findData("9:16"))
    ed._aspect_changed()
    ed.seek(8.0)
    _k = ed.p.crop.keys[0]
    ed._box_dragged("crop", "crop", (_k[1] + 0.25, _k[2] + 0.1, _k[3] * 0.7, _k[4] * 0.7), "video")
    ed.changed()
    ed._blur_drawn((0.1, 0.75, 0.35, 0.18), "output")
    ed.add_overlay(str(_lib[0]))
    ed._set_mark((2.0, 6.0))
    ed._add_section(6.0)
    ed.seek(9.0)
    ed.set_tool("crop")
    for width, height in ((1100, 720), (1440, 900), (1920, 1040)):
        w.resize(width, height)
        for key in ("library", "scan", "auto", "settings", "look", "editor"):
            w.go(key)
            if key == "auto":
                w.auto_page.table.selectRow(0)
            snap(f"{key}_{width}")
        w.go("editor")
        for _tool in ("blur", "sound"):
            ed.set_tool(_tool)
            snap(f"editor_{_tool}_{width}")
        ed.set_tool("crop")
        w.go("library")
        w.toggle_keys()
        snap(f"keys_{width}")
        w.toggle_keys()
    w.resize(1440, 900)
    w.go("library")
    w.library.set_filter("approved", 1)
    w.library.set_filter("played", 2)
    snap("library_filtered")
    w.library.clear_filters()
    # a job running in the status bar, with one more queued
    import time as _t
    from nirvash.ui.jobqueue import get_queue

    def slow(ctx, n):
        for i in range(n):
            ctx.check()
            ctx.progress(i / n, f"Video {i + 1} of {n} · beach walk.mp4")
            _t.sleep(0.05)

    get_queue().add("Finding music", slow, 400, pausable=True, ai=True)
    get_queue().add("Exporting", slow, 10)
    wait(1.5)
    snap("job_running")
    w.toggle_log(True)
    snap("log_open")
    w.toggle_log(True, "moves")
    snap("moves_open")
    w.toggle_log(False)
    get_queue().stop_all()
    wait(0.5)
    snap("job_stopped")
    w.toast.show_msg("Exported 2 videos next to the originals.", "ok")
    snap("toast")
    # the audio editor on the first song, trimmed a little and 3 dB louder
    from nirvash.ui.audio_editor import AudioEditDialog
    ed = AudioEditDialog(w, 1)
    ed.wave.set_range(4.5, 52.0, emit=True)
    ed.gain.setValue(6)
    ed.resize(960, ed.sizeHint().height())
    ed.show()
    wait(2.0)
    ed.grab().save(str(OUT / "audio_editor.png"))
    ed.done(0)
    # a light theme and a plain dark one
    from nirvash.core import appearance as ap
    for bg in ("daylight", "obsidian"):
        theme.apply(app, ap.with_background(None, bg))
        from nirvash.ui.hub import hub
        hub.themeChanged.emit()
        w.go("library")
        snap(f"theme_{bg}")
    print("saved to", OUT)
    app.quit()


QTimer.singleShot(1500, shoot)
app.exec()
