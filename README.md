# Nirvash

A simple, lossless-first video tool.

**Stage 1 (this build):**

- **Find music** — scan folders of videos, find the music (AudioSet audio tagger), and copy it into the song library without re-encoding. Videos with talking keep only their music sections, tagged *To check* (as are songs with vocals that might be talking). The same song found in several videos is merged, keeping the best copy.
- **Song library** — listen, favourite, categorise, delete (Recycle Bin). Songs are identified in the background with Shazam (AcoustID as an optional backup), with full length, genre and year from iTunes/MusicBrainz and lyrics from LRCLIB.
  - Play/pause button on every row; listens are tracked (played before, times listened all the way through).
  - Filters that combine, each with its opposite: favourite, approved, played, identified, to check, linked to videos, renamed & tagged, times listened.
  - Folder tree of where songs came from (with subfolders) and a sortable Folder column.
  - Rename files to *Artist - Title* and write title/artist/album/genre/year/cover tags Explorer shows; copies of the same song get their differences (length, BPM…) in the name. Nothing is overwritten and the audio isn't touched.
  - Remove video links: forgets the source videos, renames the file and clears its tags; later scans never relink it.
  - Delete can skip the songs' videos (and the same music) in later scans. Undo in Settings → Reset.
- **Auto-music** — pick a folder of silent videos; Nirvash picks a song for each one by length, pace and mood, then you review, swap and export. The video stream is always copied untouched.

**Around the app (0.2):**

- **Status bar** — one queue for every long job (scans, matching, exports, renames, deletes, downloads, moves). It shows a progress bar, %, time elapsed and time left, warns when nothing has moved for 20 s, and says how it ended. Starting more work while something runs adds it to the queue. Pause/Resume for scans and matching; Stop empties the queue and keeps finished work.
- **Title bar** — pills for running work and song identification, visible on every screen. The hardware chip shows the graphics card in use (with load and memory on NVIDIA).
- **Log drawer** (L) — the log and the **File moves** list, where any rename or move can be undone (Ctrl+Z undoes the last one). The log opens by itself on an error.
- **Look** — themes: backgrounds (Starfield see-through is the default), accent colours and direction, fonts (21 bundled, add your own), text size, corners, density, flat or glow buttons. Save, rename, export and import themes; theme files are shared with the other apps.
- **Settings → Storage** — every place Nirvash keeps things with its size; open, move (copies, checks, switches, then removes the old copy) or clear the cache. Missing drives are reported rather than recreated.
- **Settings → Reset** — settings, library, skipped videos, move log, cache, models or logs, one at a time.
- **Safe mode** — if Nirvash doesn't close properly twice in a row, it starts with playback and the graphics card off and says so, with a button to turn them back on.
- **One window** — starting Nirvash again brings the open window forward. Crash logs are kept per day in the logs folder.

## Keys

| Key | Does |
| --- | --- |
| K | List of keys |
| / or Ctrl+F | Search |
| F or F11 | Full screen |
| H | Hide panels (H or Esc brings them back) |
| L | Log |
| M | Show or hide the rail |
| S | Settings |
| Ctrl+Z | Undo the last rename or move |
| Esc | Close what's open |
| Space | Play / pause |
| ← → PgUp PgDn, Home End | Previous / next, first / last |
| Enter | Play the selected song |
| V / A / Delete | Favourite / approve / delete (library) |
| R | Mark the video ready (Auto-music) |

**Stage 2** adds the editor (trims, joins, moving crop with tracking, zoom, rotate, blur/pixelate, overlays, colour).

## Install

1. Download `Nirvash.zip` from the newest numbered release: https://github.com/Sezekai/nirvash/releases/latest/download/Nirvash.zip (older versions stay under **Releases**).
2. Extract so the program sits at `E:\Pictures\Nirvash\App\Nirvash.exe`.
3. Run `Nirvash.exe`. On first start it offers to download the audio tagger model (~350 MB) into `E:\Pictures\Nirvash\Models`.
   CLIP is read from Collection Sorter's copy (`%LOCALAPPDATA%\Collection Sorter\models\clip`); if that's ever gone, Nirvash offers to download its own.

Updating = replace the `App` folder. Nothing else is touched.

## Where things live

| Folder | Holds |
| --- | --- |
| `E:\Pictures\Nirvash\App` | the program (replaced by updates) |
| `E:\Pictures\Nirvash\Library\Songs` | extracted songs |
| `E:\Pictures\Nirvash\Library\Overlays` | overlays (Stage 2) |
| `E:\Pictures\Nirvash\Models` | AI models |
| `E:\Pictures\Nirvash\Cache` | previews, waveforms, cover art (safe to delete) |
| `%LOCALAPPDATA%\Nirvash` | settings, library database, themes, added fonts, logs (can be moved in Settings → Storage) |

## Build from source

```
pip install -r requirements.txt pyinstaller
python build/fetch_tools.py        # FFmpeg, libmpv, fpcalc -> bin/
pyinstaller build/nirvash.spec
```

Tests: `python -m pytest tests` (generated videos and songs only), `python tests/screenshots.py <folder>` for screenshots at 1100, 1440 and 1920 wide, and `tests/stress_library.py` / `tests/playback_stress.py`.

Docs: PRODUCT.md (purpose and what differs from the shared standards), DESIGN.md and STANDARDS.md (Nirvash's copies).

Bundled tools: FFmpeg (BtbN GPL build), libmpv (shinchiro), fpcalc (Chromaprint).
