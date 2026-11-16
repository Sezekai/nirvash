"""Turning a Project into pictures: one still for the editor, live preview frames, and the exported file.

The same code makes all three, so the export matches what the editor showed:

1. ffmpeg decodes each clip and does the per-clip work in C, fast: rotation and flips, straightening, fitting it
   into the canvas (the joined video's size, bars where the shape differs), the frame rate, and colour.
2. Python (numpy + OpenCV) does the parts that move over time: blur / pixelate boxes, the crop and zoom (sub-pixel,
   so slow pans are smooth), and overlays.
3. ffmpeg encodes the result (x264, or NVENC when "Export faster (GPU)" is on) and adds the clips' sound.

When nothing changes the picture (only trims, joins of matching videos, rotation or flips) the video is copied
instead, without re-encoding; cuts then land on the nearest keyframe before the chosen start.
"""
from __future__ import annotations

import math
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

from ..core import log as _log
from ..media import ff
from .project import Blur, Clip, Overlay, Project

log = _log.get("editor")

try:
    import cv2  # noqa: F401
    HAVE_CV2 = True
except Exception:  # pragma: no cover - only if the package is missing
    HAVE_CV2 = False


class ExportError(RuntimeError):
    pass


# ---- 1. per-clip work in ffmpeg ------------------------------------------------------------------------------------
def clip_filter(p: Project, clip: Clip, W: int, H: int, fps: float | None, *, colour: bool = True) -> str:
    """ffmpeg -vf for one clip: its turns, straightening, fit into W x H, frame rate, colour; ends as BGR."""
    f: list[str] = []
    if clip.rotate == 90:
        f.append("transpose=1")
    elif clip.rotate == 180:
        f.append("transpose=1,transpose=1")
    elif clip.rotate == 270:
        f.append("transpose=2")
    if clip.flip_h:
        f.append("hflip")
    if clip.flip_v:
        f.append("vflip")
    if abs(clip.angle) > 0.01:
        a = math.radians(clip.angle)
        # zoom in just enough that the turned picture still fills the frame (no corners)
        cw, ch = clip.shown_size()
        r = max(cw / max(1, ch), ch / max(1, cw))
        k = abs(math.cos(a)) + abs(math.sin(a)) * r
        f.append(f"rotate={a:.6f}:fillcolor=black,scale=trunc(iw*{k:.5f}/2)*2:trunc(ih*{k:.5f}/2)*2,"
                 f"crop=trunc(iw/{k:.5f}/2)*2:trunc(ih/{k:.5f}/2)*2")
    f.append(f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=bicubic,"
             f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1")
    if fps:
        f.append(f"fps={fps:.3f}")
    if colour:
        f += colour_filters(p)
    f.append("format=bgr24")
    return ",".join(f)


def colour_filters(p: Project) -> list[str]:
    c = p.colour
    out = []
    if (abs(c.brightness) > 1e-3 or abs(c.contrast - 1) > 1e-3 or abs(c.saturation - 1) > 1e-3
            or abs(c.gamma - 1) > 1e-3):
        out.append(f"eq=brightness={c.brightness * 0.3:.4f}:contrast={c.contrast:.4f}:"
                   f"saturation={c.saturation:.4f}:gamma={c.gamma:.4f}")
    if abs(c.warmth) > 1e-3 or abs(c.tint) > 1e-3:
        w, g = c.warmth * 0.25, c.tint * 0.2
        out.append(f"colorbalance=rs={w:.4f}:gs={-g:.4f}:bs={-w:.4f}:rm={w:.4f}:gm={-g:.4f}:bm={-w:.4f}")
    if c.sharpen > 1e-3:
        out.append(f"unsharp=5:5:{c.sharpen * 1.5:.3f}:5:5:0")
    return out


def decode_cmd(p: Project, i: int, src_t: float, W: int, H: int, fps: float | None, frames: int | None = None,
               duration: float | None = None) -> list[str]:
    clip = p.clips[i]
    args = [ff.ffmpeg(), "-v", "error", "-nostdin"]
    if src_t > 0:
        args += ["-ss", f"{src_t:.3f}"]
    args += ["-i", clip.path]
    if duration is not None:
        args += ["-t", f"{max(0.0, duration):.3f}"]
    if frames is not None:
        args += ["-frames:v", str(frames)]
    args += ["-map", "0:v:0", "-an", "-sn", "-vf", clip_filter(p, clip, W, H, fps), "-f", "rawvideo", "pipe:1"]
    return args


class FrameSource:
    """The edit's pictures one after another from t0, at W x H and `fps`: each clip decoded by ffmpeg as it comes
    up, crossfades blended (both clips decoded at once), fades through black applied."""

    def __init__(self, p: Project, t0: float, W: int, H: int, fps: float, end: float | None = None):
        self.p, self.t0, self.W, self.H, self.fps = p, max(0.0, t0), W, H, fps
        self.end = p.duration if end is None else min(end, p.duration)
        self.offs = p.offsets()
        self.dec: dict[int, dict] = {}
        self.n = W * H * 3

    def _frame(self, i: int, t: float) -> np.ndarray:
        d = self.dec.get(i)
        if d is None:
            c = self.p.clips[i]
            src = c.start + max(0.0, t - self.offs[i])
            proc = ff.popen(decode_cmd(self.p, i, src, self.W, self.H, self.fps, duration=max(0.05, c.out - src)),
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            d = self.dec[i] = {"proc": proc, "last": None}
        buf = d["proc"].stdout.read(self.n)
        if len(buf) < self.n:                       # a clip a frame short: hold its last picture
            return d["last"] if d["last"] is not None else np.zeros((self.H, self.W, 3), np.uint8)
        f = np.frombuffer(buf, np.uint8).reshape(self.H, self.W, 3).copy()
        d["last"] = f
        return f

    def _close(self, i: int) -> None:
        d = self.dec.pop(i, None)
        if d is not None:
            d["proc"].kill()
            d["proc"].wait()

    def close(self) -> None:
        for i in list(self.dec):
            self._close(i)

    def __iter__(self):
        import cv2
        p, offs = self.p, self.offs
        k = 0
        try:
            while True:
                t = self.t0 + k / self.fps
                if t >= self.end - 1e-6:
                    break
                active = [i for i, c in enumerate(p.clips) if offs[i] - 1e-9 <= t < offs[i] + c.length]
                if not active:
                    active = [len(p.clips) - 1]
                for i in list(self.dec):
                    if i < active[0]:
                        self._close(i)
                frames = []
                for i in active:
                    f = self._frame(i, t)
                    fa = p.fade(i, t)
                    if fa < 0.999:
                        f = cv2.convertScaleAbs(f, alpha=fa)
                    frames.append((i, f))
                if len(frames) == 1:
                    img = frames[0][1]
                else:                                   # a crossfade: the later clip comes in
                    (ia, fa_), (ib, fb) = frames[-2], frames[-1]
                    ov = max(1e-3, p.overlap(ib))
                    w = max(0.0, min(1.0, (t - offs[ib]) / ov))
                    img = cv2.addWeighted(fa_, 1 - w, fb, w, 0)
                yield t, img
                k += 1
        finally:
            self.close()


def grab(p: Project, t: float, W: int, H: int) -> np.ndarray | None:
    """The canvas picture (after per-clip work) at output time t, at W x H. None past the end or on an error."""
    if not p.clips:
        return None
    _w, _h, fps = p.canvas()
    t = min(t, max(0.0, p.duration - 1.0 / fps))
    for _t, img in FrameSource(p, t, W, H, fps):
        return img
    return None


# ---- 2. the moving parts, in Python ---------------------------------------------------------------------------------
def _box_px(box, W: int, H: int) -> tuple[int, int, int, int]:
    x, y, w, h = box
    x0, y0 = int(round(x * W)), int(round(y * H))
    x1, y1 = int(round((x + w) * W)), int(round((y + h) * H))
    x0, y0 = max(0, min(W - 1, x0)), max(0, min(H - 1, y0))
    x1, y1 = max(x0 + 1, min(W, x1)), max(y0 + 1, min(H, y1))
    return x0, y0, x1, y1


def apply_blur(img: np.ndarray, b: Blur, box) -> None:
    import cv2
    H, W = img.shape[:2]
    x0, y0, x1, y1 = _box_px(box, W, H)
    roi = img[y0:y1, x0:x1]
    if roi.size == 0:
        return
    h, w = roi.shape[:2]
    s = max(1, min(100, b.strength)) / 100
    if b.kind == "pixelate":
        block = max(2, int(min(w, h) * (0.03 + s * 0.2)))       # size of each square, in pixels
        small = cv2.resize(roi, (max(1, w // block), max(1, h // block)), interpolation=cv2.INTER_AREA)
        out = cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST)
    else:
        k = max(3, int(min(w, h) * (0.08 + s * 0.5)) | 1)
        out = cv2.GaussianBlur(roi, (k, k), 0)
        out = cv2.GaussianBlur(out, (k, k), 0)
    if b.shape == "ellipse":
        mask = np.zeros((h, w), np.uint8)
        cv2.ellipse(mask, (w // 2, h // 2), (max(1, w // 2), max(1, h // 2)), 0, 0, 360, 255, -1)
        mask = cv2.GaussianBlur(mask, (0, 0), max(1.0, min(w, h) * 0.03))[..., None] / 255.0
        roi[:] = (out * mask + roi * (1 - mask)).astype(np.uint8)
    else:
        roi[:] = out


def _fill_bg(img: np.ndarray, box_px, OW: int, OH: int, fill: str, colour: str) -> np.ndarray:
    """What shows where the crop box doesn't cover the frame: a colour, or a big soft blur of the box's picture."""
    import cv2
    if fill != "blur":
        c = colour.lstrip("#")
        try:
            r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
        except (ValueError, IndexError):
            r = g = b = 0
        out = np.empty((OH, OW, 3), np.uint8)
        out[:] = (b, g, r)
        return out
    H, W = img.shape[:2]
    x0, y0, x1, y1 = box_px
    x0, y0 = max(0, int(x0)), max(0, int(y0))
    x1, y1 = min(W, int(np.ceil(x1))), min(H, int(np.ceil(y1)))
    src = img[y0:y1, x0:x1] if x1 - x0 > 4 and y1 - y0 > 4 else img
    sh, sw = src.shape[:2]
    k = max(OW / sw, OH / sh)                                      # cover the frame, keep the shape
    small_w, small_h = max(8, OW // 12), max(8, OH // 12)
    cov = cv2.resize(src, (max(1, int(sw * k / 12)), max(1, int(sh * k / 12))), interpolation=cv2.INTER_AREA)
    ch, cw = cov.shape[:2]
    oy, ox = max(0, (ch - small_h) // 2), max(0, (cw - small_w) // 2)
    cov = cov[oy:oy + small_h, ox:ox + small_w]
    cov = cv2.GaussianBlur(cov, (0, 0), 2.5)
    out = cv2.resize(cov, (OW, OH), interpolation=cv2.INTER_LINEAR)
    return (out * 0.8).astype(np.uint8)                            # a little darker, so the picture stands out


def apply_crop(img: np.ndarray, box, OW: int, OH: int, fill: str = "blur", colour: str = "#000000") -> np.ndarray:
    """Cut the box out and fit it into OW x OH without stretching, at sub-pixel positions so moves are smooth.
    Where it doesn't cover the frame (a different shape, or zoomed out past the edge of the video) it's filled."""
    import cv2
    H, W = img.shape[:2]
    x, y, w, h = box
    bx, by, bw, bh = x * W, y * H, max(1e-3, w * W), max(1e-3, h * H)
    s = min(OW / bw, OH / bh)
    ox, oy = (OW - bw * s) / 2, (OH - bh * s) / 2
    # where the video itself lands in the frame
    vx0, vy0 = (0 - bx) * s + ox, (0 - by) * s + oy
    vx1, vy1 = (W - bx) * s + ox, (H - by) * s + oy
    covered = vx0 <= 0.5 and vy0 <= 0.5 and vx1 >= OW - 0.5 and vy1 >= OH - 0.5 and ox <= 0.5 and oy <= 0.5
    src, k = img, 1.0
    if s < 0.7:                                  # shrinking a lot: scale down first (area average, no shimmer)
        k = s
        src = cv2.resize(img, (max(1, int(W * k)), max(1, int(H * k))), interpolation=cv2.INTER_AREA)
    m = np.array([[s / k, 0, ox - bx * s], [0, s / k, oy - by * s]], np.float32)
    if covered:
        return cv2.warpAffine(src, m, (OW, OH), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    out = _fill_bg(img, (bx, by, bx + bw, by + bh), OW, OH, fill, colour)
    pic = cv2.warpAffine(src, m, (OW, OH), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    # the picture's own area: the box (fitted) and the video, whichever is smaller
    x0, y0 = int(round(max(ox, vx0, 0))), int(round(max(oy, vy0, 0)))
    x1, y1 = int(round(min(OW - ox, vx1, OW))), int(round(min(OH - oy, vy1, OH)))
    if x1 > x0 and y1 > y0:
        out[y0:y1, x0:x1] = pic[y0:y1, x0:x1]
    return out


class OverlaySource:
    """An overlay's frames (one for a picture, many for a GIF or video), with transparency."""

    def __init__(self, path: str, fps: float, max_s: float = 30.0):
        from .overlays import load_frames
        self.path = path
        self.frames, self.fps = load_frames(path, fps, max_s)
        self._sized: dict = {}

    def frame(self, t: float, w: int) -> np.ndarray | None:
        import cv2
        if not self.frames:
            return None
        i = int(t * self.fps) % len(self.frames) if len(self.frames) > 1 else 0
        key = (i, w)
        f = self._sized.get(key)
        if f is None:
            src = self.frames[i]
            h = max(1, int(round(src.shape[0] * w / max(1, src.shape[1]))))
            f = cv2.resize(src, (max(1, w), h), interpolation=cv2.INTER_AREA if w < src.shape[1] else cv2.INTER_LINEAR)
            if len(self._sized) > 400:
                self._sized.clear()
            self._sized[key] = f
        return f

    def aspect(self) -> float:
        if not self.frames:
            return 1.0
        h, w = self.frames[0].shape[:2]
        return h / max(1, w)


_SOURCES: dict[tuple, OverlaySource | None] = {}


def _source(path: str, fps: float) -> OverlaySource | None:
    """Overlays are read once (a GIF or video is decoded) and kept while the editor uses them."""
    try:
        key = (path, Path(path).stat().st_mtime_ns, round(fps, 3))
    except OSError:
        return None
    if key not in _SOURCES:
        if len(_SOURCES) > 40:
            _SOURCES.clear()
        try:
            _SOURCES[key] = OverlaySource(path, fps)
        except Exception as e:  # noqa: BLE001 - a broken overlay shouldn't stop the edit
            log.warning("overlay %s couldn't be read: %s", Path(path).name, e)
            _SOURCES[key] = None
    return _SOURCES[key]


def apply_overlay(img: np.ndarray, o: Overlay, src: OverlaySource, t_local: float) -> None:
    H, W = img.shape[:2]
    w = max(1, int(round(o.w * W)))
    f = src.frame(t_local, w)
    if f is None:
        return
    x, y = int(round(o.x * W)), int(round(o.y * H))
    fh, fw = f.shape[:2]
    x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + fw), min(H, y + fh)
    if x1 <= x0 or y1 <= y0:
        return
    part = f[y0 - y:y1 - y, x0 - x:x1 - x]
    a = part[..., 3:4].astype(np.float32) / 255.0 * max(0.0, min(1.0, o.opacity))
    dst = img[y0:y1, x0:x1]
    dst[:] = (part[..., :3] * a + dst * (1 - a)).astype(np.uint8)


class FrameOps:
    """Applies the moving parts to canvas frames at any scale (the preview uses a smaller one)."""

    def __init__(self, p: Project, out_size: tuple[int, int] | None = None):
        self.p = p
        self.out_size = out_size or p.output_size()
        _w, _h, self.fps = p.canvas()
        self.sources: dict[str, OverlaySource] = {}
        for o in p.overlays:
            if o.path and o.path not in self.sources and Path(o.path).exists():
                src = _source(o.path, self.fps)
                if src is not None:
                    self.sources[o.path] = src

    def blurs(self, img: np.ndarray, t: float, space: str = "video") -> None:
        """Blur boxes of one kind: "video" ones on the picture (before the crop), "output" ones on the frame."""
        for b in self.p.blurs:
            if b.space == space and b.start <= t <= self.p.blur_end(b):
                box = self.p.blur_box(b, t, space)
                if box is not None:
                    apply_blur(img, b, box)

    def crop(self, img: np.ndarray, t: float) -> np.ndarray:
        box = self.p.crop_box(t)
        if box is None:
            if self.out_size != (img.shape[1], img.shape[0]):
                import cv2
                return cv2.resize(img, self.out_size, interpolation=cv2.INTER_AREA)
            return img
        OW, OH = self.out_size
        return apply_crop(img, box, OW, OH, self.p.crop.fill, self.p.crop.fill_colour)

    def overlays(self, img: np.ndarray, t: float) -> None:
        for o in self.p.overlays:
            if o.start <= t <= self.p.overlay_end(o) and o.path in self.sources:
                apply_overlay(img, o, self.sources[o.path], t - o.start)

    def render(self, img: np.ndarray, t: float, overlays: bool = True) -> np.ndarray:
        """Picture blurs, crop / zoom (fitted, filled), frame blurs, overlays: the finished frame."""
        self.blurs(img, t, "video")
        img = self.crop(img, t)
        self.blurs(img, t, "output")
        if overlays:
            self.overlays(img, t)
        return img


def preview_out_size(p: Project, canvas_w: int, canvas_h: int) -> tuple[int, int]:
    """The output size scaled the same way the preview canvas is scaled from the real canvas."""
    W, H, _ = p.canvas()
    OW, OH = p.output_size()
    k = canvas_w / max(1, W)
    return max(2, int(round(OW * k / 2)) * 2), max(2, int(round(OH * k / 2)) * 2)


# ---- 3. export ------------------------------------------------------------------------------------------------------
QUALITY = {"best": (12, 12), "high": (16, 17), "smaller": (21, 22)}   # x264 CRF, NVENC CQ


def video_encoder(p: Project) -> list[str]:
    from ..core.settings import get_settings
    crf, cq = QUALITY.get(p.output.quality, QUALITY["high"])
    if p.output.gpu or get_settings().get("export.gpu"):
        return ["-c:v", "h264_nvenc", "-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", str(cq), "-b:v", "0",
                "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "slow", "-crf", str(crf), "-pix_fmt", "yuv420p"]


def lossless_plan(p: Project) -> str | None:
    """Why the video can be copied without re-encoding, or None when it can't."""
    if not p.output.lossless_when_possible or p.frame_effects() or not p.clips:
        return None
    if len(p.clips) > 1:
        try:
            infos = [ff.probe(c.path) for c in p.clips]
        except ff.FFError:
            return None
        sig = set()
        for pr in infos:
            v, a = pr.video, pr.audio
            sig.add((v.codec if v else None, pr.width, pr.height, v.raw.get("pix_fmt") if v else None,
                     round(pr.fps, 2), a.codec if a else None, a.raw.get("sample_rate") if a else None,
                     a.raw.get("channels") if a else None))
        if len(sig) != 1 or any(c.rotate or c.flip_h or c.flip_v or c.mute for c in p.clips):
            return None
        if not p.sound.is_neutral():
            return None
        return "join"
    return "single"


def keyframe_before(path: str, t: float) -> float:
    """The last keyframe at or before t (a copied video can only start on one)."""
    if t <= 0.01:
        return 0.0
    r = ff.run([ff.ffprobe(), "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey", "-show_entries",
                "frame=pts_time", "-of", "csv=p=0", "-read_intervals", f"{max(0.0, t - 30):.3f}%{t + 0.05:.3f}", path],
               timeout=120)
    best = 0.0
    for line in r.stdout.decode("ascii", "ignore").split():
        try:
            v = float(line.strip().strip(","))
        except ValueError:
            continue
        if v <= t + 0.001:
            best = max(best, v)
    return best


def _audio_args(p: Project, first_input: int, vol: str | None = None) -> tuple[list[str], list[str], list[str]]:
    """Inputs, filter graph and maps for the edit's sound: the clips' own sound cut with the picture (crossfaded or
    faded where the picture is), plus the sound layers (songs, a video's sound kept whole), mixed, as long as the
    edit, with the volume changes (`vol`, working on the edit's time) at the end."""
    clips, snd = p.clips, p.sound
    inputs, graph, mix = [], [], []
    k = first_input
    dur = p.duration
    if snd.clip_sound and any(c.has_audio and not c.mute for c in clips):
        for j, c in enumerate(clips):
            fx = []
            if j > 0 and c.transition == "black":
                fx.append(f"afade=t=in:d={max(0.01, min(c.transition_s / 2, c.length / 2)):.3f}")
            if j + 1 < len(clips) and clips[j + 1].transition == "black":
                h = max(0.01, min(clips[j + 1].transition_s / 2, c.length / 2))
                fx.append(f"afade=t=out:st={max(0.0, c.length - h):.3f}:d={h:.3f}")
            tail = ("," + ",".join(fx)) if fx else ""
            if c.has_audio and not c.mute:
                inputs += ["-ss", f"{c.start:.3f}", "-t", f"{c.length:.3f}", "-i", c.path]
                graph.append(f"[{k}:a:0]aresample=48000,aformat=channel_layouts=stereo,apad,atrim=0:{c.length:.3f},"
                             f"asetpts=N/SR/TB{tail}[a{j}]")
                k += 1
            else:
                graph.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{c.length:.3f},asetpts=N/SR/TB[a{j}]")
        cur = "a0"
        for j in range(1, len(clips)):
            ov = p.overlap(j)
            if ov > 0:
                graph.append(f"[{cur}][a{j}]acrossfade=d={ov:.3f}:c1=tri:c2=tri[x{j}]")
            else:
                graph.append(f"[{cur}][a{j}]concat=n=2:v=0:a=1[x{j}]")
            cur = f"x{j}"
        mix.append(cur)
    for n, l in enumerate(p.layer_objs()):
        if not l.path or l.length <= 0.05 or l.at >= dur:
            continue
        play = (dur - l.at) if l.loop else min(l.length, dur - l.at)
        if l.loop:
            inputs += ["-i", l.path]
            src = (f"[{k}:a:0]aresample=48000,aformat=channel_layouts=stereo,"
                   f"atrim={l.src_start:.3f}:{l.src_start + l.length:.3f},asetpts=N/SR/TB,"
                   f"aloop=loop=-1:size={int(l.length * 48000)},atrim=0:{play:.3f},asetpts=N/SR/TB")
        else:
            inputs += ["-ss", f"{l.src_start:.3f}", "-t", f"{play:.3f}", "-i", l.path]
            src = f"[{k}:a:0]aresample=48000,aformat=channel_layouts=stereo,asetpts=N/SR/TB"
        k += 1
        fx = [f"volume={l.gain_db:.2f}dB"]
        if l.fade_in > 0.01:
            fx.append(f"afade=t=in:d={min(l.fade_in, play / 2):.3f}")
        if l.fade_out > 0.01:
            fo = min(l.fade_out, play / 2)
            fx.append(f"afade=t=out:st={max(0.0, play - fo):.3f}:d={fo:.3f}")
        ms = int(round(l.at * 1000))
        if ms > 0:
            fx.append(f"adelay={ms}|{ms}")
        graph.append(src + "," + ",".join(fx) + f"[L{n}]")
        mix.append(f"L{n}")
    if not mix:
        return [], [], []
    if len(mix) == 1:
        graph.append(f"[{mix[0]}]apad,atrim=0:{dur:.3f},{vol or 'anull'}[aout]")
    else:
        graph.append("".join(f"[{m}]" for m in mix) + f"amix=inputs={len(mix)}:normalize=0:duration=longest,"
                     f"apad,atrim=0:{dur:.3f},{vol or 'anull'}[aout]")
    return inputs, ["-filter_complex", ";".join(graph)], ["-map", "[aout]", "-c:a", "aac", "-b:a", "320k"]


# ---- sound: evening out, louder / quieter --------------------------------------------------------------------------
def measure_sound(p: Project, ctx=None):
    """Loudness of the edit's sound as it is now (before any volume change). None when it's silent."""
    from ..jobs.audio_edit import Loudness
    import json as _json
    import re as _re
    inputs, graph, _maps = _audio_args(p, 0)
    if not inputs:
        return None
    g = graph[1].replace("[aout]", ",loudnorm=print_format=json[aout]")
    args = [ff.ffmpeg(), "-hide_banner", "-nostdin", "-v", "info"] + inputs + ["-filter_complex", g, "-map", "[aout]",
                                                                               "-f", "null", "-"]
    if ctx is not None:
        ctx.progress(0.01, "Measuring the sound")
    r = ff.run(args, timeout=3600)
    m = _re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr.decode("utf-8", "replace"), _re.S)
    if not m:
        return None
    d = _json.loads(m.group(0))
    lufs, peak = float(d["input_i"]), float(d["input_tp"])
    return None if lufs < -69 else Loudness(lufs, peak)


def norm_gain(p: Project) -> float:
    """The change that evens out the edit's sound (0 if off or not measured yet)."""
    from ..jobs.audio_edit import Loudness, level_gain
    snd = p.sound
    if not snd.normalize or len(snd.measured) < 3 or snd.measured[0] != p.sound_signature():
        return 0.0
    if snd.measured[1] is None:
        return 0.0
    return level_gain(Loudness(snd.measured[1], snd.measured[2]))


def ensure_measured(p: Project, ctx=None) -> None:
    snd = p.sound
    if snd.normalize and (len(snd.measured) < 3 or snd.measured[0] != p.sound_signature()):
        l = measure_sound(p, ctx)
        snd.measured = [p.sound_signature(), l.lufs if l else None, l.peak if l else None]


def volume_filter(p: Project, offset: float = 0.0) -> str | None:
    """ffmpeg filter for the volume changes. Times are the edit's; `offset` is added to the filter's own t (the
    preview plays clip files, whose time starts elsewhere). Parts fade in and out over a twentieth of a second."""
    snd = p.sound
    base = snd.gain_db + norm_gain(p)
    secs = [s for s in snd.sections if s.get("end", 0) > s.get("start", 0) and abs(s.get("gain_db", 0)) >= 0.05]
    if not secs:
        return f"volume={base:.2f}dB" if abs(base) >= 0.05 else None
    tt = f"(t+{offset:.4f})" if offset >= 0 else f"(t-{-offset:.4f})"
    r = 0.05
    terms = [f"{s['gain_db']:.2f}*min(1\\,max(0\\,({tt}-{s['start']:.3f})/{r}))*min(1\\,max(0\\,({s['end']:.3f}-{tt})/{r}))"
             for s in secs]
    expr = f"pow(10\\,({base:.2f}+" + "+".join(terms) + ")/20)"
    return f"volume=volume={expr}:eval=frame"


def export(ctx, p: Project, out: str | None = None) -> dict:
    """Make the edited video. Returns {"path", "how", "seconds"}. Never overwrites a file."""
    from ..jobs.library_files import unique_path
    if not p.clips:
        raise ExportError("Add a video first.")
    for c in p.clips:
        if not Path(c.path).exists():
            raise ExportError(f"{Path(c.path).name} isn't there any more.")
    dest = unique_path(Path(out) if out else p.default_output_path())
    tmp = dest.with_name(dest.stem + ".part" + dest.suffix)
    t0 = time.time()
    ensure_measured(p, ctx)
    plan = lossless_plan(p)
    try:
        if plan:
            how = _export_copy(ctx, p, tmp, plan)
        else:
            how = _export_render(ctx, p, tmp)
        if not tmp.exists() or tmp.stat().st_size < 1000:
            raise ExportError("ffmpeg made an empty file.")
        tmp.replace(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    log.info("exported %s (%s) in %.0f s", dest.name, how, time.time() - t0)
    return {"path": str(dest), "how": how, "seconds": time.time() - t0}


def _export_copy(ctx, p: Project, tmp: Path, plan: str) -> str:
    """Trim / join / turn without re-encoding."""
    ctx.progress(0.02, "Copying the video (no re-encoding)")
    if plan == "single":
        c = p.clips[0]
        start = keyframe_before(c.path, c.start)
        args = [ff.ffmpeg(), "-v", "error", "-y"]
        if c.rotate or c.flip_h or c.flip_v:
            # a turn as metadata: players show it turned, nothing re-encoded
            args += ["-display_rotation:v:0", str(-c.rotate % 360)]
            if c.flip_h:
                args += ["-display_hflip:v:0"]
            if c.flip_v:
                args += ["-display_vflip:v:0"]
        if start > 0:
            args += ["-ss", f"{start:.3f}"]
        args += ["-i", c.path]
        if p.sound.is_neutral() or (not c.has_audio and not p.sound.layers):
            args += ["-t", f"{c.out - start:.3f}", "-map", "0:v:0"]
            if c.has_audio and not c.mute:
                args += ["-map", "0:a:0"]
            args += ["-c", "copy"]
        else:
            # the picture is copied; the sound (with its changes and layers) is made fresh to match it
            q = p.copy()
            q.clips[0].start = start                 # the copy starts at that keyframe, so the sound does too
            d = c.start - start
            for l in q.sound.layers:
                l["at"] = l["at"] + d
            if len(p.sound.measured) >= 3 and p.sound.measured[0] == p.sound_signature():
                q.sound.measured = [q.sound_signature()] + list(p.sound.measured[1:])
            a_in, a_graph, a_maps = _audio_args(q, 1, volume_filter(q, offset=-(c.start - start)))
            args = args[:args.index("-i")] + ["-i", c.path] + a_in + a_graph
            args += ["-t", f"{c.out - start:.3f}", "-map", "0:v:0"] + a_maps + ["-c:v", "copy"]
        args += ["-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(tmp)]
        ff.run_with_progress(args, c.out - start, lambda f: ctx.progress(0.02 + 0.95 * f, "Copying the video"),
                             ctx.cancel)
        note = f" (starts at the keyframe {start - c.start:+.2f} s from your start)" if c.start - start > 0.05 else ""
        return "copied, not re-encoded" + note
    # join of matching videos: concat demuxer
    lst = tmp.with_suffix(".txt")
    lines = []
    for c in p.clips:
        st = keyframe_before(c.path, c.start)
        lines.append(f"file '{Path(c.path).as_posix()}'")
        if st > 0:
            lines.append(f"inpoint {st:.3f}")
        if c.end > 0 and c.end < c.duration - 0.01:
            lines.append(f"outpoint {c.end:.3f}")
    lst.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        vol = volume_filter(p)
        codec = (["-af", vol, "-c:v", "copy", "-c:a", "aac", "-b:a", "320k"] if vol
                 else ["-c", "copy"])
        args = [ff.ffmpeg(), "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst)] + codec + [
                "-movflags", "+faststart", str(tmp)]
        ff.run_with_progress(args, p.duration, lambda f: ctx.progress(0.02 + 0.95 * f, "Joining the videos"),
                             ctx.cancel)
    finally:
        lst.unlink(missing_ok=True)
    return "joined without re-encoding"


def _export_render(ctx, p: Project, tmp: Path) -> str:
    W, H, fps = p.canvas()
    ops = FrameOps(p)
    OW, OH = ops.out_size
    total_frames = max(1, int(round(p.duration * fps)))
    a_inputs, a_graph, a_maps = _audio_args(p, 1, volume_filter(p))
    enc = [ff.ffmpeg(), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{OW}x{OH}",
           "-r", f"{fps:.3f}", "-i", "pipe:0"] + a_inputs + a_graph + ["-map", "0:v:0"] + video_encoder(p) + a_maps
    enc += ["-movflags", "+faststart", str(tmp)]
    log.info("encode: %s", " ".join(enc[1:]))
    eproc = ff.popen(enc, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    err: list[bytes] = []
    threading.Thread(target=lambda: err.append(eproc.stderr.read()), daemon=True).start()
    done = 0
    t_start = time.time()
    try:
        for t, frame in FrameSource(p, 0.0, W, H, fps):
            ctx.check()
            out = ops.render(frame, t)
            if out.shape[1] != OW or out.shape[0] != OH:
                import cv2
                out = cv2.resize(out, (OW, OH))
            eproc.stdin.write(np.ascontiguousarray(out).tobytes())
            done += 1
            if done % 10 == 0:
                rate = done / max(0.01, time.time() - t_start)
                ctx.progress(min(0.99, done / total_frames),
                             f"Frame {done} of {total_frames} · {rate:.0f} frames a second")
        eproc.stdin.close()
        eproc.wait()
    except BaseException:
        eproc.kill()
        eproc.wait()
        raise
    if eproc.returncode != 0:
        raise ExportError((b"".join(err)).decode("utf-8", "replace")[-600:] or "the encoder failed")
    enc_name = "NVENC" if "h264_nvenc" in enc else "x264"
    return f"re-encoded ({enc_name}, {p.output.quality} quality, {OW}x{OH} at {fps:g} fps)"
