"""Windows playback stress: play many songs in a row, switch rapidly, preview auto-music, on a real screen.

NIRVASH_OLD_CRASHLOG=1 + NIRVASH_MPV_SCRIPTS=1 reproduce the old setup (Python faulthandler + mpv Lua scripts)."""
from __future__ import annotations

import os
import subprocess
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


def say(*a):
    print(*a, flush=True)


from nirvash.core.db import get_db  # noqa: E402
from nirvash.core.settings import get_settings  # noqa: E402

songs = get_settings().folder("songs")
fmts = [("aac", ".m4a"), ("libopus", ".opus"), ("libmp3lame", ".mp3"), ("aac", ".m4a")]
db = get_db()
for i in range(12):
    codec, ext = fmts[i % len(fmts)]
    f = songs / f"song {i}{ext}"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=f={220 + 40 * i}:d={20 + i}",
                    "-c:a", codec, str(f)], check=True)
    db.add_song(file_path=str(f), duration=20.0 + i, title=f"Song {i}", id_status="found", codec=codec, ext=ext)
silent = tmp / "silent"
silent.mkdir()
for i in range(3):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-t", "15",
                    "-c:v", "libx264", "-preset", "ultrafast", str(silent / f"clip {i}.mp4")], check=True)
say("media ready")

if os.environ.get("NIRVASH_OLD_CRASHLOG"):
    import faulthandler
    faulthandler.enable(file=open(tmp / "crash.log", "a"), all_threads=True)
    say("old crash log (faulthandler) enabled")
else:
    from nirvash.app import _crash_log
    _crash_log()

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication(sys.argv)
from nirvash.ui import theme  # noqa: E402

theme.apply(app)
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
lib = w.library
ids = [r["id"] for r in db.songs()]
moved = 0
say("library player:", lib.player.backend)
for n, sid in enumerate(ids):
    lib.play(sid)
    pump(1.2)
    lib.player.seek(5)
    pump(0.3)
    moved += lib.player.position() > 0.5
    say(f"played {n + 1}/{len(ids)} pos={lib.player.position():.1f} playing={lib.player.is_playing()}")
for n in range(40):                       # rapid switching
    lib.play(ids[n % len(ids)])
    pump(0.15)
say("rapid switching done")
lib.player.toggle()
pump(0.3)
lib.player.toggle()
pump(0.5)

auto = w.auto_page
w.go("auto")
auto.folder = str(silent)
auto.items = []
auto.find_songs()
end = time.time() + 120
while auto.job is not None and time.time() < end:
    pump(0.2)
say("auto items", len(auto.items))
for r in range(len(auto.items)):
    auto.table.selectRow(r)
    pump(3)
    say(f"preview {r}: pos={auto.player.position():.1f} dur={auto.player.duration():.1f}")
for r in range(10):
    auto.table.selectRow(r % max(1, len(auto.items)))
    pump(0.4)
if lib.player.backend == "mpv" and moved < len(ids) // 2:
    say(f"PLAYBACK DIDN'T RUN ({moved} of {len(ids)} songs moved)")
    sys.exit(3)
say("PLAYBACK OK")
w.identify.stop()
w.close()
