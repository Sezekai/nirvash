"""Minimal mpv-in-Qt check, step by step (Windows CI)."""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def say(*a):
    print(*a, flush=True)


tmp = Path(tempfile.mkdtemp())
vid = tmp / "v.mp4"
wav = tmp / "a.wav"
subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-t", "6", "-c:v", "libx264",
                str(vid)], check=True, capture_output=True)
subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=f=440:d=6", str(wav)], check=True, capture_output=True)
say("media ready")
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

app = QApplication(sys.argv)
from nirvash.ui import player as pl  # noqa: E402

mpv = pl._load_mpv()
say("mpv module", mpv, pl._mpv_error)
mode = sys.argv[1] if len(sys.argv) > 1 else "full"
if mode == "raw":
    w = QWidget()
    w.resize(640, 360)
    w.show()
    app.processEvents()
    m = mpv.MPV(wid=str(int(w.winId())), log_handler=lambda lv, comp, msg: say("mpv", lv, comp, msg.strip()),
                loglevel="info")
    say("created")
    m.play(str(vid))
    for _ in range(60):
        app.processEvents()
        time.sleep(0.05)
    say("pos", m.time_pos)
    m.terminate()
    say("raw ok")
    sys.exit(0)

p = pl.Player(video=True)
p.resize(640, 360)
p.show()
app.processEvents()
say("player created", p.backend)
p.load(str(vid), audio_file=str(wav), play=True)
say("loaded")
for _ in range(80):
    app.processEvents()
    time.sleep(0.05)
say("pos", p.position(), "dur", p.duration(), "playing", p.is_playing())
p.seek(1.0)
for _ in range(20):
    app.processEvents()
    time.sleep(0.05)
say("after seek", p.position())
a = pl.Player(video=False)
a.load(str(wav), play=True)
for _ in range(40):
    app.processEvents()
    time.sleep(0.05)
say("audio pos", a.position())
p.shutdown()
a.shutdown()
say("full ok")
