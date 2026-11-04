"""Real-model checks (CLAP, CLIP, Shazam). Run in CI with NIRVASH_MODEL_TESTS=1; models are downloaded once."""
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
    for spec in (models.CLAP, models.CLIP):
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


def test_clap_music_vs_speech():
    from nirvash.analysis import labels
    from nirvash.analysis.encoders import get_clap
    clap = get_clap()
    m = music()
    chunks = [m[:480000]]
    if sys.platform == "win32":
        s = speech()
        chunks.append(s[:480000])
    embs = clap.embed_audio(chunks)
    assert embs.shape == (len(chunks), 512)
    assert np.allclose(np.linalg.norm(embs, axis=1), 1, atol=1e-3)
    probs = labels.audio_class_probs(embs, clap)
    print("music clip:", {k: round(float(v[0]), 3) for k, v in probs.items()})
    assert probs["music"][0] > probs["speech"][0]
    if len(chunks) > 1:
        print("speech clip:", {k: round(float(v[1]), 3) for k, v in probs.items()})
        assert probs["speech"][1] > probs["music"][1]
    mood = labels.song_mood(embs[0], clap)
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
