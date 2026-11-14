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

    @property
    def out(self) -> float:
        return self.end if self.end > 0 else self.duration

    @property
    def length(self) -> float:
        return max(0.0, self.out - self.start)

    def shown_size(self) -> tuple[int, int]:
        """Width and height after the clip's own rotation."""
        return (self.height, self.width) if self.rotate in (90, 270) else (self.width, self.height)


Key = list  # [t, x, y, w, h]


def interp(keys: list[Key], t: float) -> tuple[float, float, float, float] | None:
    """Where a moving box is at time t: straight lines between keys, held before the first and after the last."""
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
    f = 0.0 if span <= 0 else (t - a[0]) / span
    return tuple(a[j] + (b[j] - a[j]) * f for j in range(1, 5))


def set_key(keys: list[Key], t: float, box: tuple[float, float, float, float], moving: bool, snap: float = 0.02) -> None:
    """Change a box at time t. Not moving: the single key changes. Moving: a key at t is added or replaced."""
    box = [float(v) for v in box]
    if not moving or not keys:
        keys[:] = [[0.0] + box] if not moving else [[t] + box]
        return
    for k in keys:
        if abs(k[0] - t) <= snap:
            k[1:5] = box
            return
    bisect.insort(keys, [t] + box, key=lambda k: k[0])


@dataclass
class Crop:
    enabled: bool = False
    aspect: str = "free"
    moving: bool = False            # keys at different times (moving crop / zoom)
    keys: list = field(default_factory=lambda: [[0.0, 0.1, 0.1, 0.8, 0.8]])


@dataclass
class Blur:
    id: str = field(default_factory=new_id)
    kind: str = "blur"              # blur | pixelate
    strength: int = 50              # 1..100
    shape: str = "rect"             # rect | ellipse
    start: float = 0.0
    end: float = 0.0                # 0 = to the end
    moving: bool = False
    keys: list = field(default_factory=lambda: [[0.0, 0.4, 0.4, 0.2, 0.2]])


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
    output: Output = field(default_factory=Output)
    version: int = 1

    # ---- timeline -----------------------------------------------------------------------
    @property
    def duration(self) -> float:
        return sum(c.length for c in self.clips)

    def clip_at(self, t: float) -> tuple[int, float]:
        """Which clip plays at output time t, and the time inside its source file."""
        acc = 0.0
        for i, c in enumerate(self.clips):
            if t < acc + c.length or i == len(self.clips) - 1:
                return i, c.start + max(0.0, min(c.length, t - acc))
            acc += c.length
        return 0, 0.0

    def clip_offset(self, i: int) -> float:
        return sum(c.length for c in self.clips[:i])

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
        a = ASPECTS.get(self.crop.aspect)
        if a == "source":
            w, h, _ = self.canvas()
            return w / h
        return a

    def output_size(self) -> tuple[int, int]:
        """Output width and height: the crop box's size in canvas pixels (its first key), else the canvas."""
        W, H, _ = self.canvas()
        if not self.crop.enabled or not self.crop.keys:
            return W, H
        _t, x, y, w, h = self.crop.keys[0]
        return max(16, _even(w * W)), max(16, _even(h * H))

    def crop_box(self, t: float) -> tuple[float, float, float, float] | None:
        if not self.crop.enabled:
            return None
        return interp(self.crop.keys, t)

    def blur_end(self, b: Blur) -> float:
        return b.end if b.end > 0 else self.duration

    def overlay_end(self, o: Overlay) -> float:
        return o.end if o.end > 0 else self.duration

    # ---- what kind of export it needs ---------------------------------------------------------------
    def frame_effects(self) -> bool:
        """Anything that changes the picture itself (so the video must be re-encoded)."""
        return (self.crop.enabled or bool(self.blurs) or bool(self.overlays) or not self.colour.is_neutral()
                or any(abs(c.angle) > 0.01 for c in self.clips))

    def is_empty_edit(self) -> bool:
        return (not self.frame_effects() and len(self.clips) <= 1
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
