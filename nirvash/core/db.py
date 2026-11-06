"""The library database (SQLite in the data folder)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from . import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS songs (
    id INTEGER PRIMARY KEY,
    file_path TEXT NOT NULL UNIQUE,
    codec TEXT, ext TEXT, sample_rate INTEGER, channels INTEGER, bitrate INTEGER,
    duration REAL NOT NULL DEFAULT 0,
    title TEXT, artist TEXT, album TEXT, genre TEXT, year TEXT,
    cover_url TEXT, shazam_key TEXT, isrc TEXT, mbid TEXT,
    full_length REAL, clip_offset REAL,
    lyrics_plain TEXT, lyrics_synced TEXT,
    bpm REAL, energy REAL, pace REAL, mood TEXT,
    clap_emb BLOB, fingerprint BLOB,
    partial INTEGER NOT NULL DEFAULT 0,
    checked INTEGER NOT NULL DEFAULT 0,
    favourite INTEGER NOT NULL DEFAULT 0,
    id_status TEXT NOT NULL DEFAULT 'pending',
    id_source TEXT, manual_info INTEGER NOT NULL DEFAULT 0,
    added_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS song_sources (
    id INTEGER PRIMARY KEY,
    song_id INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
    video_path TEXT NOT NULL, start REAL NOT NULL DEFAULT 0, end REAL NOT NULL DEFAULT 0,
    UNIQUE(song_id, video_path, start)
);
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, color TEXT
);
CREATE TABLE IF NOT EXISTS song_categories (
    song_id INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    PRIMARY KEY(song_id, category_id)
);
CREATE TABLE IF NOT EXISTS folder_rules (
    folder TEXT PRIMARY KEY,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS scanned_videos (
    path TEXT PRIMARY KEY, size INTEGER, mtime REAL, result TEXT, detail TEXT, scanned_at REAL
);
CREATE TABLE IF NOT EXISTS video_features (
    path TEXT PRIMARY KEY, size INTEGER, mtime REAL, data TEXT
);
CREATE TABLE IF NOT EXISTS song_usage (
    id INTEGER PRIMARY KEY, song_id INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
    video_path TEXT NOT NULL, used_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS ignored_videos (path TEXT PRIMARY KEY, added_at REAL);
CREATE TABLE IF NOT EXISTS ignored_songs (
    id INTEGER PRIMARY KEY, fingerprint BLOB NOT NULL, duration REAL, added_at REAL
);
"""

# Columns added after the first release: (name, definition). Added to older databases on open.
MIGRATIONS = [
    ("played", "INTEGER NOT NULL DEFAULT 0"),        # started playing at least once
    ("listen_count", "INTEGER NOT NULL DEFAULT 0"),  # listened all the way through
    ("last_played", "REAL"),
    ("approved", "INTEGER NOT NULL DEFAULT 0"),
    ("unlinked", "INTEGER NOT NULL DEFAULT 0"),      # video links removed - never re-linked by scans
    ("tagged", "INTEGER NOT NULL DEFAULT 0"),        # file renamed and tagged with the song info
]

SONG_FIELDS = (
    "file_path codec ext sample_rate channels bitrate duration title artist album genre year "
    "cover_url shazam_key isrc mbid full_length clip_offset lyrics_plain lyrics_synced bpm energy "
    "pace mood partial checked favourite id_status id_source manual_info added_at "
    "played listen_count last_played approved unlinked tagged"
).split()


def emb_to_blob(v: np.ndarray | None) -> bytes | None:
    return None if v is None else np.asarray(v, np.float32).tobytes()


def blob_to_emb(b: bytes | None) -> np.ndarray | None:
    return None if not b else np.frombuffer(b, np.float32).copy()


class DB:
    def __init__(self, path: Path | None = None):
        self.path = path or (paths.data_dir() / "nirvash.db")
        self._local = threading.local()
        self._write_lock = threading.RLock()
        with self.conn() as c:
            c.executescript(SCHEMA)
            have = {r[1] for r in c.execute("PRAGMA table_info(songs)")}
            for name, decl in MIGRATIONS:
                if name not in have:
                    c.execute(f"ALTER TABLE songs ADD COLUMN {name} {decl}")

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            self._local.c = c
        return c

    def q(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn().execute(sql, tuple(args)).fetchall()

    def one(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn().execute(sql, tuple(args)).fetchone()

    def x(self, sql: str, args: Iterable[Any] = ()) -> int:
        with self._write_lock:
            c = self.conn()
            cur = c.execute(sql, tuple(args))
            c.commit()
            return cur.lastrowid or cur.rowcount

    # ---- kv ---------------------------------------------------------------
    def kv_get(self, k: str, default: Any = None) -> Any:
        r = self.one("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(r["v"]) if r else default

    def kv_set(self, k: str, v: Any) -> None:
        self.x("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))

    # ---- songs ------------------------------------------------------------
    def add_song(self, **f: Any) -> int:
        f.setdefault("added_at", time.time())
        emb = f.pop("clap_emb", None)
        fp = f.pop("fingerprint", None)
        cols = [k for k in f if k in SONG_FIELDS]
        sql = f"INSERT INTO songs({','.join(cols + ['clap_emb', 'fingerprint'])}) VALUES({','.join('?' * (len(cols) + 2))})"
        fpb = None if fp is None else np.asarray(fp, np.uint32).tobytes()
        return self.x(sql, [f[k] for k in cols] + [emb_to_blob(emb), fpb])

    def update_song(self, song_id: int, **f: Any) -> None:
        sets, vals = [], []
        for k, v in f.items():
            if k == "clap_emb":
                v = emb_to_blob(v)
            elif k == "fingerprint":
                v = None if v is None else np.asarray(v, np.uint32).tobytes()
            elif k not in SONG_FIELDS:
                continue
            sets.append(f"{k}=?")
            vals.append(v)
        if sets:
            self.x(f"UPDATE songs SET {','.join(sets)} WHERE id=?", vals + [song_id])

    def song(self, song_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM songs WHERE id=?", (song_id,))

    def songs(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM songs ORDER BY added_at DESC")

    def delete_song(self, song_id: int) -> None:
        self.x("DELETE FROM songs WHERE id=?", (song_id,))

    def fingerprints(self) -> list[tuple[int, np.ndarray, float]]:
        out = []
        for r in self.q("SELECT id, fingerprint, duration FROM songs WHERE fingerprint IS NOT NULL"):
            out.append((r["id"], np.frombuffer(r["fingerprint"], np.uint32), r["duration"]))
        return out

    def add_source(self, song_id: int, video_path: str, start: float, end: float) -> None:
        self.x("INSERT OR IGNORE INTO song_sources(song_id, video_path, start, end) VALUES(?,?,?,?)",
               (song_id, video_path, start, end))

    def sources(self, song_id: int) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM song_sources WHERE song_id=? ORDER BY id", (song_id,))

    def move_sources(self, from_id: int, to_id: int) -> None:
        self.x("UPDATE OR IGNORE song_sources SET song_id=? WHERE song_id=?", (to_id, from_id))
        self.x("INSERT OR IGNORE INTO song_categories(song_id, category_id) "
               "SELECT ?, category_id FROM song_categories WHERE song_id=?", (to_id, from_id))

    def remove_sources(self, song_id: int) -> None:
        self.x("DELETE FROM song_sources WHERE song_id=?", (song_id,))

    def source_videos(self) -> dict[int, list[str]]:
        out: dict[int, list[str]] = {}
        for r in self.q("SELECT song_id, video_path FROM song_sources ORDER BY id"):
            out.setdefault(r[0], []).append(r[1])
        return out

    # ---- listening ----------------------------------------------------------
    def mark_played(self, song_id: int) -> None:
        self.x("UPDATE songs SET played=1, last_played=? WHERE id=?", (time.time(), song_id))

    def add_listen(self, song_id: int) -> None:
        self.x("UPDATE songs SET played=1, listen_count=listen_count+1, last_played=? WHERE id=?",
               (time.time(), song_id))

    # ---- things the user deleted: never brought back by a scan ---------------
    def ignore_video(self, path: str) -> None:
        self.x("INSERT OR IGNORE INTO ignored_videos(path, added_at) VALUES(?,?)", (path, time.time()))

    def ignored_videos(self) -> set[str]:
        return {r[0].lower() for r in self.q("SELECT path FROM ignored_videos")}

    def ignore_song(self, fp: bytes, duration: float) -> None:
        self.x("INSERT INTO ignored_songs(fingerprint, duration, added_at) VALUES(?,?,?)", (fp, duration, time.time()))

    def ignored_songs(self) -> list[tuple[int, np.ndarray]]:
        return [(r["id"], np.frombuffer(r["fingerprint"], np.uint32)) for r in self.q("SELECT * FROM ignored_songs")]

    def ignore_counts(self) -> tuple[int, int]:
        return (int(self.one("SELECT COUNT(*) FROM ignored_videos")[0]),
                int(self.one("SELECT COUNT(*) FROM ignored_songs")[0]))

    def clear_ignored(self) -> None:
        with self._write_lock:
            c = self.conn()
            c.execute("DELETE FROM ignored_videos")
            c.execute("DELETE FROM ignored_songs")
            c.commit()

    # ---- categories -------------------------------------------------------
    def categories(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM categories ORDER BY name COLLATE NOCASE")

    def add_category(self, name: str, color: str) -> int:
        return self.x("INSERT INTO categories(name, color) VALUES(?,?)", (name, color))

    def rename_category(self, cid: int, name: str) -> None:
        self.x("UPDATE categories SET name=? WHERE id=?", (name, cid))

    def recolor_category(self, cid: int, color: str) -> None:
        self.x("UPDATE categories SET color=? WHERE id=?", (color, cid))

    def delete_category(self, cid: int) -> None:
        self.x("DELETE FROM categories WHERE id=?", (cid,))

    def song_category_ids(self, song_id: int) -> set[int]:
        return {r[0] for r in self.q("SELECT category_id FROM song_categories WHERE song_id=?", (song_id,))}

    def all_song_categories(self) -> dict[int, set[int]]:
        out: dict[int, set[int]] = {}
        for r in self.q("SELECT song_id, category_id FROM song_categories"):
            out.setdefault(r[0], set()).add(r[1])
        return out

    def set_song_category(self, song_id: int, cid: int, on: bool) -> None:
        if on:
            self.x("INSERT OR IGNORE INTO song_categories(song_id, category_id) VALUES(?,?)", (song_id, cid))
        else:
            self.x("DELETE FROM song_categories WHERE song_id=? AND category_id=?", (song_id, cid))

    def folder_rules(self) -> dict[str, int]:
        return {r["folder"]: r["category_id"] for r in self.q("SELECT * FROM folder_rules")}

    def set_folder_rule(self, folder: str, cid: int | None) -> None:
        if cid is None:
            self.x("DELETE FROM folder_rules WHERE folder=?", (folder,))
        else:
            self.x("INSERT INTO folder_rules(folder, category_id) VALUES(?,?) "
                   "ON CONFLICT(folder) DO UPDATE SET category_id=excluded.category_id", (folder, cid))

    # ---- scans / features -------------------------------------------------
    def scanned(self, path: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM scanned_videos WHERE path=?", (path,))

    def mark_scanned(self, path: str, size: int, mtime: float, result: str, detail: str = "") -> None:
        self.x("INSERT INTO scanned_videos(path,size,mtime,result,detail,scanned_at) VALUES(?,?,?,?,?,?) "
               "ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime, result=excluded.result, "
               "detail=excluded.detail, scanned_at=excluded.scanned_at",
               (path, size, mtime, result, detail, time.time()))

    def video_features(self, path: str, size: int, mtime: float) -> dict | None:
        r = self.one("SELECT * FROM video_features WHERE path=?", (path,))
        if r and r["size"] == size and abs((r["mtime"] or 0) - mtime) < 1e-3:
            return json.loads(r["data"])
        return None

    def save_video_features(self, path: str, size: int, mtime: float, data: dict) -> None:
        self.x("INSERT INTO video_features(path,size,mtime,data) VALUES(?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
               "size=excluded.size, mtime=excluded.mtime, data=excluded.data", (path, size, mtime, json.dumps(data)))

    def record_usage(self, song_id: int, video_path: str) -> None:
        self.x("INSERT INTO song_usage(song_id, video_path, used_at) VALUES(?,?,?)", (song_id, video_path, time.time()))

    def usage_counts(self) -> dict[int, int]:
        return {r[0]: r[1] for r in self.q("SELECT song_id, COUNT(*) FROM song_usage GROUP BY song_id")}

    # ---- resets -----------------------------------------------------------
    def reset_library(self) -> None:
        with self._write_lock:
            c = self.conn()
            for t in ("song_usage", "song_categories", "song_sources", "songs", "scanned_videos", "video_features",
                      "ignored_videos", "ignored_songs"):
                c.execute(f"DELETE FROM {t}")
            c.commit()


_db: DB | None = None


def get_db() -> DB:
    global _db
    if _db is None:
        _db = DB()
    return _db
