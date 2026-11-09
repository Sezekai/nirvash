"""Settings > Storage: how big each place is, moving a place (copy, check, switch, then remove the old copy) and
clearing the cache. Only Nirvash's own folders are touched here, never your videos."""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from ..core import log as _log
from ..core import paths
from ..core.db import get_db
from ..core.settings import get_settings
from .context import JobContext

log = _log.get("storage")

# key, what it holds, can be moved, can be cleared
PLACES = [
    ("songs", "Song library", True, False),
    ("models", "AI models", True, False),
    ("cache", "Cache: previews, waveforms, cover art", True, True),
    ("overlays", "Overlays (for the editor)", True, False),
    ("data", "Settings, library database, logs and themes", True, False),
]


def place_path(key: str) -> Path:
    if key == "data":
        return paths.data_dir()
    s = get_settings()
    custom = s.get(f"folders.{key}")
    if custom:
        return Path(custom)
    return {"songs": s.root() / "Library" / "Songs", "overlays": s.root() / "Library" / "Overlays",
            "models": s.root() / "Models", "cache": s.root() / "Cache"}[key]


def folder_size(p: Path) -> tuple[int, int]:
    """(bytes, files) under a folder."""
    total = files = 0
    if not p.exists():
        return 0, 0
    for root, _dirs, fs in os.walk(p):
        for f in fs:
            try:
                total += os.path.getsize(os.path.join(root, f))
                files += 1
            except OSError:
                pass
    return total, files


def human(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("bytes", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def missing_places() -> list[tuple[str, str, Path]]:
    """Places on a drive that isn't connected: (key, label, path)."""
    out = []
    for key, lab, *_ in PLACES:
        p = place_path(key)
        if not paths.drive_ok(p):
            out.append((key, lab, p))
    return out


def _skip(name: str) -> bool:
    return name in (paths.POINTER,)


def copy_tree(ctx: JobContext, src: Path, dst: Path, label: str) -> tuple[int, int]:
    """Copy everything with byte-level progress (bar, %, MB/s). Returns (bytes, files)."""
    total, nfiles = folder_size(src)
    done = 0
    t0 = time.time()
    copied = 0
    for root, _dirs, fs in os.walk(src):
        rel = Path(root).relative_to(src)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for f in fs:
            if _skip(f) and rel == Path("."):
                continue
            a, b = Path(root) / f, dst / rel / f
            with open(a, "rb") as fi, open(b, "wb") as fo:
                while True:
                    ctx.check()
                    chunk = fi.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    fo.write(chunk)
                    done += len(chunk)
                    el = max(0.001, time.time() - t0)
                    ctx.progress(0.95 * done / max(1, total),
                                 f"{label}: {human(done)} of {human(total)} · {human(done / el)}/s")
            shutil.copystat(a, b, follow_symlinks=False)
            copied += 1
    return total, nfiles


def verify(src: Path, dst: Path) -> None:
    """Same files, same sizes - or the move stops before switching."""
    for root, _dirs, fs in os.walk(src):
        rel = Path(root).relative_to(src)
        for f in fs:
            if _skip(f) and rel == Path("."):
                continue
            a, b = Path(root) / f, dst / rel / f
            if not b.exists() or b.stat().st_size != a.stat().st_size:
                raise RuntimeError(f"The copy of {rel / f} doesn't match, so nothing was switched.")


def move_place(ctx: JobContext, key: str, new_parent: str) -> dict:
    """Copy a place into new_parent, check it, switch to it, then remove the old copy."""
    src = place_path(key)
    dst = Path(new_parent) / (src.name if key != "data" else "Nirvash data")
    if dst.resolve() == src.resolve():
        raise RuntimeError("That's where it already is.")
    if dst.exists() and any(dst.iterdir()):
        raise RuntimeError(f"{dst} already has files in it. Choose an empty place; nothing is ever overwritten.")
    if not paths.drive_ok(dst):
        raise RuntimeError(f"The drive {dst.anchor} isn't connected.")
    dst.mkdir(parents=True, exist_ok=True)
    label = dict((k, lab) for k, lab, *_ in PLACES)[key]
    if key == "data":
        try:
            get_db().x("PRAGMA wal_checkpoint(FULL)")
        except Exception:
            pass
    if src.exists():
        copy_tree(ctx, src, dst, f"Copying {label.split(':')[0].lower()}")
        ctx.progress(0.96, "Checking the copy")
        verify(src, dst)
    restart = False
    if key == "data":
        # The database and logs are open, so the switch happens at the next start (the old copy goes then too).
        ptr = paths.home_data_dir() / paths.POINTER
        tmp = ptr.with_suffix(".tmp")
        tmp.write_text(json.dumps({"data": str(dst), "delete_old": str(src)}), "utf-8")
        tmp.replace(ptr)
        restart = True
    else:
        if key == "songs":
            _repoint_songs(src, dst)
        get_settings().set(f"folders.{key}", str(dst))
        ctx.progress(0.98, "Removing the old copy")
        if src.exists():
            shutil.rmtree(src, ignore_errors=True)
    log.info("moved %s: %s -> %s", key, src, dst)
    return {"key": key, "from": str(src), "to": str(dst), "restart": restart}


def _repoint_songs(old: Path, new: Path) -> None:
    db = get_db()
    o = str(old).rstrip("\\/")
    rows = db.q("SELECT id, file_path FROM songs")
    for r in rows:
        fp = r["file_path"]
        if fp.casefold().startswith(o.casefold() + os.sep.casefold()) or fp.casefold().startswith(o.casefold() + "/"):
            db.update_song(r["id"], file_path=str(new) + fp[len(o):])


def clear_cache(ctx: JobContext | None = None) -> int:
    """Delete the cache (Nirvash's own files, safe to remove). Returns bytes freed."""
    p = place_path("cache")
    size, _n = folder_size(p)
    if p.exists():
        for child in p.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                try:
                    child.unlink()
                except OSError:
                    pass
    return size


def finish_data_move() -> None:
    """At startup: after the small-data folder moved, remove the old copy (keeping the pointer file)."""
    ptr = paths.read_pointer()
    old, new = ptr.get("delete_old"), ptr.get("data")
    if not old or not new or Path(new) != paths.data_dir():
        return
    oldp = Path(old)
    home = paths.home_data_dir()
    try:
        if oldp.resolve() == home.resolve():
            for child in home.iterdir():
                if child.name == paths.POINTER:
                    continue
                shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(missing_ok=True)
        elif oldp.exists():
            shutil.rmtree(oldp, ignore_errors=True)
    except Exception as e:
        log.warning("couldn't remove the old data folder %s: %s", old, e)
        return
    ptr.pop("delete_old", None)
    (home / paths.POINTER).write_text(json.dumps(ptr), "utf-8")
