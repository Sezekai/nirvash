# Nirvash: product

## Who it's for

One person (the owner) with a large local collection of short videos and clips on Windows, on a gaming laptop with an
NVIDIA graphics card. Everything runs on that PC; nothing about the collection is uploaded anywhere except the short
audio samples sent to the song lookup services the owner has turned on.

## Purpose

- Find the music inside videos and keep it as a song library: listen, check, tidy, tag and categorise.
- Give silent videos a fitting song, losslessly: the video stream is always copied untouched.
- Light edits (the Editor): trims, joins, crop and moving crop / zoom with subject tracking, turning, blur and
  pixelate, overlays, colour. Lossless where nothing changes the picture; one high-quality re-encode otherwise.

## Constraints

- Lossless first: audio is copied, not re-encoded, unless mixing needs it (then AAC 320k, and the screen says so).
- Never overwrite or permanently delete: deletes go to the Recycle Bin; renames and moves are logged and can be undone.
- Works offline apart from the lookups and the one-time model downloads, which are always asked about first.
- The repo never holds the owner's library, file names or database; tests use generated videos and songs.

## Never in it

- Accounts, sync, telemetry or ads.
- Uploading whole videos anywhere.
- Writing into the video files themselves.

## Decided for Nirvash (differs from the shared standards)

| Standard | Nirvash | Why |
| --- | --- | --- |
| DESIGN.md: chips and numbers use the accent or state colours only | Category chips and counts keep their own fixed colours (teal, violet, pink, amber, blue, green) | The owner likes the colourful library; categories are told apart at a glance |
| DESIGN.md: plain backgrounds | Default theme "Starfield, see-through": a painted star field behind see-through panels (a `scene` block in the theme file). Other apps read the theme file and ignore `scene` | Owner's choice; every preset still keeps text at 4.5:1 or more |
| STANDARDS.md: count badges on rail icons | Not used | Owner's choice; the title-bar pills show running work instead |
| STANDARDS.md: tick boxes for picking items | Not used; rows are selected the usual way (click, Shift, Ctrl) | Owner's choice |
| STANDARDS.md: one portable .exe | A zipped folder unpacking to `Nirvash\App` | libmpv, FFmpeg and ONNX Runtime are large and start faster unpacked |
| STANDARDS.md: favourite key | Favourite is **V** (not F) | F is full screen everywhere in the suite |
| Keys | Auto-music uses **R** for "ready"; the library uses **E** to edit a song's audio | Their own steps |
| Keys | Volume is **-** and **=**, mute is **0** (not ↑ ↓) | ↑ ↓ move through the song list; the owner chose - and =, 0 sits beside them |
| Keys | In the editor ← → step one frame, I / O trim, Ctrl+Y redo, and Ctrl+Z undoes edits (elsewhere it undoes the last move) | An editor needs frame steps and its own undo |
| Lossless first | A volume change re-encodes the song once (same format, high bitrate), always from the kept original | The owner wants volume changes to carry over to other players; trims alone stay lossless |

## Release

Every version gets a numbered release (`v0.2.0` …) built and tested on GitHub Actions. The newest is always at
https://github.com/Sezekai/nirvash/releases/latest/download/Nirvash.zip
