"""Model download: resumable, reports progress as (fraction, message) like the app's job context."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class FakeResp:
    def __init__(self, data: bytes, start: int = 0):
        self.data = data[start:]
        self.status_code = 206 if start else 200
        self.headers = {"content-length": str(len(self.data))}

    def iter_content(self, n):
        for i in range(0, len(self.data), 3):
            yield self.data[i:i + 3]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeSession:
    headers: dict = {}

    def get(self, url, headers=None, **kw):
        start = int(headers["Range"].split("=")[1].rstrip("-")) if headers else 0
        return FakeResp(f"file:{url}".encode() * 20, start)


def test_download_with_job_progress(tmp_path, monkeypatch):
    monkeypatch.setenv("NIRVASH_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("NIRVASH_ROOT", str(tmp_path / "root"))
    from nirvash.core import settings as sm
    sm._settings = None
    from nirvash.analysis import models
    from nirvash.jobs.context import JobContext

    monkeypatch.setattr(models.requests, "Session", FakeSession)
    monkeypatch.setattr(models.time, "time", iter(range(0, 10_000_000)).__next__)  # every chunk reports progress
    seen = []
    ctx = JobContext(lambda f, m: seen.append((f, m)))
    d = models.download(models.AST, ctx.progress, ctx.cancel)   # exactly how Settings calls it
    for f in models.AST.files:
        assert (d / f.local).read_bytes().startswith(b"file:")
    assert seen and all(isinstance(f, float) and isinstance(m, str) for f, m in seen)
    assert seen[-1][0] == 1.0
    assert models.model_dir(models.AST) == d
