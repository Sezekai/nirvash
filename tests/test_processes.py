"""Starting helper processes from many threads while the environment changes must never crash."""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_every_child_gets_its_own_environment(monkeypatch):
    from nirvash.media import ff
    seen = []
    real = ff.subprocess.Popen

    def spy(args, **kw):
        seen.append(kw.get("env"))
        return real(args, **kw)

    monkeypatch.setattr(ff.subprocess, "Popen", spy)
    ff.run([ff.ffmpeg(), "-version"])
    assert seen and isinstance(seen[0], dict) and seen[0]


def test_processes_from_threads_while_env_changes():
    from nirvash.media import ff
    errors = []
    stop = threading.Event()

    def worker():
        try:
            for _ in range(40):
                p = ff.run([ff.ffprobe(), "-version"], timeout=60)
                assert p.returncode == 0
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=worker) for _ in range(4)]
    for t in ts:
        t.start()
    i = 0
    while any(t.is_alive() for t in ts):
        os.environ["NIRVASH_TEST_VAR"] = "x" * (i % 500)   # raw change, like native code would do
        ff.set_env("NIRVASH_TEST_VAR2", str(i))
        i += 1
    for t in ts:
        t.join()
    assert not errors, errors
