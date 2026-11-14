"""The overlay library: pictures, GIFs and short videos you put on top of videos, kept in folders under
Library\\Overlays. Adding files copies them in (the originals stay where they were); removing sends them to the
Recycle Bin."""
from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np

from ..core import log as _log
from ..core.settings import get_settings
from ..media import ff

log = _log.get("overlays")

STILL = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
MOVING = {".gif", ".mp4", ".mov", ".webm", ".mkv", ".avi", ".apng"}
KINDS = STILL | MOVING


def root() -> Path:
    return get_settings().folder("overlays")


def folders() -> list[Path]:
    """Every folder in the library, the library itself first."""
    r = root()
    return [r] + sorted((p for p in r.rglob("*") if p.is_dir() and not p.name.startswith(".")),
                        key=lambda p: str(p.relative_to(r)).casefold())


def files(folder: Path | None = None, recursive: bool = False) -> list[Path]:
    f = folder or root()
    it = f.rglob("*") if recursive else f.iterdir()
    return sorted((p for p in it if p.is_file() and p.suffix.lower() in KINDS and not p.name.startswith(".")),
                  key=lambda p: p.name.casefold())


def new_folder(parent: Path, name: str) -> Path:
    from ..jobs.library_files import safe_name, unique_path
    p = unique_path(parent / safe_name(name, 80))
    p.mkdir(parents=True)
    return p


def import_files(paths: list[str], folder: Path) -> list[Path]:
    """Copy files into a library folder (never overwriting). Returns the new paths."""
    from ..jobs.library_files import unique_path
    out = []
    for s in paths:
        src = Path(s)
        if not src.is_file() or src.suffix.lower() not in KINDS:
            continue
        dest = unique_path(folder / src.name)
        shutil.copy2(src, dest)
        out.append(dest)
    log.info("added %d overlay%s to %s", len(out), "s" if len(out) != 1 else "", folder.name)
    return out


def remove(path: Path) -> bool:
    from ..jobs.library_files import to_recycle_bin
    return to_recycle_bin(path)


# ---- reading -------------------------------------------------------------------------------------------------------
def load_frames(path: str, fps: float = 30.0, max_s: float = 30.0, max_w: int = 1920) -> tuple[list[np.ndarray], float]:
    """BGRA frames: one for a picture, up to max_s seconds of a GIF or video (they loop)."""
    import cv2
    ext = Path(path).suffix.lower()
    if ext in STILL:
        img = cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_UNCHANGED)
        if img is None:
            raise ValueError("not a picture OpenCV can read")
        if img.ndim == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA)
        elif img.shape[2] == 3:
            img = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
        if img.dtype != np.uint8:
            img = (img / 257).astype(np.uint8)
        if img.shape[1] > max_w:
            img = cv2.resize(img, (max_w, int(img.shape[0] * max_w / img.shape[1])), interpolation=cv2.INTER_AREA)
        return [img], fps
    pr = ff.probe(path)
    w, h = pr.width, pr.height
    if not w or not h:
        raise ValueError("no picture in it")
    if w > max_w:
        w, h = max_w, max(2, int(h * max_w / w) // 2 * 2)
    src_fps = min(fps, pr.fps or fps)
    args = [ff.ffmpeg(), "-v", "error", "-nostdin", "-i", path, "-t", f"{max_s:.2f}", "-an",
            "-vf", f"fps={src_fps:.3f},scale={w}:{h},format=bgra", "-f", "rawvideo", "pipe:1"]
    proc = ff.run(args, timeout=300)
    n = w * h * 4
    data = proc.stdout
    frames = [np.frombuffer(data[i:i + n], np.uint8).reshape(h, w, 4).copy() for i in range(0, len(data) - n + 1, n)]
    if not frames:
        raise ValueError("couldn't read any frames")
    return frames, src_fps


def thumbnail(path: Path, size: int = 96) -> Path | None:
    """A small PNG of the overlay (its first frame), cached."""
    import cv2
    try:
        st = path.stat()
    except OSError:
        return None
    cache = get_settings().cache("overlay_thumbs") / f"{abs(hash((str(path), st.st_mtime_ns, size)))}.png"
    if cache.exists():
        return cache
    try:
        frames, _ = load_frames(str(path), 30.0, 0.1, max_w=512)
        img = frames[0]
        k = size / max(img.shape[:2])
        img = cv2.resize(img, (max(1, int(img.shape[1] * k)), max(1, int(img.shape[0] * k))),
                         interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".png", img)
        if ok:
            cache.write_bytes(buf.tobytes())
            return cache
    except Exception as e:  # noqa: BLE001
        log.warning("no thumbnail for %s: %s", path.name, e)
    return None
