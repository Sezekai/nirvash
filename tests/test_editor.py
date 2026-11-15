"""The editor engine on generated videos: lossless trims/joins/turns, re-encoded effects, tracking, overlays."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_pipeline import env, sh  # noqa: E402,F401


def _video(path: Path, w=320, h=240, d=4.0, fps=25, src="testsrc2", audio=True):
    args = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"{src}=s={w}x{h}:r={fps}:d={d}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=f=440:d={d}"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-g", str(fps), "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac", "-shortest"]
    sh(*args, str(path))
    return path


def _halves(path: Path, d=3.0):
    """Left half red, right half blue."""
    sh("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=red:s=160x120:r=25:d={d}", "-f", "lavfi", "-i",
       f"color=c=blue:s=160x120:r=25:d={d}", "-filter_complex", "hstack", "-c:v", "libx264", "-preset", "ultrafast",
       "-pix_fmt", "yuv420p", str(path))
    return path


def _frame(path: Path, t: float) -> np.ndarray:
    from nirvash.media import ff
    pr = ff.probe(path)
    w, h = pr.width, pr.height
    r = ff.run([ff.ffmpeg(), "-v", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1", "-f", "rawvideo",
                "-pix_fmt", "bgr24", "pipe:1"])
    return np.frombuffer(r.stdout[: w * h * 3], np.uint8).reshape(h, w, 3)


class Ctx:
    def __init__(self):
        import threading
        self.cancel = threading.Event()
        self.msgs = []

    def progress(self, f, m=""):
        self.msgs.append((f, m))

    def check(self):
        if self.cancel.is_set():
            raise InterruptedError()

    def event(self, *a):
        pass


def test_project_basics():
    from nirvash.edit.project import Clip, Project, interp, set_key
    p = Project(clips=[Clip("a.mp4", 1, 3, duration=10, width=640, height=360, fps=25),
                       Clip("b.mp4", 0, 0, duration=4, width=1920, height=1080, fps=50)])
    assert p.duration == 6 and p.clip_at(1.5) == (0, 2.5) and p.clip_at(3.0) == (1, 1.0)
    assert p.canvas() == (1920, 1080, 50.0)
    # a box set by hand stays put until the next one, then moves there over the transition, arriving on time
    keys = [[0.0, 0, 0, 0.5, 0.5, "h"]]
    set_key(keys, 2.0, (0.5, 0.5, 0.5, 0.5))
    assert interp(keys, 1.0, 0.5) == (0, 0, 0.5, 0.5)
    mid = interp(keys, 1.75, 0.5)
    assert 0.2 < mid[0] < 0.3
    assert interp(keys, 2.0, 0.5) == (0.5, 0.5, 0.5, 0.5) and interp(keys, 9.0, 0.5) == (0.5, 0.5, 0.5, 0.5)
    assert interp(keys, 1.99, 0.0) == (0, 0, 0.5, 0.5)            # no transition: a cut
    set_key(keys, 0.01, (0.1, 0.1, 0.2, 0.2))                        # at the very start: the starting box
    assert keys[0] == [0.0, 0.1, 0.1, 0.2, 0.2, "h"] and len(keys) == 2
    # tracked keys ("l") are followed in straight lines
    tk = [[0.0, 0, 0, 0.1, 0.1, "h"], [1.0, 0.2, 0, 0.1, 0.1, "l"], [2.0, 0.4, 0, 0.1, 0.1, "l"]]
    assert abs(interp(tk, 1.5)[0] - 0.3) < 1e-9
    # fixing a part: the box stays from 0.5 to 1.5, then goes back to the tracked path
    from nirvash.edit.project import apply_section
    apply_section(tk, 0.5, 1.5, (0.9, 0.9, 0.1, 0.1), 0.25, 6.0)
    assert interp(tk, 1.0, 0.25)[0] == 0.9 and abs(interp(tk, 1.75, 0.25)[0] - 0.35) < 0.02
    # output: the chosen shape, as big as fits; boxes are fitted into it
    p.crop.enabled = True
    p.crop.aspect = "1:1"
    assert p.output_size() == (1080, 1080)
    p.crop.aspect = "source"
    assert p.output_size() == (1920, 1080)
    p.crop.keys = [[0.0, 0.25, 0.25, 0.5, 0.5, "h"]]
    box = (0.3, 0.3, 0.1, 0.1)
    back = p.output_to_canvas(p.canvas_to_output(box, 0.0), 0.0)
    assert all(abs(a - b) < 1e-9 for a, b in zip(back, box))
    assert p.canvas_to_output((0.25, 0.25, 0.5, 0.5), 0) == (0.0, 0.0, 1.0, 1.0)
    q = Project.from_json(p.to_json())
    assert q.to_json() == p.to_json() and q.frame_effects()


def test_lossless_trim_turn_join(env):
    from nirvash.edit.project import Project, clip_from_file
    from nirvash.edit.render import export, lossless_plan
    from nirvash.media import ff
    a = _video(env / "a.mp4", d=6)
    p = Project(clips=[clip_from_file(str(a))])
    p.clips[0].start, p.clips[0].end = 2.0, 5.0
    assert lossless_plan(p) == "single"
    r = export(Ctx(), p)
    assert r["how"].startswith("copied") and abs(ff.probe(r["path"]).duration - 3.0) < 0.15
    assert Path(r["path"]).name == "a_edited.mp4"
    # a turn is metadata only
    p.clips[0].rotate = 90
    r2 = export(Ctx(), p)
    assert Path(r2["path"]).name == "a_edited (2).mp4"            # never overwrites
    c = clip_from_file(r2["path"])
    assert (c.width, c.height) == (240, 320)
    # joining two matching videos: copied
    b = _video(env / "b.mp4", d=3)
    p = Project(clips=[clip_from_file(str(a)), clip_from_file(str(b))])
    assert lossless_plan(p) == "join"
    r3 = export(Ctx(), p)
    assert r3["how"].startswith("joined") and abs(ff.probe(r3["path"]).duration - 9.0) < 0.3


def test_mixed_join_and_effects(env):
    from nirvash.edit.project import Blur, Project, clip_from_file
    from nirvash.edit.render import export, lossless_plan
    from nirvash.media import ff
    a = _video(env / "small.mp4", 320, 240, 2.0, 25)
    b = _video(env / "big.mp4", 480, 270, 2.0, 30, audio=False)
    p = Project(clips=[clip_from_file(str(a)), clip_from_file(str(b))])
    assert lossless_plan(p) is None
    ctx = Ctx()
    r = export(ctx, p)
    pr = ff.probe(r["path"])
    assert (pr.width, pr.height) == (480, 270) and abs(pr.fps - 30) < 0.1 and abs(pr.duration - 4.0) < 0.2
    assert pr.audio is not None                                  # silence where the second clip has none
    assert any("frames a second" in m for _f, m in ctx.msgs) and max(f for f, _ in ctx.msgs) <= 1.0
    # blur flattens the box
    p = Project(clips=[clip_from_file(str(a))])
    p.blurs.append(Blur(kind="blur", strength=100, keys=[[0.0, 0.25, 0.25, 0.5, 0.5]]))
    r = export(Ctx(), p)
    before, after = _frame(a, 1.0), _frame(Path(r["path"]), 1.0)
    import cv2

    def detail(img):           # how much fine detail: edges everywhere
        return np.abs(cv2.Laplacian(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), cv2.CV_32F)).mean()

    roi = (slice(70, 170), slice(90, 230))
    assert detail(after[roi]) < detail(before[roi]) * 0.4
    assert detail(after[:50, :]) > detail(before[:50, :]) * 0.7          # outside the box: untouched
    p.blurs[0].kind = "pixelate"
    r = export(Ctx(), p)
    px = _frame(Path(r["path"]), 1.0)[roi]
    assert len(np.unique(px.reshape(-1, 3)[:, 1] // 8)) < len(np.unique(before[roi].reshape(-1, 3)[:, 1] // 8)) + 5


def test_crop_zoom_colour_overlay(env):
    import cv2
    from nirvash.edit.project import Overlay, Project, clip_from_file
    from nirvash.edit.render import export
    from nirvash.media import ff
    v = _halves(env / "halves.mp4")
    p = Project(clips=[clip_from_file(str(v))])
    p.crop.enabled = True
    p.crop.aspect = "4:3"
    p.crop.keys = [[0.0, 0.5, 0.0, 0.5, 1.0, "h"]]                 # the blue half
    r = export(Ctx(), p)
    pr = ff.probe(r["path"])
    assert (pr.width, pr.height) == (160, 120)
    f = _frame(Path(r["path"]), 1.0).astype(int)
    assert f[..., 0].mean() > 150 and f[..., 2].mean() < 60          # blue, not red
    # moves: on the red half, stays there, then glides to the blue half arriving at 2.5 s
    p.crop.keys = [[0.0, 0.0, 0.0, 0.5, 1.0, "h"], [2.5, 0.5, 0.0, 0.5, 1.0, "h"]]
    r = export(Ctx(), p)
    assert _frame(Path(r["path"]), 0.1).astype(int)[..., 2].mean() > 150
    assert _frame(Path(r["path"]), 1.5).astype(int)[..., 2].mean() > 150     # still held on the red half
    assert _frame(Path(r["path"]), 2.8).astype(int)[..., 0].mean() > 150
    # a box of another shape is fitted, not stretched: the rest is filled with the chosen colour
    p.crop.aspect = "source"                                           # 320 x 120 frame
    p.crop.keys = [[0.0, 0.5, 0.0, 0.5, 1.0, "h"]]                     # 160 x 120 box
    p.crop.fill, p.crop.fill_colour = "colour", "#00ff00"
    r = export(Ctx(), p)
    f = _frame(Path(r["path"]), 1.0).astype(int)
    assert f.shape[:2] == (120, 320)
    assert f[60, 160, 0] > 150 and f[60, 10, 1] > 150 and f[60, 310, 1] > 150      # blue middle, green sides
    # zoomed out past the edges of the video: a blurred copy fills the frame there
    p.crop.fill = "blur"
    p.crop.keys = [[0.0, -0.5, -0.5, 2.0, 2.0, "h"]]
    r = export(Ctx(), p)
    f = _frame(Path(r["path"]), 1.0).astype(int)
    assert f[5, 5].sum() > 40 and f[60, 120, 2] > 150 and f[60, 200, 0] > 150
    # colour: brighter
    p = Project(clips=[clip_from_file(str(v))])
    p.colour.brightness = 0.8
    r = export(Ctx(), p)
    assert _frame(Path(r["path"]), 1.0).mean() > _frame(v, 1.0).mean() + 15
    # overlay: an opaque green square, top left, half as wide as the video
    png = env / "logo.png"
    img = np.zeros((50, 50, 4), np.uint8)
    img[..., 1] = 255
    img[..., 3] = 255
    cv2.imwrite(str(png), img)
    p = Project(clips=[clip_from_file(str(v))])
    p.overlays.append(Overlay(path=str(png), x=0.0, y=0.0, w=0.5, start=1.0, end=2.0))
    r = export(Ctx(), p)
    out = Path(r["path"])
    assert _frame(out, 1.5).astype(int)[20, 20, 1] > 200               # green while it's on
    assert _frame(out, 0.5).astype(int)[20, 20, 1] < 100               # not before


def test_tracking_follows(env):
    from nirvash.edit.project import Project, clip_from_file, interp
    from nirvash.edit.track import model_path, track
    assert model_path().exists()
    # a white square moving right on black
    v = env / "move.mp4"
    sh("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=320x240:r=25:d=3", "-f", "lavfi", "-i",
       "color=c=white:s=40x40:r=25:d=3", "-filter_complex", "[0][1]overlay=x='20+t*80':y=100",
       "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(v))
    p = Project(clips=[clip_from_file(str(v))])
    keys = [[0.0, 20 / 320, 100 / 240, 40 / 320, 40 / 240]]
    res = track(Ctx(), p, keys, 0.0, 2.5)
    assert res["frames"] > 40 and res["lost"] is None
    x, y, w, h = interp(res["keys"], 2.0)
    assert abs(x * 320 - (20 + 2.0 * 80)) < 14 and abs(y * 240 - 100) < 10, (x * 320, y * 240)


def test_overlay_library(env):
    import cv2
    from nirvash.edit import overlays as ov
    src = env / "pics"
    src.mkdir()
    cv2.imwrite(str(src / "a.png"), np.zeros((20, 30, 4), np.uint8))
    (src / "notes.txt").write_text("x")
    sub = ov.new_folder(ov.root(), "Logos")
    got = ov.import_files([str(src / "a.png"), str(src / "a.png"), str(src / "notes.txt")], sub)
    assert [g.name for g in got] == ["a.png", "a (2).png"] and (src / "a.png").exists()
    assert ov.folders()[1] == sub and len(ov.files(ov.root(), recursive=True)) == 2
    assert ov.thumbnail(got[0]) is not None
    gif = env / "anim.gif"
    sh("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=64x48:r=10:d=1", str(gif))
    frames, fps = ov.load_frames(str(gif), 25)
    assert len(frames) >= 9 and frames[0].shape == (48, 64, 4)
    _ = os


def _db(path, start, dur):
    from nirvash.media import ff
    y = ff.decode_audio(path, 16000, start=start, duration=dur)
    return 20 * np.log10(np.sqrt(np.mean(y ** 2)) + 1e-9)


def test_sound_even_out_and_parts(env):
    from nirvash.edit.project import Project, clip_from_file
    from nirvash.edit.render import export, measure_sound, volume_filter
    from nirvash.jobs.audio_edit import measure
    v = env / "quiet.mp4"
    sh("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=160x120:r=25:d=6", "-f", "lavfi", "-i",
       "sine=f=440:d=6", "-af", "volume=-24dB", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
       "-shortest", str(v))
    p = Project(clips=[clip_from_file(str(v))])
    before = measure_sound(p)
    # evened out (towards -14 LUFS), copied picture, re-encoded sound only
    p.sound.normalize = True
    r = export(Ctx(), p)
    assert r["how"].startswith("copied")
    after = measure(r["path"])
    assert after.lufs > before.lufs + 4, (before, after)
    # louder for 2..4 s only
    p.sound.normalize = False
    p.sound.sections = [{"id": "x", "start": 2.0, "end": 4.0, "gain_db": 10.0}]
    assert "eval=frame" in volume_filter(p)
    r = export(Ctx(), p)
    inside, outside = _db(r["path"], 2.3, 1.4), _db(r["path"], 0.3, 1.4)
    assert 8 < inside - outside < 12, (inside, outside)
    # the same through a full re-encode (crop on)
    p.crop.enabled = True
    r = export(Ctx(), p)
    inside, outside = _db(r["path"], 2.3, 1.4), _db(r["path"], 4.3, 1.4)
    assert 8 < inside - outside < 12, (inside, outside)
