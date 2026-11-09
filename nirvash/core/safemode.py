"""Safe mode: after two unclean exits in a row, start with the risky parts off (playback and the graphics card),
say why, and offer to turn them back on (STANDARDS.md, "Crash safety")."""
from __future__ import annotations

import json
import os
import time

from . import paths

_state: dict = {}
_active = False


def _file():
    return paths.data_dir() / "run_state.json"


def start() -> bool:
    """Call once at startup. Returns True when this run is in safe mode."""
    global _active, _state
    try:
        _state = json.loads(_file().read_text("utf-8"))
    except Exception:
        _state = {}
    unclean = int(_state.get("unclean", 0))
    if _state.get("running"):
        unclean += 1
    else:
        unclean = 0
    _active = unclean >= 2 and not _state.get("force_normal") and not os.environ.get("NIRVASH_NO_SAFE_MODE")
    _state.update(running=True, unclean=unclean, started=time.time(), force_normal=False, safe=_active)
    _save()
    return _active


def clean_exit() -> None:
    _state.update(running=False, unclean=0)
    _save()


def turn_off() -> None:
    """Next start runs normally (the button in the safe-mode banner)."""
    _state.update(running=False, unclean=0, force_normal=True)
    _save()


def active() -> bool:
    return _active


def unclean_count() -> int:
    return int(_state.get("unclean", 0))


def _save() -> None:
    try:
        tmp = _file().with_suffix(".tmp")
        tmp.write_text(json.dumps(_state), "utf-8")
        tmp.replace(_file())
    except Exception:
        pass
