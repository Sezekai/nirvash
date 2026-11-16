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
  - Delete can skip the songs' videos (and, if you tick it, the same music) in later scans. Undo in Settings → Reset.
  - **Edit audio** (right-click a song or the waveform, or E): drag handles to trim the start or end, change the volume (with an "Even out" button), or re-cut the song from the full sound of the video it came from. Trimming copies the audio untouched; a volume change re-encodes once at high quality in the same format so it plays the same in any player. The original is kept in `Library\Originals`, so **Back to the original** always undoes it.
  - **Even volume:** after each scan, songs much louder or quieter than the rest (more than 2 dB from −14 LUFS) are evened out automatically; the rest aren't touched. Settings → Song volume changes the target or turns it off.
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
| K | Keyboard shortcuts (a popup over any screen) |
| / or Ctrl+F | Search |
| F or F11 | Full screen |
| H | Hide panels (H or Esc brings them back) |
| L | Log |
| M | Show or hide the rail |
| S | Settings |
| Ctrl+Z | Undo the last rename or move |
| Esc | Close what's open |
| Space | Play / pause (or click the playing song's row) |
| - / = | Volume down / up |
| 0 | Mute / unmute |
| ← → PgUp PgDn, Home End | Previous / next, first / last |
| Enter | Play the selected song |
| V / A / E / Delete | Favourite / approve / edit audio / delete (library) |
| R | Mark the video ready (Auto-music) |
| I / O | Editor: trim the clip to start / end at the playhead |
| ← → | Editor: one frame back / on |
| Ctrl+Y | Editor: redo |

- **Editor** (0.4) — open a video (or drop one in), add more to join them, and:
  - **Trim** each clip with handles on its sound wave (I / O at the playhead).
  - **Keep parts, cut parts**: mark a part and **Cut it out** (or **Keep only it**); repeat for more parts and the rest join up. **Split at the playhead** (X) makes pieces to trim or remove.
  - **Join** clips in any order (drag them), each join a **Cut**, **Crossfade** or **Through black** (0.1–3 s; "Use this for every join"). Mixed sizes and frame rates become the biggest size and the highest frame rate, nothing made smaller.
  - **Crop**: the box starts as the whole video, in the finished shape you pick (same as the video, 9:16, 1:1…). Go to a moment and move or resize it: it stays there until you move it again, gliding between positions (Moves take 0–3 s, 0 = a cut). Smaller zooms in; bigger zooms out, even past the edges. Nothing is stretched: where the box doesn't fill the frame, a blurred copy of the picture or a colour fills it. **Follow a subject** keeps the box on something; where it goes wrong, put the box right, mark that part and **Keep this box for the marked part**.
  - **Crop extras**: stop the box at the video's edges (all, top and bottom, or left and right); **Zoom in on the marked part** (the rest stays as it is, gliding in and out); with a part marked, moving the box changes only that part.
  - **Turn, mirror, flip and straighten** each clip.
  - **Blur or pixelate** boxes or ovals, drawn on the picture, for part of the video or all of it. "On the frame" stays in the same spot of the finished (cropped) video; "On the picture" sticks to what's filmed, still, moved by hand, or following a subject.
  - **Sound layers**: the videos' own sound cut with the picture, **kept whole** (on its own line to move and trim), or off; **add a song** from the library (sorted by how well it fits, starting at a good point) or **replace the sound** with one; add any sound file. Each layer can be moved along the timeline, trimmed, faded, looped and made louder or quieter.
  - **Sound**: even out the loudness (to your song library's level), louder or quieter overall, and louder or quieter parts (mark a part, then Make it louder / quieter).
  - **Mark a part** by dragging along the times above the timeline (or Starts here / Ends here): used to fix a stretch of a crop or blur, to follow only that part, and for louder or quieter parts.
  - **Start over…** throws away every edit of the video (Ctrl+Z brings it back while the editor is open).
  - **Overlays** from the overlay library (`Library\Overlays`, with folders): pictures, GIFs and short videos, placed and sized on the picture, see-through, for part of the video.
  - **Colour**: brightness, contrast, colour, shadows, warmth, tint, sharpen.
  - **Export** next to the first video as `<name>_edited.mp4` (never overwriting). Only trims, turns and joins of matching videos are copied without re-encoding; anything that changes the picture is re-encoded once at high quality (x264, or NVENC with "Export faster").
  - Edits are saved as you go and come back when you open the video again (Recent ▾). Ctrl+Z / Ctrl+Y undo and redo.

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
| `E:\Pictures\Nirvash\Library\Originals` | originals of songs you trimmed or whose volume changed (for undoing) |
| `E:\Pictures\Nirvash\Library\Overlays` | the overlay library (pictures, GIFs, short videos, in folders) |
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

Bundled tools: FFmpeg (BtbN GPL build), libmpv (shinchiro), fpcalc (Chromaprint), OpenCV (opencv-python-headless) with the VitTrack model from the OpenCV Model Zoo (Apache 2.0, `assets/models`).
