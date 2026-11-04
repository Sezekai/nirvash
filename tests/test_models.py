"""Real-model checks (AudioSet tagger, CLIP, Shazam). Run in CI with NIRVASH_MODEL_TESTS=1; models are downloaded once."""
from __future__ import annotations

import os
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pytestmark = pytest.mark.skipif(os.environ.get("NIRVASH_MODEL_TESTS") != "1", reason="model tests not enabled")


@pytest.fixture(scope="module", autouse=True)
def env(tmp_path_factory):
    d = tmp_path_factory.mktemp("nv")
    os.environ["NIRVASH_DATA"] = str(d / "data")
    os.environ.setdefault("NIRVASH_ROOT", str(d / "root"))
    from nirvash.core import settings as sm
    sm._settings = None
    from nirvash.analysis import models
    for spec in (models.AST, models.CLIP):
        if models.model_dir(spec) is None:
            models.download(spec)
    yield


def music(seconds=12.0, sr=48000):
    from tests.test_pipeline import make_music_wav  # noqa
    p = Path(os.environ["NIRVASH_DATA"]) / "m.wav"
    make_music_wav(p, seconds)
    from nirvash.media import ff
    return ff.decode_audio(p, sr)


def speech(sr=48000):
    """Spoken words via Windows text-to-speech."""
    out = Path(os.environ["NIRVASH_DATA"]) / "speech.wav"
    text = ("Hello and welcome back to the channel. Today I want to talk about how I spent my weekend, "
            "what I cooked, and why I think you should try it too. Let me know what you think in the comments.")
    ps = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          f"$s.SetOutputToWaveFile('{out}'); $s.Speak('{text}'); $s.Dispose()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
    from nirvash.media import ff
    return ff.decode_audio(out, sr)


def test_tagger_music_speech_and_talk_over_music():
    from nirvash.analysis import ast
    from nirvash.jobs.scan import slot_label
    tg = ast.get_tagger()
    print("provider", tg.provider)
    m = music(10.24, 16000)
    chunks = {"music": m}
    if sys.platform == "win32":
        s = speech(16000)[: len(m)]
        chunks["speech"] = s
        chunks["speech over music"] = 0.35 * m[: len(s)] + s
        chunks["quiet speech over loud music"] = 0.8 * m[: len(s)] + 0.5 * s
    p = tg.probs(list(chunks.values()))
    cls = tg.classes(p)
    got = {}
    for i, name in enumerate(chunks):
        top = np.argsort(-p[i])[:6]
        print(name, {k: round(float(v[i]), 3) for k, v in cls.items()},
              [(tg.labels[j], round(float(p[i, j]), 3)) for j in top])
        got[name] = slot_label(cls["music"][i], cls["speech"][i], cls["singing"][i], 0.5)
    print("decisions", got)
    assert got["music"] == "music"
    if "speech" in got:
        assert got["speech"] == "speech"
        assert got["speech over music"] == "speech"
    mood = tg.mood(p[:1])
    print("mood", mood)
    assert abs(sum(mood.values()) - 1) < 1e-3


def test_clip_colours():
    from nirvash.analysis.encoders import get_clip
    clip = get_clip()
    red = np.zeros((224, 224, 3), np.uint8)
    red[..., 0] = 230
    blue = np.zeros((224, 224, 3), np.uint8)
    blue[..., 2] = 230
    im = clip.embed_images([red, blue])
    t = clip.embed_text(["a plain red square", "a plain blue square"])
    sims = im @ t.T
    print("clip sims", sims)
    assert sims[0, 0] > sims[0, 1] and sims[1, 1] > sims[1, 0]


def test_shazam_no_match_is_clean():
    from nirvash.lookup import services
    y = music(12.0, 16000)
    r = services.shazam(y, 16000)
    assert r is None or isinstance(r, dict)


def test_lrclib_and_itunes_reachable():
    from nirvash.lookup import services
    it = services.itunes("Bohemian Rhapsody", "Queen")
    assert it and it.get("full_length", 0) > 300
    ly = services.lrclib("Bohemian Rhapsody", "Queen", None, it["full_length"])
    assert ly and ly.get("lyrics_plain")
