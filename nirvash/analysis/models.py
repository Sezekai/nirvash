"""AI model files: where they are, whether they're present, and downloading them."""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

from ..core import log as _log
from ..core.paths import collection_sorter_clip_dir, ensure
from ..core.settings import get_settings

log = _log.get("models")

HF = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")


@dataclass(frozen=True)
class ModelFile:
    repo: str
    remote: str
    local: str  # relative to the model's folder


@dataclass(frozen=True)
class ModelSpec:
    key: str
    label: str
    use: str
    folder: str
    files: tuple[ModelFile, ...]
    approx_mb: int


AST = ModelSpec(
    "ast", "Audio tagger (AST, AudioSet)", "Finds music, talking and singing; song mood", "audio-tagger",
    (ModelFile("Xenova/ast-finetuned-audioset-10-10-0.4593", "onnx/model.onnx", "model.onnx"),
     ModelFile("Xenova/ast-finetuned-audioset-10-10-0.4593", "config.json", "config.json")),
    350)

CLIP = ModelSpec(
    "clip", "CLIP ViT-B/32 (images)", "Video mood for auto-matching", "clip",
    (ModelFile("Xenova/clip-vit-base-patch32", "onnx/vision_model.onnx", "vision_model.onnx"),
     ModelFile("Xenova/clip-vit-base-patch32", "onnx/text_model.onnx", "text_model.onnx")),
    600)

SPECS = {s.key: s for s in (AST, CLIP)}


def models_root() -> Path:
    return get_settings().folder("models")


def own_dir(spec: ModelSpec) -> Path:
    return models_root() / spec.folder


def model_dir(spec: ModelSpec) -> Path | None:
    """Folder that holds a complete copy of the model, or None.

    CLIP is read from Collection Sorter's folder first, then Nirvash's own Models folder."""
    candidates = []
    if spec.key == "clip":
        candidates.append(collection_sorter_clip_dir())
    candidates.append(own_dir(spec))
    for d in candidates:
        if all((d / f.local).exists() for f in spec.files):
            return d
    return None


def status(spec: ModelSpec) -> dict:
    d = model_dir(spec)
    shared = d is not None and spec.key == "clip" and d == collection_sorter_clip_dir()
    return {"present": d is not None, "dir": str(d) if d else "", "shared": shared}


def download(spec: ModelSpec, on_progress: Callable[[float, str], None] | None = None,
             cancel: threading.Event | None = None) -> Path:
    """Download any missing files of `spec` into Nirvash's own Models folder (resumable)."""
    dest_dir = ensure(own_dir(spec))
    files = [f for f in spec.files if not (dest_dir / f.local).exists()]
    sess = requests.Session()
    sess.headers["User-Agent"] = "Nirvash"
    for i, f in enumerate(files):
        dest = dest_dir / f.local
        part = dest.with_suffix(dest.suffix + ".part")
        url = f"{HF}/{f.repo}/resolve/main/{f.remote}"
        for attempt in range(1, 6):
            try:
                have = part.stat().st_size if part.exists() else 0
                headers = {"Range": f"bytes={have}-"} if have else {}
                with sess.get(url, headers=headers, stream=True, timeout=60, allow_redirects=True) as r:
                    if r.status_code == 416:
                        part.replace(dest)
                        break
                    if r.status_code not in (200, 206):
                        raise RuntimeError(f"HTTP {r.status_code} for {f.remote}")
                    if r.status_code == 200:
                        have = 0
                    total = have + int(r.headers.get("content-length") or 0)
                    with open(part, "ab" if r.status_code == 206 else "wb") as fh:
                        got = have
                        last = 0.0
                        for chunk in r.iter_content(1 << 20):
                            if cancel is not None and cancel.is_set():
                                raise InterruptedError("cancelled")
                            fh.write(chunk)
                            got += len(chunk)
                            if on_progress and time.time() - last > 0.2:
                                last = time.time()
                                frac = (i + (got / total if total else 0)) / max(1, len(files))
                                on_progress(frac, f"{spec.label}: {f.local} ({got / 1e6:.0f}/{total / 1e6:.0f} MB)")
                part.replace(dest)
                break
            except InterruptedError:
                raise
            except Exception as e:
                log.warning("download %s attempt %d failed: %s", f.remote, attempt, e, exc_info=attempt == 1)
                if attempt == 5:
                    raise
                time.sleep(2 * attempt)
    if on_progress:
        on_progress(1.0, f"{spec.label}: done")
    return dest_dir
