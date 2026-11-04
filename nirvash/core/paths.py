"""Where everything lives.

Layout (defaults, all changeable in Settings):

    E:\\Pictures\\Nirvash\\            <- root (the folder that holds App\\)
        App\\                         <- the program, replaced by updates
        Library\\Songs\\               <- extracted songs
        Library\\Overlays\\            <- overlay images (stage 2)
        Models\\                      <- AI models
        Cache\\                       <- previews, waveforms, covers; safe to clear

    %LOCALAPPDATA%\\Nirvash\\          <- small files that survive updates
        settings.json, nirvash.db, projects\\, logs\\
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .. import APP_NAME


def app_dir() -> Path:
    """Folder holding the program (Nirvash.exe, or the repo when run from source)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def bundle_dir() -> Path:
    """Folder holding bundled read-only files (assets, bin)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", app_dir()))
    return app_dir()


def default_root() -> Path:
    env = os.environ.get("NIRVASH_ROOT")
    if env:
        return Path(env)
    a = app_dir()
    if a.name.lower() == "app":
        return a.parent
    if getattr(sys, "frozen", False):
        return a
    return a / "_dev_root"


def data_dir() -> Path:
    env = os.environ.get("NIRVASH_DATA")
    if env:
        p = Path(env)
    elif sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        p = Path(base) / APP_NAME
    else:
        p = Path.home() / ".local" / "share" / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def local_appdata() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local"))
    return Path.home() / ".local" / "share"


def collection_sorter_clip_dir() -> Path:
    return local_appdata() / "Collection Sorter" / "models" / "clip"


def bin_path(name: str) -> str:
    """Full path of a bundled tool (ffmpeg, ffprobe, fpcalc), or its bare name to use PATH."""
    exe = name + (".exe" if sys.platform == "win32" else "")
    for d in (bundle_dir() / "bin", app_dir() / "bin"):
        p = d / exe
        if p.exists():
            return str(p)
    return name


def asset(name: str) -> Path:
    return bundle_dir() / "assets" / name


def ensure(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p
