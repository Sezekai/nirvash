"""Follow something as it moves, so a crop or a blur box can stay on it.

You draw the box around it at the playhead; tracking then watches the following frames with OpenCV's VitTrack
(a small neural tracker, bundled, runs on the CPU) and writes a key for every frame. The box keeps the size you
drew; only its position follows. Positions are smoothed so the box doesn't shake. Tracking stops at the end of
the item's time, or when the tracker loses the subject for half a second (it says so)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

from ..core import log as _log
from ..core.paths import asset
from ..media import ff
from .project import Project, interp
from .render import decode_cmd

log = _log.get("track")

ANALYSIS_W = 640      # frames are looked at this wide (faster, and plenty for following a subject)


def model_path() -> Path:
    return asset("models/vittrack.onnx")


def _tracker():
    import cv2
    m = model_path()
    if m.exists() and hasattr(cv2, "TrackerVit_create"):
        p = cv2.TrackerVit_Params()
        p.net = str(m)
        return cv2.TrackerVit_create(p), "VitTrack"
    log.warning("VitTrack model missing; using the simpler MIL tracker")
    return cv2.TrackerMIL_create(), "MIL"


def _smooth(vals: np.ndarray, win: int) -> np.ndarray:
    if len(vals) < 3 or win < 2:
        return vals
    win = min(win, len(vals) | 1)
    k = np.ones(win) / win
    pad = win // 2
    padded = np.pad(vals, (pad, pad), mode="edge")
    return np.convolve(padded, k, mode="valid")[: len(vals)]


def track(ctx, p: Project, keys: list, t0: float, t1: float, smooth: float = 0.25) -> dict:
    """Track the box at t0 (from `keys`) until t1. Returns {"keys", "until", "lost", "tracker"}; keys are new key
    lists to put in place of the old ones between t0 and `until` (keys outside that are kept)."""
    import cv2
    W, H, fps = p.canvas()
    aw = ANALYSIS_W if W > ANALYSIS_W else W
    ah = max(2, int(round(H * aw / W / 2)) * 2)
    box = interp(keys, t0)
    if box is None:
        raise ValueError("draw the box first")
    bx, by, bw, bh = box
    tracker, name = _tracker()
    n = aw * ah * 3
    out_t, out_xy = [], []
    lost_run, lost_at = 0, None
    total = max(1, int((t1 - t0) * fps))
    t = t0
    started = False
    i, src_t = p.clip_at(t0)
    done = 0
    while i < len(p.clips) and t < t1 - 1e-6:
        clip = p.clips[i]
        off = p.clip_offset(i)
        dur = min(clip.out - src_t, t1 - t)
        proc = ff.popen(decode_cmd(p, i, src_t, aw, ah, fps, duration=dur), stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            k = 0
            while True:
                ctx.check()
                buf = proc.stdout.read(n)
                if len(buf) < n:
                    break
                frame = np.frombuffer(buf, np.uint8).reshape(ah, aw, 3)
                ft = off + (src_t - clip.start) + k / fps
                k += 1
                if ft > t1:
                    break
                if not started:
                    tracker.init(frame, (int(bx * aw), int(by * ah), max(4, int(bw * aw)), max(4, int(bh * ah))))
                    started = True
                    cx, cy = bx + bw / 2, by + bh / 2
                else:
                    ok, r = tracker.update(frame)
                    score = tracker.getTrackingScore() if hasattr(tracker, "getTrackingScore") else (1.0 if ok else 0.0)
                    if not ok or score < 0.3:
                        lost_run += 1
                        if lost_run >= max(3, int(fps * 0.5)):
                            lost_at = ft
                            break
                        continue
                    lost_run = 0
                    cx = (r[0] + r[2] / 2) / aw
                    cy = (r[1] + r[3] / 2) / ah
                out_t.append(ft)
                out_xy.append((cx, cy))
                done += 1
                if done % 5 == 0:
                    ctx.progress(min(0.99, done / total), f"Following it · frame {done} of {total}")
        finally:
            proc.kill()
            proc.wait()
        if lost_at is not None:
            break
        i += 1
        if i < len(p.clips):
            src_t = p.clips[i].start
            t = p.clip_offset(i)
    if not out_t:
        raise ValueError("couldn't read the video there")
    xy = np.array(out_xy, np.float64)
    win = max(1, int(fps * smooth))
    xs, ys = _smooth(xy[:, 0], win), _smooth(xy[:, 1], win)
    xs[0], ys[0] = xy[0]                                     # starts exactly where you drew it
    new = [[float(tt), float(min(1 - bw, max(0.0, x - bw / 2))), float(min(1 - bh, max(0.0, y - bh / 2))), bw, bh]
           for tt, x, y in zip(out_t, xs, ys)]
    step = max(1, int(round(fps / 15)))                      # about 15 keys a second is smooth enough
    new = new[::step] + ([new[-1]] if (len(new) - 1) % step else [])
    until = out_t[-1]
    merged = [k for k in keys if k[0] < t0 - 1e-6] + new + [k for k in keys if k[0] > until + 1e-6]
    log.info("tracked %d frames with %s (%.2f-%.2f s)%s", len(out_t), name, t0, until,
             f", lost at {lost_at:.2f} s" if lost_at else "")
    ctx.progress(1.0, "")
    return {"keys": merged, "until": until, "lost": lost_at, "tracker": name, "frames": len(out_t)}
