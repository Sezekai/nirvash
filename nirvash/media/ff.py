"""FFmpeg / ffprobe helpers."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from ..core import log as _log
from ..core.paths import bin_path

log = _log.get("ff")

VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".flv", ".wmv", ".asf", ".ts", ".m2ts", ".mts",
              ".mpg", ".mpeg", ".3gp", ".ogv", ".gif"}

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class Cancelled(Exception):
    pass


class FFError(RuntimeError):
    pass


def ffmpeg() -> str:
    return bin_path("ffmpeg")


def ffprobe() -> str:
    return bin_path("ffprobe")


# Starting processes from several threads while anything changes the process environment can crash Windows'
# CreateProcess (it reads the live environment block). So: every child gets its own copy of the environment,
# made from Python's mapping, and processes are started one at a time.
ENV_LOCK = threading.RLock()


def child_env() -> dict[str, str]:
    with ENV_LOCK:
        return dict(os.environ)


def set_env(key: str, value: str) -> None:
    with ENV_LOCK:
        os.environ[key] = value


def popen(args: list[str], **kw) -> subprocess.Popen:
    kw.setdefault("creationflags", NO_WINDOW)
    kw.setdefault("env", child_env())
    with ENV_LOCK:
        return subprocess.Popen(args, **kw)


def run(args: list[str], *, timeout: float | None = None, input: bytes | None = None) -> subprocess.CompletedProcess:
    proc = popen(args, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = proc.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    return subprocess.CompletedProcess(args, proc.returncode, out, err)


@dataclass
class Stream:
    index: int
    kind: str
    codec: str
    raw: dict = field(repr=False)


@dataclass
class Probe:
    path: str
    duration: float
    container: str
    streams: list[Stream]
    raw: dict = field(repr=False)

    @property
    def video(self) -> Stream | None:
        for s in self.streams:
            if s.kind == "video" and not s.raw.get("disposition", {}).get("attached_pic"):
                return s
        return None

    @property
    def audio(self) -> Stream | None:
        return next((s for s in self.streams if s.kind == "audio"), None)

    @property
    def width(self) -> int:
        v = self.video
        return int(v.raw.get("width", 0)) if v else 0

    @property
    def height(self) -> int:
        v = self.video
        return int(v.raw.get("height", 0)) if v else 0

    @property
    def fps(self) -> float:
        v = self.video
        if not v:
            return 0.0
        for key in ("avg_frame_rate", "r_frame_rate"):
            r = v.raw.get(key, "0/0")
            try:
                a, b = r.split("/")
                if float(b):
                    f = float(a) / float(b)
                    if 0 < f < 1000:
                        return f
            except Exception:
                pass
        return 0.0


def probe(path: str | Path) -> Probe:
    p = run([ffprobe(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], timeout=60)
    if p.returncode != 0:
        raise FFError(p.stderr.decode("utf-8", "replace").strip()[-400:] or "ffprobe failed")
    data = json.loads(p.stdout or b"{}")
    fmt = data.get("format", {})
    streams = [Stream(int(s.get("index", i)), s.get("codec_type", ""), s.get("codec_name", ""), s)
               for i, s in enumerate(data.get("streams", []))]
    dur = 0.0
    try:
        dur = float(fmt.get("duration") or 0)
    except Exception:
        pass
    if not dur:
        for s in streams:
            try:
                dur = max(dur, float(s.raw.get("duration") or 0))
            except Exception:
                pass
    return Probe(str(path), dur, fmt.get("format_name", ""), streams, data)


def decode_audio(path: str | Path, sr: int = 48000, *, start: float = 0.0, duration: float | None = None,
                 stream: int | None = None) -> np.ndarray:
    """Decode to mono float32 at `sr`."""
    args = [ffmpeg(), "-v", "error", "-nostdin"]
    if start > 0:
        args += ["-ss", f"{start:.3f}"]
    args += ["-i", str(path)]
    if duration is not None:
        args += ["-t", f"{duration:.3f}"]
    args += ["-map", f"0:{stream}" if stream is not None else "0:a:0", "-vn", "-ac", "1", "-ar", str(sr),
             "-f", "f32le", "-acodec", "pcm_f32le", "pipe:1"]
    p = run(args, timeout=1800)
    if p.returncode != 0 and not p.stdout:
        raise FFError(p.stderr.decode("utf-8", "replace").strip()[-400:] or "audio decode failed")
    return np.frombuffer(p.stdout, np.float32).copy()


def gray_frames(path: str | Path, fps: float, w: int, h: int, max_frames: int = 2000) -> np.ndarray:
    """Small grayscale frames sampled at `fps` -> array (n, h, w) uint8."""
    args = [ffmpeg(), "-v", "error", "-nostdin", "-i", str(path), "-an", "-sn",
            "-vf", f"fps={fps},scale={w}:{h}:flags=area,format=gray", "-frames:v", str(max_frames),
            "-f", "rawvideo", "pipe:1"]
    p = run(args, timeout=1800)
    buf = np.frombuffer(p.stdout, np.uint8)
    n = len(buf) // (w * h)
    return buf[: n * w * h].reshape(n, h, w)


def rgb_frames_at(path: str | Path, times: list[float], size: int = 256) -> list[np.ndarray]:
    """RGB frames at given times, scaled so the short side is `size` -> list of (h, w, 3) uint8."""
    out = []
    for t in times:
        vf = f"scale={size}:{size}:force_original_aspect_ratio=increase:flags=bicubic,crop={size}:{size},format=rgb24"
        args = [ffmpeg(), "-v", "error", "-nostdin", "-ss", f"{max(0.0, t):.3f}", "-i", str(path), "-an", "-sn",
                "-frames:v", "1", "-vf", vf, "-f", "rawvideo", "pipe:1"]
        p = run(args, timeout=120)
        if len(p.stdout) >= size * size * 3:
            out.append(np.frombuffer(p.stdout[: size * size * 3], np.uint8).reshape(size, size, 3))
    return out


_TIME_RE = re.compile(r"out_time_(?:us|ms)=(\d+)")


def run_with_progress(args: list[str], total_s: float, on_progress: Callable[[float], None] | None = None,
                      cancel: threading.Event | None = None) -> None:
    """Run ffmpeg with -progress, reporting 0..1. Raises FFError / Cancelled."""
    full = [args[0], "-hide_banner", "-nostdin", "-progress", "pipe:1", "-nostats"] + args[1:]
    log.info("ffmpeg %s", " ".join(f'"{a}"' if " " in a else a for a in full[1:]))
    proc = popen(full, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    err_chunks: list[bytes] = []

    def _drain():
        assert proc.stderr
        for chunk in iter(lambda: proc.stderr.read(4096), b""):
            err_chunks.append(chunk)
            if len(err_chunks) > 200:
                del err_chunks[:100]

    t = threading.Thread(target=_drain, daemon=True)
    t.start()
    assert proc.stdout
    for line in iter(proc.stdout.readline, b""):
        if cancel is not None and cancel.is_set():
            proc.kill()
            proc.wait()
            raise Cancelled()
        m = _TIME_RE.search(line.decode("ascii", "ignore"))
        if m and on_progress and total_s > 0:
            on_progress(min(1.0, int(m.group(1)) / 1e6 / total_s))
    proc.wait()
    t.join(timeout=2)
    if proc.returncode != 0:
        err = b"".join(err_chunks).decode("utf-8", "replace").strip()
        raise FFError(err[-800:] or f"ffmpeg exited with {proc.returncode}")
    if on_progress:
        on_progress(1.0)


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTS and path.suffix.lower() != ".gif"


def list_videos(folder: str | Path, recursive: bool) -> list[Path]:
    root = Path(folder)
    it = root.rglob("*") if recursive else root.glob("*")
    out = []
    for p in it:
        try:
            if p.is_file() and is_video(p):
                out.append(p)
        except OSError:
            pass
    return sorted(out, key=lambda p: str(p).lower())


def fmt_time(s: float | None) -> str:
    if s is None:
        return "–"
    s = max(0.0, float(s))
    m, sec = divmod(int(round(s)), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"
