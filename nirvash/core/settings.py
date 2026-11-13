"""Settings, stored as JSON in the data folder (survives updates)."""
from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any

from . import paths

SHORT_SONG_MODES = {
    "chain": "Chain a second similar song",
    "loop": "Loop the song",
    "trim_fade": "Let the music end early, with a fade-out",
    "long_only": "Only pick songs that are long enough",
}

DEFAULTS: dict[str, Any] = {
    "folders": {
        # Empty = default location under the root folder.
        "root": "",
        "songs": "",
        "overlays": "",
        "models": "",
        "cache": "",
        "originals": "",
    },
    "export": {
        "suffix": "_edited",
        "gpu": False,               # "Export faster (GPU)" - NVENC, used by re-encodes
        "aac_bitrate": "320k",
        "opus_bitrate": "256k",
        "mp3_bitrate": "320k",
    },
    "scan": {
        "recursive": True,
        "rescan": False,
        "min_section_s": 12.0,      # shortest music stretch kept from a partial video
        "full_ratio": 0.9,          # share of a video that must be music to count as "all music"
        "silence_db": -50.0,
        "speech_threshold": 0.5,    # how sure the tagger must be that someone is talking
    },
    "identify": {
        "shazam": True,
        "acoustid_key": "",
        "audiotag_key": "",         # third backup: AudioTag.info, 3 hours of audio a month free
        "musicbrainz": True,
        "itunes": True,
        "lyrics": True,
    },
    "level": {
        "auto": True,               # even out songs that are much louder or quieter than the rest
        "target": -14.0,            # loudness every song is brought towards (LUFS)
        "tolerance": 2.0,           # songs this close to the target are left untouched
        "max_gain": 12.0,           # never change a song by more than this (dB)
    },
    "automusic": {
        "short_mode": "chain",
        "crossfade_s": 3.0,
        "fade_in_s": 0.5,
        "fade_out_s": 2.0,
        "smart_start": True,
        "volume": 1.0,
        "weights": {"length": 0.40, "pace": 0.25, "mood": 0.25, "prefs": 0.10},
        "include_with_sound": False,
        "recursive": False,
    },
    "ui": {
        "volume": 80,
        "geometry": "",
        "last_scan_folders": [],
        "last_auto_folder": "",
        "log_open": False,
    },
    "models": {
        "device": "auto",           # auto | gpu | cpu
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Settings:
    def __init__(self, path: Path | None = None):
        self.path = path or (paths.data_dir() / "settings.json")
        self._lock = threading.RLock()
        self.data = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
            self.data = _merge(DEFAULTS, raw)
        except FileNotFoundError:
            self.data = copy.deepcopy(DEFAULTS)
        except Exception:
            # A broken settings file must never stop the app; keep a copy for inspection.
            try:
                self.path.replace(self.path.with_suffix(".broken.json"))
            except Exception:
                pass
            self.data = copy.deepcopy(DEFAULTS)

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2), "utf-8")
            tmp.replace(self.path)

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def set(self, dotted: str, value: Any, save: bool = True) -> None:
        with self._lock:
            parts = dotted.split(".")
            cur = self.data
            for part in parts[:-1]:
                cur = cur.setdefault(part, {})
            cur[parts[-1]] = value
        if save:
            self.save()

    def reset(self) -> None:
        with self._lock:
            self.data = copy.deepcopy(DEFAULTS)
        self.save()

    # ---- folders -------------------------------------------------------
    def root(self) -> Path:
        r = self.get("folders.root")
        return Path(r) if r else paths.default_root()

    def folder(self, key: str) -> Path:
        custom = self.get(f"folders.{key}")
        if custom:
            p = Path(custom)
        else:
            defaults = {
                "songs": self.root() / "Library" / "Songs",
                "overlays": self.root() / "Library" / "Overlays",
                "models": self.root() / "Models",
                "cache": self.root() / "Cache",
                "originals": self.root() / "Library" / "Originals",
            }
            p = defaults[key]
        return paths.ensure(p)

    def cache(self, sub: str) -> Path:
        return paths.ensure(self.folder("cache") / sub)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
