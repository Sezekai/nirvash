"""Background song identification: Shazam first, AcoustID as backup, then extra info and lyrics."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

import numpy as np

from ..analysis import audio_features as af
from ..analysis.fingerprint import fingerprint_compressed
from ..core import log as _log
from ..core.db import get_db
from ..core.settings import get_settings
from ..lookup import services
from ..media import ff

log = _log.get("identify")

SR = 16000
SEG = 12.0


def pick_segments(y: np.ndarray, sr: int, n: int = 3) -> list[float]:
    """Start times of up to n loud 12 s segments, spread across the clip."""
    dur = len(y) / sr
    if dur <= SEG + 1:
        return [0.0]
    env = af.loudness_envelope(y, sr, 1.0)
    win = int(SEG)
    c = np.convolve(env, np.ones(win) / win, mode="valid")
    out: list[float] = []
    zones = np.array_split(np.arange(len(c)), n)
    for z in zones:
        if len(z):
            out.append(float(z[int(np.argmax(c[z]))]))
    # Loudest first.
    out.sort(key=lambda t: -c[int(t)])
    return out


def _merge(dst: dict, src: dict | None, keys: tuple[str, ...], overwrite: bool = False) -> None:
    if not src:
        return
    for k in keys:
        v = src.get(k)
        if v and (overwrite or not dst.get(k)):
            dst[k] = v


def identify_song(song_id: int, cancel: threading.Event | None = None) -> dict:
    db = get_db()
    s = get_settings()
    row = db.song(song_id)
    if row is None:
        return {}
    path = Path(row["file_path"])
    upd: dict[str, Any] = {}
    found_by = None
    try:
        y = ff.decode_audio(path, SR)
    except Exception as e:
        db.update_song(song_id, id_status="error")
        log.warning("could not read %s: %s", path, e)
        return {}
    info: dict[str, Any] = {}
    tries = errors = 0
    if s.get("identify.shazam", True):
        for start in pick_segments(y, SR):
            tries += 1
            if cancel is not None and cancel.is_set():
                raise InterruptedError()
            seg = y[int(start * SR): int((start + SEG) * SR)]
            try:
                r = services.shazam(seg, SR, cancel)
            except InterruptedError:
                raise
            except Exception as e:
                log.info("Shazam failed for %s: %s", path.name, e)
                errors += 1
                r = None
            if r and r.get("title"):
                info = r
                found_by = "Shazam"
                if r.get("match_offset") is not None:
                    info["clip_offset"] = max(0.0, r["match_offset"] - start)
                break
    key = (s.get("identify.acoustid_key") or "").strip()
    if not info and key:
        fpc = fingerprint_compressed(str(path))
        if fpc:
            tries += 1
            try:
                r = services.acoustid(fpc[0], fpc[1], key)
            except Exception as e:
                log.info("AcoustID failed for %s: %s", path.name, e)
                errors += 1
                r = None
            if r and r.get("title"):
                info = r
                found_by = "AcoustID"
    if not info:
        # Every lookup failing (e.g. no internet) is an error to retry, not "not found".
        db.update_song(song_id, id_status="error" if tries and errors == tries else "not_found")
        return {}
    title, artist = info.get("title"), info.get("artist")
    if s.get("identify.itunes", True) and title:
        try:
            _merge(info, services.itunes(title, artist), ("full_length", "genre", "album", "year", "cover_url"))
        except Exception as e:
            log.info("iTunes lookup failed: %s", e)
    if s.get("identify.musicbrainz", True) and title:
        try:
            mb = services.musicbrainz(info.get("mbid"), title, artist)
            _merge(info, mb, ("mbid", "full_length", "album", "year"))
            if mb and mb.get("genre"):
                info["genre"] = mb["genre"] if not info.get("genre") else info["genre"]
        except Exception as e:
            log.info("MusicBrainz lookup failed: %s", e)
    if s.get("identify.lyrics", True) and title:
        try:
            ly = services.lrclib(title, artist, info.get("album"), info.get("full_length"))
            if ly:
                info["lyrics_plain"] = ly.get("lyrics_plain")
                info["lyrics_synced"] = ly.get("lyrics_synced")
                if not info.get("full_length") and ly.get("full_length"):
                    info["full_length"] = ly["full_length"]
        except Exception as e:
            log.info("LRCLIB lookup failed: %s", e)
    keys = ["album", "genre", "year", "cover_url", "shazam_key", "isrc", "mbid", "full_length", "clip_offset",
            "lyrics_plain", "lyrics_synced"]
    if not row["manual_info"]:
        keys = ["title", "artist"] + keys
    for k in keys:
        if info.get(k) is not None:
            upd[k] = info[k]
    upd["id_status"] = "found"
    upd["id_source"] = found_by
    db.update_song(song_id, **upd)
    if upd.get("cover_url"):
        cover = s.cache("covers") / f"{song_id}.jpg"
        services.download(upd["cover_url"], cover)
    log.info("identified %s as %s - %s (%s)", path.name, artist, title, found_by)
    return upd


class IdentifyQueue:
    """Single background worker that identifies pending songs one by one.

    Starts after `start_delay` seconds so it never competes with the app opening. on_status gets a dict:
    pending, current, done (this run), started (time), busy."""

    def __init__(self, on_change: Callable[[int], None] | None = None,
                 on_status: Callable[[dict], None] | None = None, start_delay: float = 4.0):
        self.on_change = on_change
        self.on_status = on_status
        self.start_delay = start_delay
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._cancel = threading.Event()
        self._paused = False
        self.current = ""
        self.done = 0
        self.started = 0.0
        self._t = threading.Thread(target=self._run, daemon=True, name="identify")
        self._t.start()

    def poke(self) -> None:
        self._wake.set()

    def pause(self, paused: bool) -> None:
        self._paused = paused
        if not paused:
            self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._cancel.set()
        self._wake.set()

    def pending(self) -> int:
        r = get_db().one("SELECT COUNT(*) FROM songs WHERE id_status='pending'")
        return int(r[0]) if r else 0

    def _status(self, msg: str) -> None:
        self.current = msg
        if self.on_status:
            self.on_status({"pending": self.pending(), "current": msg, "done": self.done,
                            "started": self.started, "busy": bool(msg)})

    def _run(self) -> None:
        if self._stop.wait(self.start_delay):
            return
        while not self._stop.is_set():
            row = None
            if not self._paused:
                row = get_db().one("SELECT id, file_path FROM songs WHERE id_status='pending' ORDER BY added_at LIMIT 1")
            if row is None:
                # Queue empty: the next batch starts its own count and clock.
                self.done = 0
                self.started = 0.0
                self._status("")
                self._wake.wait(30)
                self._wake.clear()
                continue
            if not self.started:
                self.started = time.time()
            try:
                self._status(Path(row["file_path"]).name)
                identify_song(row["id"], self._cancel)
            except InterruptedError:
                return
            except Exception:
                log.exception("identify failed")
                get_db().update_song(row["id"], id_status="error")
            self.done += 1
            if self.on_change:
                self.on_change(row["id"])
