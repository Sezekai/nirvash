"""What an edit is: the clips (one video, or several joined), and what's done to them.

Times are seconds on the output timeline (the clips one after another, after their trims). Positions are fractions
of the frame (0..1) so they don't depend on the preview size:
- blur boxes and the crop box are fractions of the *canvas* (the joined video before cropping);
- overlays are fractions of the *output* frame (after cropping), so a logo stays put when the crop moves.

Anything that moves (the crop box, a blur box) has keys: (t, x, y, w, h). One key = it stays still. More keys
= it moves (and the crop box zooms when its size changes) in straight lines between them. Tracking writes many.
"""
from __future__ import annotations

import bisect
import copy
import json
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

ASPECTS = {"free": None, "source": "source", "16:9": 16 / 9, "9:16": 9 / 16, "1:1": 1.0, "4:5": 0.8, "4:3": 4 / 3,
           "3:4": 0.75, "21:9": 21 / 9}


def new_id() -> str:
    return uuid.uuid4().hex[:8]


@dataclass
class Clip:
    path: str
    start: float = 0.0              # trim: where it starts in the source
    end: float = 0.0                # and where it ends (0 = the end of the source)
    duration: float = 0.0           # the source's length (filled in when added)
    width: int = 0                  # as it plays (rotation metadata already applied)
    height: int = 0
    fps: float = 30.0
    has_audio: bool = True
    rotate: int = 0                 # 0 / 90 / 180 / 270, clockwise
    flip_h: bool = False
    flip_v: bool = False
    angle: float = 0.0              # small straightening, degrees; the picture is zoomed so no corners show
    mute: bool = False
    transition: str = "cut"         # how it joins on from the clip before: cut | dissolve | black
    transition_s: float = 0.5

    @property
    def out(self) -> float:
        return self.end if self.end > 0 else self.duration

    @property
    def length(self) -> float:
        return max(0.0, self.out - self.start)

    def shown_size(self) -> tuple[int, int]:
        """Width and height after the clip's own rotation."""
        return (self.height, self.width) if self.rotate in (90, 270) else (self.width, self.height)


Key = list  # [t, x, y, w, h, mode]: mode "h" = hold (set by hand), "l" = straight line (tracking writes these)


def _mode(k: Key) -> str:
    return k[5] if len(k) > 5 else "l"        # keys from 0.4.0 had no mode and glided in straight lines


def _ease(f: float) -> float:
    f = max(0.0, min(1.0, f))
    return f * f * (3 - 2 * f)


def interp(keys: list[Key], t: float, transition: float = 0.0) -> tuple[float, float, float, float] | None:
    """Where a box is at time t.

    A key set by hand ("h") holds the box where it was put until the next key; the box then moves to the next key
    over `transition` seconds, arriving exactly at that key's time (eased, so it starts and stops gently; 0 = a cut).
    Tracked keys ("l") are followed in straight lines from one to the next."""
    if not keys:
        return None
    if len(keys) == 1 or t <= keys[0][0]:
        return tuple(keys[0][1:5])
    if t >= keys[-1][0]:
        return tuple(keys[-1][1:5])
    ts = [k[0] for k in keys]
    i = bisect.bisect_right(ts, t)
    a, b = keys[i - 1], keys[i]
    span = b[0] - a[0]
    if _mode(b) == "l":
        f = 0.0 if span <= 0 else (t - a[0]) / span
    else:
        tr = min(transition, span)
        if tr <= 1e-6:
            f = 0.0
        else:
            f = _ease((t - (b[0] - tr)) / tr)
    return tuple(a[j] + (b[j] - a[j]) * f for j in range(1, 5))


def set_key(keys: list[Key], t: float, box: tuple[float, float, float, float], snap: float = 0.02) -> None:
    """Put the box here from time t on (until the next key). At the very start it changes the starting box."""
    box = [float(v) for v in box]
    if not keys:
        keys.append([0.0] + box + ["h"])
        return
    if t <= snap:
        t = 0.0
    for k in keys:
        if abs(k[0] - t) <= snap:
            k[1:] = box + ["h"]
            return
    bisect.insort(keys, [t] + box + ["h"], key=lambda k: k[0])


def apply_section(keys: list[Key], a: float, b: float, box, transition: float, end: float) -> None:
    """Keep `box` from a to b (fixing a part tracking got wrong), then go back to what was there before."""
    old = [list(k) for k in keys]
    after_t = min(end, b + max(transition, 0.04))
    back = interp(old, after_t, transition) if after_t < end - 1e-3 else None
    new = [k for k in old if k[0] < a - 1e-6]
    new.append([a] + list(box) + ["h"])
    new.append([b] + list(box) + ["h"])
    if back is not None:
        new.append([after_t] + list(back) + ["h"])
        new += [k for k in old if k[0] > after_t + 1e-6]
    keys[:] = new or [[0.0] + list(box) + ["h"]]


LOCKS = {"off": "Free", "all": "Inside the video", "tb": "Top and bottom", "lr": "Left and right"}


@dataclass
class Crop:
    enabled: bool = False
    lock: str = "off"               # keep the box inside the video's edges: off | all | tb (top/bottom) | lr
    aspect: str = "source"          # the finished video's shape ("source" = the same as the video)
    keep_shape: bool = True         # resizing the box keeps that shape (off: any shape, the rest is filled)
    fill: str = "blur"              # what fills the frame where the box doesn't: "blur" (a blurred copy) or "colour"
    fill_colour: str = "#000000"
    transition: float = 0.5         # seconds a move between two positions takes (0 = a cut)
    moving: bool = False            # unused since 0.5 (kept so older saved edits load)
    keys: list = field(default_factory=lambda: [[0.0, 0.0, 0.0, 1.0, 1.0, "h"]])


@dataclass
class Blur:
    id: str = field(default_factory=new_id)
    space: str = "output"           # "output": a spot of the finished (cropped) frame; "video": on the picture itself
                                    # (moves with what's filmed - following a subject uses this)
    kind: str = "blur"              # blur | pixelate
    strength: int = 50              # 1..100
    shape: str = "rect"             # rect | ellipse
    start: float = 0.0
    end: float = 0.0                # 0 = to the end
    moving: bool = False            # unused since 0.5
    keys: list = field(default_factory=lambda: [[0.0, 0.4, 0.4, 0.2, 0.2, "h"]])


@dataclass
class Overlay:
    id: str = field(default_factory=new_id)
    path: str = ""                  # a file in the overlay library (or anywhere)
    start: float = 0.0
    end: float = 0.0                # 0 = to the end
    x: float = 0.05                 # top left, fraction of the output frame
    y: float = 0.05
    w: float = 0.25                 # width, fraction of the output width (height follows the image)
    opacity: float = 1.0


@dataclass
class Section:
    id: str = field(default_factory=new_id)
    start: float = 0.0
    end: float = 0.0
    gain_db: float = 6.0


@dataclass
class Layer:
    """A sound on its own line of the timeline: a song from the library, a video's own sound kept whole, or any
    sound file. It starts `at` seconds into the edit and plays src_start..src_end of its file."""
    id: str = field(default_factory=new_id)
    path: str = ""
    kind: str = "song"              # song | original | file
    song_id: int | None = None
    name: str = ""
    duration: float = 0.0           # the file's length
    src_start: float = 0.0
    src_end: float = 0.0            # 0 = to the end of the file
    at: float = 0.0
    gain_db: float = 0.0
    fade_in: float = 0.5
    fade_out: float = 2.0
    loop: bool = False              # play again from its start until the edit ends

    @property
    def length(self) -> float:
        end = self.src_end if self.src_end > 0 else self.duration
        return max(0.0, end - self.src_start)


@dataclass
class Sound:
    clip_sound: bool = True         # the videos' own sound, cut along with the picture
    layers: list = field(default_factory=list)     # dicts of Layer
    normalize: bool = False         # bring the whole edit to the library's loudness (Settings > Song volume)
    gain_db: float = 0.0            # louder / quieter, the whole edit
    sections: list = field(default_factory=list)   # louder / quieter parts (dicts of Section)
    measured: list = field(default_factory=list)   # [signature, LUFS, true peak] of the edit's sound

    def is_neutral(self) -> bool:
        return (not self.normalize and abs(self.gain_db) < 0.05 and not self.sections and self.clip_sound
                and not self.layers)


@dataclass
class Colour:
    brightness: float = 0.0         # -1 .. 1
    contrast: float = 1.0           # 0.5 .. 2
    saturation: float = 1.0         # 0 .. 2.5
    gamma: float = 1.0              # 0.5 .. 2
    warmth: float = 0.0             # -1 (cool) .. 1 (warm)
    tint: float = 0.0               # -1 (green) .. 1 (magenta)
    sharpen: float = 0.0            # 0 .. 1

    def is_neutral(self) -> bool:
        return self == Colour()


@dataclass
class Output:
    quality: str = "high"           # high | best | smaller
    gpu: bool = False               # NVENC (faster)
    lossless_when_possible: bool = True
    name: str = ""                  # empty = "<first clip>_edited.mp4"


@dataclass
class Project:
    clips: list[Clip] = field(default_factory=list)
    crop: Crop = field(default_factory=Crop)
    blurs: list[Blur] = field(default_factory=list)
    overlays: list[Overlay] = field(default_factory=list)
    colour: Colour = field(default_factory=Colour)
    sound: Sound = field(default_factory=Sound)
    output: Output = field(default_factory=Output)
    version: int = 2

    # ---- timeline -----------------------------------------------------------------------
    def overlap(self, i: int) -> float:
        """How long clip i overlaps the one before it (a crossfade); 0 for cuts and fades through black."""
        if i <= 0 or i >= len(self.clips):
            return 0.0
        c = self.clips[i]
        if c.transition != "dissolve":
            return 0.0
        return max(0.0, min(c.transition_s, self.clips[i - 1].length / 2, c.length / 2))

    def offsets(self) -> list[float]:
        out, acc = [], 0.0
        for i, c in enumerate(self.clips):
            acc -= self.overlap(i)
            out.append(acc)
            acc += c.length
        return out

    @property
    def duration(self) -> float:
        if not self.clips:
            return 0.0
        return self.offsets()[-1] + self.clips[-1].length

    def clip_at(self, t: float) -> tuple[int, float]:
        """Which clip plays at output time t (the later one during a crossfade), and the time in its file."""
        offs = self.offsets()
        i = 0
        for j, off in enumerate(offs):
            if t >= off - 1e-9:
                i = j
        c = self.clips[i]
        return i, c.start + max(0.0, min(c.length, t - offs[i]))

    def clip_offset(self, i: int) -> float:
        return self.offsets()[i] if 0 <= i < len(self.clips) else self.duration

    def fade(self, i: int, t: float) -> float:
        """Brightness of clip i at output time t for fades through black (1 = untouched)."""
        offs = self.offsets()
        c = self.clips[i]
        f = 1.0
        if i > 0 and c.transition == "black":
            h = max(0.01, min(c.transition_s / 2, c.length / 2))
            f = min(f, (t - offs[i]) / h)
        if i + 1 < len(self.clips) and self.clips[i + 1].transition == "black":
            n = self.clips[i + 1]
            h = max(0.01, min(n.transition_s / 2, c.length / 2))
            f = min(f, (offs[i] + c.length - t) / h)
        return max(0.0, min(1.0, f))

    def split(self, t: float) -> bool:
        """Cut the clip under t into two at t (both keep the same file). False if t is at a clip's edge."""
        if not self.clips:
            return False
        i, src = self.clip_at(t)
        c = self.clips[i]
        if src - c.start < 0.05 or c.out - src < 0.05:
            return False
        a, b = copy.deepcopy(c), copy.deepcopy(c)
        a.end = src
        b.start, b.transition = src, "cut"
        self.clips[i:i + 1] = [a, b]
        return True

    def cut_out(self, a: float, b: float) -> None:
        """Remove output a..b from the clips (the parts either side are joined)."""
        a, b = max(0.0, a), min(self.duration, b)
        if b - a < 0.02:
            return
        self.split(b)
        self.split(a)
        offs = self.offsets()
        keep = [c for c, off in zip(self.clips, offs) if not (off >= a - 0.06 and off + c.length <= b + 0.06)]
        if keep:
            self.clips = keep

    def keep_only(self, a: float, b: float) -> None:
        d = self.duration
        if b < d - 0.02:
            self.cut_out(b, d)
        if a > 0.02:
            self.cut_out(0.0, a)

    # ---- frame sizes ------------------------------------------------------------------------
    def canvas(self) -> tuple[int, int, float]:
        """The joined video's size and frame rate: the biggest clip's, so nothing is made smaller (others are fitted
        inside it with bars where the shape differs) and the highest frame rate."""
        if not self.clips:
            return 1280, 720, 30.0
        best = max(self.clips, key=lambda c: c.shown_size()[0] * c.shown_size()[1])
        w, h = best.shown_size()
        fps = max(c.fps or 30.0 for c in self.clips)
        return _even(w), _even(h), min(120.0, round(fps, 3))

    def aspect_value(self) -> float | None:
        """The finished video's shape (width / height)."""
        a = ASPECTS.get(self.crop.aspect)
        if a == "source" or a is None:
            w, h, _ = self.canvas()
            return w / h
        return a

    def output_size(self) -> tuple[int, int]:
        """The finished video's size: the biggest frame of the chosen shape that fits in the video (no crop: the
        video's size). Every crop box is fitted into this, so zooming never changes it."""
        W, H, _ = self.canvas()
        if not self.crop.enabled:
            return W, H
        a = self.aspect_value()
        if W / H > a:
            return _even(H * a), H
        return W, _even(W / a)

    def clamp_crop(self, box, keep_shape: bool | None = None):
        """Keep a crop box inside the video's locked edges (it slides up to the edge; if it's bigger than the video
        that way, it's made smaller, keeping its shape when the shape is kept)."""
        lock = self.crop.lock
        if lock == "off":
            return tuple(box)
        x, y, w, h = box
        keep = self.crop.keep_shape if keep_shape is None else keep_shape
        if lock in ("all", "lr") and w > 1:
            k = 1 / w
            w, h = 1.0, (h * k if keep else h)
        if lock in ("all", "tb") and h > 1:
            k = 1 / h
            h, w = 1.0, (w * k if keep else w)
        if lock in ("all", "lr"):
            x = min(1 - w, max(0.0, x))
        if lock in ("all", "tb"):
            y = min(1 - h, max(0.0, y))
        return x, y, w, h

    def crop_box(self, t: float) -> tuple[float, float, float, float] | None:
        if not self.crop.enabled:
            return None
        return interp(self.crop.keys, t, self.crop.transition)

    def fit(self, t: float) -> tuple[float, float, float, float, float] | None:
        """How the crop box at t lands in the finished frame: scale (output px per canvas px), the box's top left
        in canvas px, and where its picture starts in the output (bars around it when its shape differs)."""
        box = self.crop_box(t)
        if box is None:
            return None
        W, H, _ = self.canvas()
        OW, OH = self.output_size()
        bx, by, bw, bh = box[0] * W, box[1] * H, max(1e-3, box[2] * W), max(1e-3, box[3] * H)
        s = min(OW / bw, OH / bh)
        return s, bx, by, (OW - bw * s) / 2, (OH - bh * s) / 2

    def canvas_to_output(self, box, t: float):
        """A box on the video picture -> where it is in the finished frame (fractions)."""
        f = self.fit(t)
        if f is None:
            return box
        s, bx, by, ox, oy = f
        W, H, _ = self.canvas()
        OW, OH = self.output_size()
        x, y, w, h = box
        return ((x * W - bx) * s + ox) / OW, ((y * H - by) * s + oy) / OH, w * W * s / OW, h * H * s / OH

    def output_to_canvas(self, box, t: float):
        f = self.fit(t)
        if f is None:
            return box
        s, bx, by, ox, oy = f
        W, H, _ = self.canvas()
        OW, OH = self.output_size()
        x, y, w, h = box
        return ((x * OW - ox) / s + bx) / W, ((y * OH - oy) / s + by) / H, w * OW / s / W, h * OH / s / H

    def blur_box(self, b: Blur, t: float, space: str):
        """A blur box at t, in "output" or "video" (canvas) fractions."""
        box = interp(b.keys, t, self.crop.transition)
        if box is None or b.space == space:
            return box
        return self.canvas_to_output(box, t) if space == "output" else self.output_to_canvas(box, t)

    def blur_end(self, b: Blur) -> float:
        return b.end if b.end > 0 else self.duration

    def overlay_end(self, o: Overlay) -> float:
        return o.end if o.end > 0 else self.duration

    # ---- what kind of export it needs ---------------------------------------------------------------
    def sound_signature(self) -> str:
        return json.dumps([[[c.path, round(c.start, 3), round(c.out, 3), c.mute, c.transition, c.transition_s]
                            for c in self.clips], self.sound.clip_sound, self.sound.layers])

    def layer_objs(self) -> list[Layer]:
        return [Layer(**_known(Layer, x)) for x in self.sound.layers]

    def layer_end(self, l: Layer) -> float:
        return self.duration if l.loop else min(self.duration, l.at + l.length)

    def frame_effects(self) -> bool:
        """Anything that changes the picture itself (so the video must be re-encoded)."""
        return (self.crop.enabled or bool(self.blurs) or bool(self.overlays) or not self.colour.is_neutral()
                or any(abs(c.angle) > 0.01 for c in self.clips)
                or any(c.transition != "cut" for c in self.clips[1:]))

    def is_empty_edit(self) -> bool:
        return (not self.frame_effects() and len(self.clips) <= 1 and self.sound.is_neutral()
                and all(c.start <= 0.01 and (c.end <= 0 or c.end >= c.duration - 0.01) and not c.rotate
                        and not c.flip_h and not c.flip_v and not c.mute for c in self.clips))

    # ---- saving --------------------------------------------------------------------------------------
    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(s: str) -> "Project":
        d = json.loads(s)
        p = Project()
        p.clips = [Clip(**_known(Clip, c)) for c in d.get("clips", [])]
        p.crop = Crop(**_known(Crop, d.get("crop", {})))
        p.blurs = [Blur(**_known(Blur, b)) for b in d.get("blurs", [])]
        p.overlays = [Overlay(**_known(Overlay, o)) for o in d.get("overlays", [])]
        p.colour = Colour(**_known(Colour, d.get("colour", {})))
        p.sound = Sound(**_known(Sound, d.get("sound", {})))
        p.sound.layers = [asdict(Layer(**_known(Layer, x))) for x in p.sound.layers]
        if d.get("version", 1) < 2 and p.crop.enabled:
            # 0.4.0 crops: the first box set the output size; keep that shape as the output shape
            W, H, _ = p.canvas()
            k0 = p.crop.keys[0] if p.crop.keys else [0, 0, 0, 1, 1]
            p.crop.aspect = _closest_aspect(k0[3] * W / max(1e-6, k0[4] * H))
        p.output = Output(**_known(Output, d.get("output", {})))
        return p

    def copy(self) -> "Project":
        return copy.deepcopy(self)

    def default_output_path(self) -> Path:
        from ..core.settings import get_settings
        first = Path(self.clips[0].path)
        suffix = get_settings().get("export.suffix", "_edited") or "_edited"
        name = self.output.name.strip() or f"{first.stem}{suffix}.mp4"
        if not name.lower().endswith((".mp4", ".mkv", ".mov")):
            name += ".mp4"
        return first.with_name(name)


def _closest_aspect(a: float) -> str:
    best = min((k for k, v in ASPECTS.items() if isinstance(v, float)), key=lambda k: abs(ASPECTS[k] - a))
    return best if abs(ASPECTS[best] - a) < 0.03 else "source"


def _known(cls, d: dict) -> dict:
    names = set(cls.__dataclass_fields__)
    return {k: v for k, v in (d or {}).items() if k in names}


def _even(v: float) -> int:
    return max(2, int(round(v / 2)) * 2)


def clip_from_file(path: str) -> Clip:
    """A clip for a video file, with its length, size (as it plays) and frame rate."""
    from ..media import ff
    pr = ff.probe(path)
    v = pr.video
    if v is None:
        raise ValueError(f"{Path(path).name} has no picture")
    w, h = pr.width, pr.height
    if display_rotation(v.raw) in (90, 270):
        w, h = h, w
    return Clip(path=str(path), duration=pr.duration, width=w, height=h, fps=pr.fps or 30.0,
                has_audio=pr.audio is not None)


def display_rotation(stream_raw: dict) -> int:
    """The rotation a player applies (phones film sideways and store the turn as metadata)."""
    rot = 0
    try:
        rot = int(float(stream_raw.get("tags", {}).get("rotate", 0)))
    except (TypeError, ValueError):
        pass
    for sd in stream_raw.get("side_data_list", []) or []:
        if "rotation" in sd:
            try:
                rot = -int(float(sd["rotation"]))
            except (TypeError, ValueError):
                pass
    return rot % 360
