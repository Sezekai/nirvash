"""Online song lookups: Shazam, AcoustID + MusicBrainz, AudD, iTunes, LRCLIB."""
from __future__ import annotations

import asyncio
import io
import threading
import time
import wave
from collections import deque

import numpy as np
import requests

from .. import APP_NAME, VERSION
from ..core import log as _log

log = _log.get("lookup")
UA = f"{APP_NAME}/{VERSION} (desktop app)"

_http = requests.Session()
_http.headers["User-Agent"] = UA


class RateLimiter:
    def __init__(self, n: int, per_s: float):
        self.n, self.per = n, per_s
        self.times: deque[float] = deque()
        self.lock = threading.Lock()

    def wait(self, cancel: threading.Event | None = None) -> None:
        while True:
            with self.lock:
                now = time.time()
                while self.times and now - self.times[0] > self.per:
                    self.times.popleft()
                if len(self.times) < self.n:
                    self.times.append(now)
                    return
                sleep = self.per - (now - self.times[0]) + 0.05
            if cancel is not None and cancel.wait(min(sleep, 1.0)):
                raise InterruptedError("cancelled")
            elif cancel is None:
                time.sleep(sleep)


SHAZAM_LIMIT = RateLimiter(18, 60.0)   # Shazam allows about 20 a minute
MB_LIMIT = RateLimiter(1, 1.1)         # MusicBrainz: 1 request per second
ITUNES_LIMIT = RateLimiter(18, 60.0)
AUDD_LIMIT = RateLimiter(20, 60.0)


def wav_bytes(y: np.ndarray, sr: int) -> bytes:
    pcm = (np.clip(y, -1, 1) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return buf.getvalue()


# ---- Shazam ----------------------------------------------------------------
_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()
_shazam = None


def _run_async(coro):
    global _loop
    with _loop_lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, daemon=True, name="shazam-loop").start()
    fut = asyncio.run_coroutine_threadsafe(coro, _loop)
    try:
        return fut.result(timeout=25)
    except TimeoutError:
        fut.cancel()
        raise RuntimeError("Shazam didn't answer within 25 s")


def shazam(y: np.ndarray, sr: int, cancel: threading.Event | None = None) -> dict | None:
    """Recognise a mono chunk. Returns normalised info or None."""
    from shazamio import Shazam

    SHAZAM_LIMIT.wait(cancel)

    global _shazam
    data = wav_bytes(y, sr)

    async def go():
        global _shazam
        if _shazam is None:
            _shazam = Shazam()
        return await _shazam.recognize(data)

    out = _run_async(go())
    track = (out or {}).get("track")
    if not track:
        return None
    info: dict = {
        "title": track.get("title"),
        "artist": track.get("subtitle"),
        "shazam_key": str(track.get("key") or ""),
        "genre": (track.get("genres") or {}).get("primary"),
        "cover_url": (track.get("images") or {}).get("coverarthq") or (track.get("images") or {}).get("coverart"),
        "isrc": track.get("isrc"),
    }
    for sec in track.get("sections") or []:
        for md in sec.get("metadata") or []:
            if md.get("title") == "Album":
                info["album"] = md.get("text")
            elif md.get("title") == "Released":
                info["year"] = md.get("text")
    matches = out.get("matches") or []
    if matches:
        try:
            info["match_offset"] = float(matches[0].get("offset") or 0)
        except Exception:
            pass
    return info


# ---- AudD (optional, needs your own token: 300 free lookups, then paid) ------
class AudDError(RuntimeError):
    def __init__(self, msg: str, code: int | None = None):
        super().__init__(msg)
        self.code = code

    @property
    def stop(self) -> bool:
        """Errors that will happen for every song (bad token, no lookups left): stop using AudD this run."""
        return self.code in (900, 901, 902, 903, 904)


def _mmss(t: str | None) -> float | None:
    try:
        parts = [float(x) for x in (t or "").split(":")]
    except ValueError:
        return None
    v = 0.0
    for x in parts:
        v = v * 60 + x
    return v if parts else None


def audd(y: np.ndarray, sr: int, token: str) -> dict | None:
    """Recognise about 12 s of audio with AudD (https://audd.io)."""
    AUDD_LIMIT.wait()
    r = _http.post("https://api.audd.io/", data={"api_token": token, "return": "apple_music"},
                   files={"file": ("clip.wav", wav_bytes(y, sr), "audio/wav")}, timeout=60)
    r.raise_for_status()
    data = r.json()
    if data.get("status") == "error":
        err = data.get("error") or {}
        raise AudDError(f"AudD: {err.get('error_message', 'error')}", err.get("error_code"))
    res = data.get("result")
    if not res or not res.get("title"):
        return None
    info: dict = {"title": res.get("title"), "artist": res.get("artist"), "album": res.get("album")}
    if res.get("release_date"):
        info["year"] = str(res["release_date"])[:4]
    am = res.get("apple_music") or {}
    if am.get("durationInMillis"):
        info["full_length"] = am["durationInMillis"] / 1000.0
    if am.get("genreNames"):
        info["genre"] = am["genreNames"][0]
    if am.get("isrc"):
        info["isrc"] = am["isrc"]
    art = (am.get("artwork") or {}).get("url")
    if art:
        info["cover_url"] = art.replace("{w}", "600").replace("{h}", "600")
    off = _mmss(res.get("timecode"))
    if off is not None:
        info["match_offset"] = off
    return info


# ---- AcoustID / MusicBrainz -------------------------------------------------
def acoustid(fp: str, duration: int, key: str) -> dict | None:
    r = _http.post("https://api.acoustid.org/v2/lookup", data={
        "client": key, "duration": duration, "fingerprint": fp,
        "meta": "recordings releasegroups compress"}, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"AcoustID: {data.get('error', {}).get('message', 'error')}")
    best = None
    for res in data.get("results") or []:
        if res.get("score", 0) < 0.6:
            continue
        for rec in res.get("recordings") or []:
            if not rec.get("title"):
                continue
            cand = (res["score"], rec)
            if best is None or cand[0] > best[0]:
                best = cand
    if not best:
        return None
    rec = best[1]
    artists = ", ".join(a.get("name", "") for a in rec.get("artists") or [])
    info = {"title": rec.get("title"), "artist": artists or None, "mbid": rec.get("id")}
    if rec.get("duration"):
        info["full_length"] = float(rec["duration"])
    rgs = rec.get("releasegroups") or []
    if rgs:
        info["album"] = rgs[0].get("title")
    return info


def musicbrainz(mbid: str | None = None, title: str | None = None, artist: str | None = None) -> dict | None:
    MB_LIMIT.wait()
    base = "https://musicbrainz.org/ws/2/recording"
    if mbid:
        r = _http.get(f"{base}/{mbid}", params={"inc": "genres+tags+releases+artist-credits", "fmt": "json"}, timeout=30)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        rec = r.json()
    else:
        if not title:
            return None
        q = f'recording:"{title}"' + (f' AND artist:"{artist}"' if artist else "")
        r = _http.get(base, params={"query": q, "limit": 5, "fmt": "json"}, timeout=30)
        r.raise_for_status()
        recs = [x for x in r.json().get("recordings") or [] if int(x.get("score", 0)) >= 90]
        if not recs:
            return None
        rec = recs[0]
        MB_LIMIT.wait()
        r = _http.get(f"{base}/{rec['id']}", params={"inc": "genres+tags+releases", "fmt": "json"}, timeout=30)
        if r.ok:
            rec = r.json()
    info: dict = {"mbid": rec.get("id")}
    if rec.get("length"):
        info["full_length"] = rec["length"] / 1000.0
    genres = sorted(rec.get("genres") or [], key=lambda g: -g.get("count", 0))
    tags = sorted(rec.get("tags") or [], key=lambda g: -g.get("count", 0))
    names = [g["name"] for g in genres[:3]] or [t["name"] for t in tags[:3]]
    if names:
        info["genre"] = ", ".join(n.title() for n in names)
    rels = rec.get("releases") or []
    if rels:
        info["album"] = rels[0].get("title")
        if rels[0].get("date"):
            info["year"] = rels[0]["date"][:4]
    return info


# ---- iTunes (full length, genre, year) --------------------------------------
def itunes(title: str, artist: str | None) -> dict | None:
    ITUNES_LIMIT.wait()
    term = f"{title} {artist or ''}".strip()
    r = _http.get("https://itunes.apple.com/search", params={"term": term, "entity": "song", "limit": 10}, timeout=30)
    r.raise_for_status()
    t_low = title.lower()
    a_low = (artist or "").lower()
    for res in r.json().get("results") or []:
        rt = (res.get("trackName") or "").lower()
        ra = (res.get("artistName") or "").lower()
        if (t_low in rt or rt in t_low) and (not a_low or a_low.split(",")[0].strip() in ra or ra in a_low):
            info = {"full_length": (res.get("trackTimeMillis") or 0) / 1000.0 or None,
                    "genre": res.get("primaryGenreName"),
                    "album": res.get("collectionName"),
                    "year": (res.get("releaseDate") or "")[:4] or None}
            if res.get("artworkUrl100"):
                info["cover_url"] = res["artworkUrl100"].replace("100x100", "600x600")
            return info
    return None


# ---- LRCLIB (lyrics) ----------------------------------------------------------
def lrclib(title: str, artist: str | None, album: str | None, duration: float | None) -> dict | None:
    params = {"track_name": title, "artist_name": artist or ""}
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = int(round(duration))
    r = _http.get("https://lrclib.net/api/get", params=params, timeout=30)
    data = r.json() if r.ok else None
    if not data or not (data.get("plainLyrics") or data.get("syncedLyrics") or data.get("instrumental")):
        r = _http.get("https://lrclib.net/api/search", params={"track_name": title, "artist_name": artist or ""},
                      timeout=30)
        results = r.json() if r.ok else []
        data = None
        for res in results or []:
            if duration and res.get("duration") and abs(res["duration"] - duration) > 3:
                continue
            if res.get("plainLyrics") or res.get("syncedLyrics"):
                data = res
                break
    if not data:
        return None
    if data.get("instrumental"):
        return {"lyrics_plain": "(Instrumental)", "lyrics_synced": None,
                "full_length": data.get("duration")}
    return {"lyrics_plain": data.get("plainLyrics"), "lyrics_synced": data.get("syncedLyrics"),
            "full_length": data.get("duration")}


def download(url: str, dest) -> bool:
    try:
        r = _http.get(url, timeout=30)
        if r.ok and r.content:
            dest.write_bytes(r.content)
            return True
    except Exception as e:
        log.info("cover download failed: %s", e)
    return False
