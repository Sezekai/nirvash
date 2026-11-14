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


def grab(p: Project, t: float, W: int, H: int) -> np.ndarray | None:
    """The canvas picture (after per-clip work) at output time t, at W x H. None past the end or on an error."""
    if not p.clips:
        return None
    i, st = p.clip_at(t)
    clip = p.clips[i]
    st = min(st, max(0.0, clip.out - 0.04))
    proc = ff.run(decode_cmd(p, i, st, W, H, None, frames=1), timeout=60)
    n = W * H * 3
    if len(proc.stdout) < n:
        return None
    return np.frombuffer(proc.stdout[:n], np.uint8).reshape(H, W, 3).copy()


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


def apply_crop(img: np.ndarray, box, OW: int, OH: int) -> np.ndarray:
    """Cut the box out and scale it to OW x OH, at sub-pixel positions so slow moves don't jitter."""
    import cv2
    H, W = img.shape[:2]
    x, y, w, h = box
    bx, by, bw, bh = x * W, y * H, max(1e-3, w * W), max(1e-3, h * H)
    sx, sy = OW / bw, OH / bh
    m = np.array([[sx, 0, -bx * sx], [0, sy, -by * sy]], np.float32)
    interp = cv2.INTER_AREA if sx < 0.7 else cv2.INTER_LINEAR
    if interp == cv2.INTER_AREA:          # warpAffine has no area mode: shrink first, then place exactly
        k = max(sx, sy)
        small = cv2.resize(img, (max(1, int(W * k)), max(1, int(H * k))), interpolation=cv2.INTER_AREA)
        m = np.array([[sx / k, 0, -bx * sx], [0, sy / k, -by * sy]], np.float32)
        return cv2.warpAffine(small, m, (OW, OH), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
    return cv2.warpAffine(img, m, (OW, OH), flags=interp, borderMode=cv2.BORDER_CONSTANT)


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

    def blurs(self, img: np.ndarray, t: float) -> None:
        from .project import interp
        for b in self.p.blurs:
            if b.start <= t <= self.p.blur_end(b):
                apply_blur(img, b, interp(b.keys, t - 0.0))

    def finish(self, img: np.ndarray, t: float, *, crop: bool = True, overlays: bool = True) -> np.ndarray:
        """Crop/zoom then overlays. `img` is the canvas (with blurs done)."""
        box = self.p.crop_box(t) if crop else None
        if box is not None:
            OW, OH = self.out_size
            img = apply_crop(img, box, OW, OH)
        if overlays:
            for o in self.p.overlays:
                if o.start <= t <= self.p.overlay_end(o) and o.path in self.sources:
                    apply_overlay(img, o, self.sources[o.path], t - o.start)
        return img

    def render(self, img: np.ndarray, t: float) -> np.ndarray:
        self.blurs(img, t)
        return self.finish(img, t)


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


def _audio_args(p: Project, first_input: int) -> tuple[list[str], list[str], list[str]]:
    """Inputs, filter graph and maps for the joined sound (each clip's trimmed audio, silence where there's none)."""
    clips = p.clips
    if all(c.mute or not c.has_audio for c in clips):
        return [], [], []
    if len(clips) == 1:
        c = clips[0]
        inputs = ["-ss", f"{c.start:.3f}", "-t", f"{c.length:.3f}", "-i", c.path]
        return inputs, [], ["-map", f"{first_input}:a:0", "-c:a", "aac", "-b:a", "320k"]
    inputs, parts, graph = [], [], []
    k = first_input
    for j, c in enumerate(clips):
        if c.has_audio and not c.mute:
            inputs += ["-ss", f"{c.start:.3f}", "-t", f"{c.length:.3f}", "-i", c.path]
            graph.append(f"[{k}:a:0]aresample=48000,aformat=channel_layouts=stereo,apad,atrim=0:{c.length:.3f},"
                         f"asetpts=N/SR/TB[a{j}]")
            k += 1
        else:
            graph.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{c.length:.3f},asetpts=N/SR/TB[a{j}]")
        parts.append(f"[a{j}]")
    graph.append("".join(parts) + f"concat=n={len(clips)}:v=0:a=1[aout]")
    return inputs, ["-filter_complex", ";".join(graph)], ["-map", "[aout]", "-c:a", "aac", "-b:a", "320k"]


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
        args += ["-i", c.path, "-t", f"{c.out - start:.3f}", "-map", "0:v:0"]
        if c.has_audio and not c.mute:
            args += ["-map", "0:a?"]
        args += ["-c", "copy", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(tmp)]
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
        args = [ff.ffmpeg(), "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy",
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
    a_inputs, a_graph, a_maps = _audio_args(p, 1)
    enc = [ff.ffmpeg(), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{OW}x{OH}",
           "-r", f"{fps:.3f}", "-i", "pipe:0"] + a_inputs + a_graph + ["-map", "0:v:0"] + video_encoder(p) + a_maps
    enc += ["-movflags", "+faststart", str(tmp)]
    log.info("encode: %s", " ".join(enc[1:]))
    eproc = ff.popen(enc, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    err: list[bytes] = []
    threading.Thread(target=lambda: err.append(eproc.stderr.read()), daemon=True).start()
    done = 0
    n = W * H * 3
    t_start = time.time()
    try:
        for i, clip in enumerate(p.clips):
            off = p.clip_offset(i)
            nf = int(round(clip.length * fps))
            dproc = ff.popen(decode_cmd(p, i, clip.start, W, H, fps, duration=clip.length),
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            got = 0
            last = None
            try:
                while got < nf:
                    ctx.check()
                    buf = dproc.stdout.read(n)
                    if len(buf) < n:
                        if last is None:
                            break
                        frame = last.copy()          # pad a short clip with its last frame
                    else:
                        frame = np.frombuffer(buf, np.uint8).reshape(H, W, 3).copy()
                        last = frame
                    t = off + got / fps
                    out = ops.render(frame, t)
                    if out.shape[1] != OW or out.shape[0] != OH:
                        import cv2
                        out = cv2.resize(out, (OW, OH))
                    eproc.stdin.write(np.ascontiguousarray(out).tobytes())
                    got += 1
                    done += 1
                    if done % 10 == 0:
                        rate = done / max(0.01, time.time() - t_start)
                        ctx.progress(done / total_frames,
                                     f"Frame {done} of {total_frames} · {rate:.0f} frames a second")
            finally:
                dproc.kill()
                dproc.wait()
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
