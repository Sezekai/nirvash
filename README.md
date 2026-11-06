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

**Stage 2** adds the editor (trims, joins, moving crop with tracking, zoom, rotate, blur/pixelate, overlays, colour).

## Install

1. Download `Nirvash.zip` from the repo's **Releases** (the *latest* release is always the newest build).
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
| `%LOCALAPPDATA%\Nirvash` | settings, library database, logs |

## Build from source

```
pip install -r requirements.txt pyinstaller
python build/fetch_tools.py        # FFmpeg, libmpv, fpcalc -> bin/
pyinstaller build/nirvash.spec
```

Bundled tools: FFmpeg (BtbN GPL build), libmpv (shinchiro), fpcalc (Chromaprint).
