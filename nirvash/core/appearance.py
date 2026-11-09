"""The look of the app as plain data: presets, themes, theme files (DESIGN.md "Appearance").

A theme is JSON in the format every app of the suite shares, so themes move between apps. Nirvash adds one
optional block, "scene" (a starfield drawn behind see-through panels); other apps simply ignore it."""
from __future__ import annotations

import copy
import json
import re
import threading
from pathlib import Path
from typing import Any

from . import paths

THEME_VERSION = 1
BG_KEYS = ["ground", "rail", "panel", "raised", "raised2", "field", "line", "line2", "text", "sec", "faint", "stage"]
BG_LABELS = {"ground": "Window", "rail": "Side rail", "panel": "Panels", "raised": "Buttons and cards",
             "raised2": "Hover and selected", "field": "Text boxes and lists", "line": "Borders",
             "line2": "Strong borders", "text": "Text", "sec": "Secondary text", "faint": "Faint text",
             "stage": "Behind videos"}
STATE_KEYS = ["ok", "warn", "danger"]
STATE_LABELS = {"ok": "Success", "warn": "Warning", "danger": "Danger"}
DEFAULT_STATES = {"ok": "#5be38a", "warn": "#ffbf5a", "danger": "#ff5f73"}


def _bg(name, *v, scene=None):
    d = {"name": name, **dict(zip(BG_KEYS, v))}
    if scene:
        d["scene"] = scene
    return d


# Each background lists every surface and text colour. All keep text, secondary and faint text at 4.5:1 or more.
BACKGROUNDS: dict[str, dict] = {
    "starglass": _bg("Starfield, see-through", "#05060b", "#05060a", "#0f1220", "#171b2d", "#20263d", "#090b14",
                     "#2a3150", "#353d61", "#eceef5", "#a7adc8", "#8a92b5", "#03040a",
                     scene={"image": "starfield", "opacity": 66}),
    "starfield": _bg("Starfield", "#05060b", "#05060a", "#0f1220", "#171b2d", "#20263d", "#090b14", "#1e2338",
                     "#2c3352", "#eceef5", "#a0a6c2", "#8189ad", "#03040a", scene={"image": "starfield", "opacity": 100}),
    "obsidian": _bg("Obsidian", "#0a0a0b", "#060607", "#111113", "#19191c", "#232327", "#070708", "#212125", "#2e2e33",
                    "#ececee", "#a0a0a7", "#7d7d84", "#040405"),
    "carbon": _bg("Carbon", "#0e0f10", "#0a0b0c", "#151618", "#1d1f22", "#27292d", "#0a0b0c", "#25272b", "#33363b",
                  "#eaecee", "#9fa4aa", "#7d8188", "#070808"),
    "graphite": _bg("Graphite", "#111214", "#0d0e10", "#18191c", "#202226", "#2a2c31", "#0d0e10", "#2a2c31", "#383b42",
                    "#e8eaed", "#9aa0a8", "#80858d", "#0a0b0c"),
    "midnight": _bg("Midnight", "#0c0e15", "#090b11", "#121521", "#1a1e2d", "#252a3d", "#0a0c12", "#232839", "#30364b",
                    "#e8eaf2", "#9ba1b6", "#7b8095", "#07080d"),
    "ink": _bg("Ink blue", "#08101f", "#060c18", "#0e1829", "#152237", "#1d2d47", "#060c17", "#1b2840", "#273754",
               "#e6edf8", "#94a3bd", "#76849b", "#040912"),
    "slate": _bg("Slate", "#151a21", "#10141a", "#1b2129", "#232b35", "#2d3641", "#11161c", "#2b333e", "#38424e",
                 "#e7ebf0", "#9ba5b2", "#818a95", "#0d1116"),
    "aubergine": _bg("Aubergine", "#110d16", "#0c0910", "#18121f", "#21192b", "#2c2239", "#0c0910", "#2a2135",
                     "#3a2e48", "#efe9f5", "#a99cb8", "#887c95", "#08060b"),
    "auberginegreen": _bg("Aubergine green", "#0d1712", "#09100d", "#121f19", "#192b23", "#213a2e", "#09100d",
                          "#1c3026", "#2b4a3c", "#edf2f0", "#9db8ac", "#7da190", "#070b09"),
    "garnet": _bg("Garnet", "#170c0e", "#11090a", "#1f1113", "#2c181b", "#3b2024", "#11090a", "#311a1e", "#4c292f",
                  "#f2edee", "#b99da1", "#a27c82", "#0c0607"),
    "violethaze": _bg("Violet haze", "#0e0c18", "#0a0811", "#131020", "#1b162d", "#241e3d", "#0a0811", "#1e1933",
                      "#2f274f", "#eeecf3", "#a098bd", "#877eae", "#07060c"),
    "tealdepth": _bg("Teal depth", "#0c1816", "#081110", "#10201e", "#162d2a", "#1e3d39", "#081110", "#19332f",
                     "#274f49", "#edf2f2", "#9cbab6", "#7ba39e", "#060c0b"),
    "plum": _bg("Plum", "#170d16", "#100910", "#1f111e", "#2b182a", "#3a2138", "#100910", "#301b2f", "#4b2a48",
                "#f2edf2", "#b99db7", "#a27c9f", "#0b060b"),
    "rose": _bg("Rose noir", "#160d12", "#10090d", "#1f1218", "#2b1922", "#39222d", "#10090d", "#301c26", "#4a2b3b",
                "#f2edf0", "#b89dab", "#a17d8f", "#0b0709"),
    "petrol": _bg("Petrol", "#0b1518", "#080f11", "#101c21", "#16282e", "#1d353e", "#080f11", "#182c33", "#264550",
                  "#edf1f2", "#9cb2ba", "#7b98a3", "#060a0c"),
    "nightsky": _bg("Night sky", "#0b0e19", "#080a12", "#0f1321", "#151b2f", "#1c243f", "#080a12", "#171e34", "#242f51",
                    "#eceef3", "#98a1bd", "#7682a7", "#06070c"),
    "olive": _bg("Olive", "#14160e", "#0e100a", "#1b1e13", "#26291a", "#323723", "#0e100a", "#2a2e1d", "#41482e",
                 "#f1f2ee", "#b1b79f", "#979f7f", "#0a0b07"),
    "wine": _bg("Wine", "#140b0e", "#0f0709", "#1c1014", "#26161c", "#321e25", "#0f0709", "#2e1b21", "#3f262e",
                "#f3e9eb", "#b39aa1", "#917c82", "#0a0507"),
    "forest": _bg("Forest", "#0b110d", "#080c09", "#111912", "#18231a", "#212e23", "#080c09", "#1d2a20", "#29392c",
                  "#e8f1ea", "#97ab9b", "#758678", "#050906"),
    "teal": _bg("Deep teal", "#0a1214", "#070e10", "#101a1d", "#172427", "#203033", "#070e10", "#1d2b2e", "#29393d",
                "#e5f0ef", "#93aaa8", "#738887", "#050b0c"),
    "mocha": _bg("Mocha", "#13110f", "#0e0c0b", "#1a1715", "#231f1c", "#2e2925", "#0e0c0b", "#2b2622", "#3a332e",
                 "#efebe6", "#ada49b", "#898078", "#090807"),
    "indigo": _bg("Indigo", "#0e0f17", "#0a0b12", "#161826", "#1d2032", "#252942", "#0b0c13", "#2b3050", "#3a4170",
                  "#e8eaf6", "#8d93b5", "#7c82a8", "#07080d"),
    "daylight": _bg("Daylight", "#eef0f3", "#e4e7ec", "#f8f9fb", "#ffffff", "#e8ebf0", "#ffffff", "#d9dde4", "#c7ccd5",
                    "#1b1e24", "#545b66", "#6b727c", "#dfe2e7"),
    "linen": _bg("Linen", "#f2efe9", "#e9e4db", "#faf8f4", "#ffffff", "#ece7de", "#ffffff", "#ddd6ca", "#cbc2b3",
                 "#25211b", "#5f574c", "#776f64", "#e4ded3"),
}

ACCENTS: dict[str, dict] = {
    "tealViolet": {"name": "Teal → Violet", "stops": ["#2fe0c0", "#9b86ff"]},
    "limeTeal": {"name": "Lime → Teal", "stops": ["#b6f05a", "#2fd6b4"]},
    "violetBlue": {"name": "Violet → Blue", "stops": ["#a497ff", "#74a0ff"]},
    "violetPink": {"name": "Violet → Pink", "stops": ["#b08cff", "#ff7ab6"]},
    "blueCyan": {"name": "Blue → Cyan", "stops": ["#6b98ff", "#3fd3f0"]},
    "aquaLime": {"name": "Aqua → Lime", "stops": ["#3fe0e0", "#b6f05a"]},
    "mintSky": {"name": "Mint → Sky", "stops": ["#7ef0b6", "#7cc4ff"]},
    "amberPink": {"name": "Amber → Pink", "stops": ["#ffb547", "#ff6f91"]},
    "coralOrange": {"name": "Coral → Orange", "stops": ["#ff7a6b", "#ffb04d"]},
    "sunset": {"name": "Sunset", "stops": ["#ffb347", "#ff6a88", "#b47cff"]},
    "rosePeach": {"name": "Rose → Peach", "stops": ["#ff8fb1", "#ffc59b"]},
    "goldLime": {"name": "Gold → Lime", "stops": ["#ffd25e", "#b6f05a"]},
    "aurora": {"name": "Aurora", "stops": ["#5ef0c0", "#5aa8ff", "#c08cff"]},
    "solidLime": {"name": "Lime", "stops": ["#b6f05a"]},
    "solidTeal": {"name": "Teal", "stops": ["#2fd6b4"]},
    "solidBlue": {"name": "Blue", "stops": ["#5aa6ff"]},
    "solidViolet": {"name": "Violet", "stops": ["#a497ff"]},
    "solidGreen": {"name": "Green", "stops": ["#4cc274"]},
    "solidOrange": {"name": "Orange", "stops": ["#ff9d4d"]},
    "solidPink": {"name": "Pink", "stops": ["#ff7ab6"]},
}
CORNERS = {"square": ("Square", 2, 3), "sharp": ("Sharp", 4, 6), "standard": ("Standard", 6, 10),
           "soft": ("Soft", 8, 14), "round": ("Round", 12, 18)}
DENSITY = {"compact": ("Compact", 28, 24), "comfortable": ("Comfortable", 32, 28), "roomy": ("Roomy", 36, 32)}
TEXT_SIZES = [12, 13, 14, 15]
SYSTEM_FONTS = ["Segoe UI", "Segoe UI Variable Text", "Satoshi", "Bahnschrift", "Aptos", "Calibri"]
SCENES = {"none": "Plain", "starfield": "Starfield"}


def _bg_values(key: str) -> dict:
    b = {k: v for k, v in BACKGROUNDS[key].items() if k in BG_KEYS}
    return b


DEFAULT_THEME: dict = {
    "version": THEME_VERSION,
    "name": "Default",
    "background": _bg_values("starglass"),
    "states": dict(DEFAULT_STATES),
    "accent": {"stops": list(ACCENTS["tealViolet"]["stops"]), "angle": 90, "onAccent": "auto"},
    "font": {"family": "Inter", "size": 13},
    "corners": {"control": 6, "panel": 10},
    "density": {"control": 28, "row": 24},
    "buttons": {"style": "flat"},
    "scene": dict(BACKGROUNDS["starglass"]["scene"]),
}

_HEX = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _color(v, d):
    if isinstance(v, str) and _HEX.match(v.strip()):
        h = v.strip().lower()
        return "#" + "".join(c * 2 for c in h[1:]) if len(h) == 4 else h
    return d


def _clamp(v, lo, hi, d):
    try:
        n = round(float(v))
    except (TypeError, ValueError):
        return d
    return max(lo, min(hi, n))


def normalize(t: Any) -> dict:
    """Fill gaps from the default and drop anything invalid, so an imported or hand-edited theme can't break the app."""
    d = DEFAULT_THEME
    t = t if isinstance(t, dict) else {}
    g = lambda k: t.get(k) if isinstance(t.get(k), dict) else {}  # noqa: E731
    bg = {k: _color(g("background").get(k), d["background"][k]) for k in BG_KEYS}
    st = {k: _color(g("states").get(k), d["states"][k]) for k in STATE_KEYS}
    stops = [s for s in (_color(x, None) for x in (g("accent").get("stops") or [])) if s][:4] \
        if isinstance(g("accent").get("stops"), list) else []
    if not stops:
        stops = list(d["accent"]["stops"])
    on = g("accent").get("onAccent")
    fam = g("font").get("family")
    style = g("buttons").get("style")
    if "scene" in t:
        sc = g("scene")
        scene = {"image": sc.get("image") if sc.get("image") in SCENES else "none",
                 "opacity": _clamp(sc.get("opacity"), 40, 100, 100)}
    else:
        # A theme from another app has no scene: show it plain, as it looks there.
        scene = {"image": "none", "opacity": 100} if t else dict(d["scene"])
    return {
        "version": THEME_VERSION,
        "name": t["name"].strip()[:60] if isinstance(t.get("name"), str) and t["name"].strip() else d["name"],
        "background": bg,
        "states": st,
        "accent": {"stops": stops, "angle": _clamp(g("accent").get("angle"), 0, 360, 90),
                   "onAccent": on if on in ("dark", "light") else "auto"},
        "font": {"family": fam.strip()[:80] if isinstance(fam, str) and fam.strip() else d["font"]["family"],
                 "size": _clamp(g("font").get("size"), 11, 16, d["font"]["size"])},
        "corners": {"control": _clamp(g("corners").get("control"), 0, 16, d["corners"]["control"]),
                    "panel": _clamp(g("corners").get("panel"), 0, 24, d["corners"]["panel"])},
        "density": {"control": _clamp(g("density").get("control"), 24, 44, d["density"]["control"]),
                    "row": _clamp(g("density").get("row"), 20, 40, d["density"]["row"])},
        "buttons": {"style": style if style in ("flat", "glow") else "flat"},
        "scene": scene,
    }


def luminance(hex_: str) -> float:
    h = _color(hex_, "#000000")
    out = []
    for i in (1, 3, 5):
        v = int(h[i:i + 2], 16) / 255
        out.append(v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4)
    return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]


def contrast(a: str, b: str) -> float:
    x, y = sorted((luminance(a), luminance(b)), reverse=True)
    return (x + 0.05) / (y + 0.05)


DARK_ON, LIGHT_ON = "#0b0c0f", "#ffffff"


def on_accent(t: dict) -> str:
    """Text colour that stays readable on every colour of the accent."""
    mode = t["accent"]["onAccent"]
    if mode == "dark":
        return DARK_ON
    if mode == "light":
        return LIGHT_ON
    worst = lambda c: min(contrast(c, s) for s in t["accent"]["stops"])  # noqa: E731
    return DARK_ON if worst(DARK_ON) >= worst(LIGHT_ON) else LIGHT_ON


def accent_contrast(t: dict) -> float:
    c = on_accent(t)
    return min(contrast(c, s) for s in t["accent"]["stops"])


def is_light(t: dict) -> bool:
    return luminance(t["background"]["ground"]) > 0.4


def describe(t: dict) -> dict:
    """Which presets a theme matches, e.g. 'Starfield, see-through · Teal → Violet · Inter · Standard · Compact'."""
    t = normalize(t)
    bg = next((k for k, v in BACKGROUNDS.items() if all(v[f] == t["background"][f] for f in BG_KEYS)
               and (v.get("scene") or {"image": "none", "opacity": 100}) == t["scene"]), None)
    if bg is None:
        bg = next((k for k, v in BACKGROUNDS.items() if all(v[f] == t["background"][f] for f in BG_KEYS)), None)
    acc = next((k for k, v in ACCENTS.items() if v["stops"] == t["accent"]["stops"]), None)
    cor = next((k for k, v in CORNERS.items() if (v[1], v[2]) == (t["corners"]["control"], t["corners"]["panel"])), None)
    den = next((k for k, v in DENSITY.items() if (v[1], v[2]) == (t["density"]["control"], t["density"]["row"])), None)
    return {"background": bg, "accent": acc, "corners": cor, "density": den,
            "text": " · ".join([BACKGROUNDS[bg]["name"] if bg else "Custom background",
                                ACCENTS[acc]["name"] if acc else "Custom accent", t["font"]["family"],
                                CORNERS[cor][0] if cor else "Custom corners",
                                DENSITY[den][0] if den else "Custom density"])}


def with_background(t: dict, key: str) -> dict:
    t = normalize(t)
    t["background"] = _bg_values(key)
    t["scene"] = dict(BACKGROUNDS[key].get("scene") or {"image": "none", "opacity": 100})
    return t


def presets_check() -> list[str]:
    """Problems with the presets (text below 4.5:1 on panels). Used by the tests."""
    bad = []
    for k, v in BACKGROUNDS.items():
        for f in ("text", "sec", "faint"):
            if contrast(v[f], v["panel"]) < 4.5:
                bad.append(f"{k}.{f} {contrast(v[f], v['panel']):.2f}")
    return bad


# ---------------------------------------------------------------------------
# Storage: appearance.json in the data folder (current look, active theme, saved themes)
class Appearance:
    def __init__(self, path: Path | None = None):
        self.path = path or (paths.data_dir() / "appearance.json")
        self._lock = threading.RLock()
        self.current: dict = normalize(None)
        self.active: str | None = "Default"
        self.saved: list[dict] = []
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            return
        except Exception:
            try:
                self.path.replace(self.path.with_suffix(".broken.json"))
            except Exception:
                pass
            return
        self.current = normalize(raw.get("current"))
        self.active = raw.get("active") if isinstance(raw.get("active"), str) else None
        self.saved = [normalize(x) for x in raw.get("saved") or [] if isinstance(x, dict)]

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": THEME_VERSION, "current": self.current, "active": self.active,
                                       "saved": self.saved}, indent=1), "utf-8")
            tmp.replace(self.path)

    def set_current(self, t: dict, active: str | None = None, save: bool = True) -> None:
        self.current = normalize(t)
        self.active = active
        if save:
            self.save()

    def theme(self, name: str) -> dict | None:
        if name == "Default":
            return normalize(None)
        return next((copy.deepcopy(x) for x in self.saved if x["name"] == name), None)

    def unique_name(self, name: str) -> str:
        names = {x["name"].casefold() for x in self.saved} | {"default"}
        base, n, out = name.strip() or "My theme", 2, name.strip() or "My theme"
        while out.casefold() in names:
            out = f"{base} ({n})"
            n += 1
        return out

    def save_as(self, name: str) -> str:
        t = copy.deepcopy(self.current)
        t["name"] = self.unique_name(name)
        self.saved.append(t)
        self.active = t["name"]
        self.save()
        return t["name"]

    def update(self, name: str) -> None:
        for i, x in enumerate(self.saved):
            if x["name"] == name:
                t = copy.deepcopy(self.current)
                t["name"] = name
                self.saved[i] = t
        self.active = name
        self.save()

    def rename(self, old: str, new: str) -> str:
        new = self.unique_name(new)
        for x in self.saved:
            if x["name"] == old:
                x["name"] = new
        if self.active == old:
            self.active = new
        self.save()
        return new

    def duplicate(self, name: str) -> str:
        t = self.theme(name)
        if t is None:
            return name
        t["name"] = self.unique_name(f"{name} copy")
        self.saved.append(t)
        self.save()
        return t["name"]

    def delete(self, name: str) -> None:
        self.saved = [x for x in self.saved if x["name"] != name]
        if self.active == name:
            self.active = None
        self.save()

    def edited(self) -> bool:
        """The current look differs from the active theme."""
        t = self.theme(self.active) if self.active else None
        if t is None:
            return True
        a, b = dict(t), dict(self.current)
        a.pop("name", None)
        b.pop("name", None)
        return a != b

    # theme files
    @staticmethod
    def export_file(t: dict, path: Path) -> None:
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(json.dumps(normalize(t), indent=2), "utf-8")
        tmp.replace(path)

    def import_file(self, path: Path) -> str:
        raw = json.loads(Path(path).read_text("utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("That file isn't a theme.")
        t = normalize(raw)
        t["name"] = self.unique_name(t["name"] if t["name"] != "Default" else Path(path).stem.split(".")[0])
        self.saved.append(t)
        self.save()
        return t["name"]


_app: Appearance | None = None


def get_appearance() -> Appearance:
    global _app
    if _app is None:
        _app = Appearance()
    return _app


# ---------------------------------------------------------------------------
# Fonts: bundled (assets/fonts) and added by you (data/fonts/<id>/, listed in fonts/index.json)
def bundled_fonts() -> list[dict]:
    try:
        return json.loads((paths.asset("fonts") / "fonts.json").read_text("utf-8"))
    except Exception:
        return []


def user_fonts_dir() -> Path:
    return paths.data_dir() / "fonts"


def user_fonts() -> list[dict]:
    try:
        return json.loads((user_fonts_dir() / "index.json").read_text("utf-8"))
    except Exception:
        return []


WEIGHT_WORDS = [("thin", 100), ("extralight", 200), ("ultralight", 200), ("light", 300), ("regular", 400),
                ("book", 400), ("normal", 400), ("medium", 500), ("semibold", 600), ("demibold", 600),
                ("extrabold", 800), ("ultrabold", 800), ("black", 900), ("heavy", 900), ("bold", 700)]


def guess_font(files: list[Path]) -> tuple[str, list[dict]]:
    """Family name and weights from file names like 'Satoshi-Bold.otf'."""
    out = []
    fam = None
    for f in files:
        stem = f.stem
        low = re.sub(r"[^a-z]", "", stem.lower())
        weight = 400
        for w, n in WEIGHT_WORDS:
            if low.endswith(w) or low.endswith(w + "italic"):
                weight = n
                break
        style = "italic" if "italic" in low else "normal"
        base = re.split(r"[-_ ]", stem)[0]
        fam = fam or re.sub(r"(?<=[a-z])(?=[A-Z])", " ", base).strip()
        out.append({"file": f.name, "weight": weight, "style": style})
    return fam or "My font", out


def add_user_font(files: list[Path], family: str) -> dict:
    import shutil
    fid = re.sub(r"[^a-z0-9]+", "-", family.lower()).strip("-") or "font"
    d = user_fonts_dir() / fid
    d.mkdir(parents=True, exist_ok=True)
    _fam, info = guess_font(files)
    for f in files:
        shutil.copy2(f, d / f.name)
    idx = [x for x in user_fonts() if x.get("id") != fid]
    entry = {"id": fid, "family": family, "files": info}
    idx.append(entry)
    tmp = user_fonts_dir() / "index.json.tmp"
    tmp.write_text(json.dumps(idx, indent=1), "utf-8")
    tmp.replace(user_fonts_dir() / "index.json")
    return entry
