"""AudioSet tagger (Audio Spectrogram Transformer, ONNX) for "is this music, talking, or both?" and song mood.

It gives an independent yes/no for each of AudioSet's 527 sounds, so talking over music shows up as both
Speech and Music (and is left out), while singing shows up as Music + Singing (and is kept)."""
from __future__ import annotations

import json
import threading

import numpy as np

from ..core import log as _log
from ..core.settings import get_settings
from . import models
from .runtime import make_session, pick_output

log = _log.get("ast")

SR = 16000
N_MELS = 128
MAX_FRAMES = 1024          # 10.24 s
WIN_S = 10.24
MEAN, STD = -4.2677393, 4.5689974


def _mel_filters() -> np.ndarray:
    """Kaldi-style triangular mel filters (built in mel space), 257 bins -> 128 mels."""
    n_bins = 257
    lo, hi = 20.0, SR / 2

    def hz2mel(f):
        return 1127.0 * np.log(1.0 + np.asarray(f, np.float64) / 700.0)

    mel_lo, mel_hi = hz2mel(lo), hz2mel(hi)
    mel_pts = np.linspace(mel_lo, mel_hi, N_MELS + 2)
    fft_mels = hz2mel(np.linspace(0, SR // 2, n_bins))
    left, center, right = mel_pts[:-2], mel_pts[1:-1], mel_pts[2:]
    up = (fft_mels[:, None] - left[None, :]) / (center - left)[None, :]
    down = (right[None, :] - fft_mels[:, None]) / (right - center)[None, :]
    return np.maximum(0.0, np.minimum(up, down))  # (257, 128)


_FB = _mel_filters()
_WIN = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(400) / 399)  # symmetric Hann


def fbank(wave: np.ndarray) -> np.ndarray:
    """Mono 16 kHz float waveform -> (1024, 128) normalised log-mel features."""
    x = np.asarray(wave, np.float64)
    if len(x) < 400:
        x = np.pad(x, (0, 400 - len(x)))
    n = 1 + (len(x) - 400) // 160
    idx = np.arange(400)[None, :] + 160 * np.arange(n)[:, None]
    fr = x[idx]
    fr = fr - fr.mean(axis=1, keepdims=True)                       # remove DC
    fr = np.concatenate([fr[:, :1] * (1 - 0.97), fr[:, 1:] - 0.97 * fr[:, :-1]], axis=1)  # pre-emphasis
    fr = fr * _WIN[None, :]
    spec = np.abs(np.fft.rfft(fr, n=512, axis=1)) ** 2
    mel = np.log(np.maximum(spec @ _FB, 1.192092955078125e-07))
    if mel.shape[0] < MAX_FRAMES:
        mel = np.pad(mel, ((0, MAX_FRAMES - mel.shape[0]), (0, 0)))
    else:
        mel = mel[:MAX_FRAMES]
    return ((mel - MEAN) / (STD * 2)).astype(np.float32)


# AudioSet label groups (names as in the model's config)
SPEECH = ["Speech", "Male speech, man speaking", "Female speech, woman speaking", "Child speech, kid speaking",
          "Conversation", "Narration, monologue", "Babbling", "Speech synthesizer", "Whispering", "Shout",
          "Laughter"]
SINGING = ["Singing", "Choir", "Male singing", "Female singing", "Child singing", "Rapping", "Vocal music",
           "A capella", "Song"]
MUSIC = ["Music", "Musical instrument", "Pop music", "Electronic music"]
MOOD_LABELS = {
    "happy": "Happy music",
    "playful": "Funny music",
    "sad": "Sad music",
    "calm": "Tender music",
    "energetic": "Exciting music",
    "aggressive": "Angry music",
    "dark": "Scary music",
}
GENRES = ["Pop music", "Hip hop music", "Rock music", "Heavy metal", "Punk rock", "Rhythm and blues", "Soul music",
          "Reggae", "Country", "Funk", "Folk music", "Jazz", "Disco", "Classical music", "Electronic music",
          "House music", "Techno", "Dubstep", "Drum and bass", "Electronica", "Electronic dance music",
          "Ambient music", "Trance music", "Music of Latin America", "Music of Asia", "Christian music",
          "Music for children", "Independent music", "Video game music", "Soundtrack music", "Lo-fi music"]


class AudioTagger:
    def __init__(self):
        d = models.model_dir(models.AST)
        if d is None:
            raise ModelMissing("The audio model isn't downloaded yet (Settings > AI models).")
        cfg = json.loads((d / "config.json").read_text("utf-8"))
        id2label = {int(k): v for k, v in cfg["id2label"].items()}
        self.labels = [id2label[i] for i in range(len(id2label))]
        self.index = {n: i for i, n in enumerate(self.labels)}
        device = get_settings().get("models.device", "auto")
        self.sess, self.provider = make_session(d / "model.onnx", device)
        self.inp = self.sess.get_inputs()[0].name
        self.out = pick_output(self.sess, "logits")
        self.batch = 4
        try:
            self.probs([np.zeros(SR, np.float32)])
        except Exception as e:
            log.warning("audio model failed on %s (%s); using the processor", self.provider, e)
            self.sess, self.provider = make_session(d / "model.onnx", "cpu")
        log.info("Audio tagger ready on %s", self.provider)

    def probs(self, chunks: list[np.ndarray]) -> np.ndarray:
        """Mono 16 kHz chunks (<=10.24 s) -> (n, 527) sigmoid probabilities."""
        out = []
        i = 0
        while i < len(chunks):
            part = chunks[i:i + self.batch]
            x = np.stack([fbank(c) for c in part])
            try:
                logits = self.sess.run([self.out], {self.inp: x})[0]
            except Exception:
                if self.batch > 1:
                    self.batch = 1
                    continue
                raise
            out.append(1.0 / (1.0 + np.exp(-logits)))
            i += len(part)
        return np.concatenate(out) if out else np.zeros((0, len(self.labels)), np.float32)

    def group(self, p: np.ndarray, names: list[str]) -> np.ndarray:
        idx = [self.index[n] for n in names if n in self.index]
        return p[:, idx].max(axis=1) if idx else np.zeros(len(p))

    def classes(self, p: np.ndarray) -> dict[str, np.ndarray]:
        return {"music": self.group(p, MUSIC), "speech": self.group(p, SPEECH), "singing": self.group(p, SINGING)}

    def mood(self, p: np.ndarray) -> dict[str, float]:
        """Mean probabilities over windows -> normalised mood mix."""
        m = p.mean(axis=0)
        v = np.array([m[self.index[l]] if l in self.index else 0.0 for l in MOOD_LABELS.values()])
        v = v / max(float(v.sum()), 1e-9)
        return {k: round(float(x), 4) for k, x in zip(MOOD_LABELS, v)}

    def genre(self, p: np.ndarray) -> tuple[str, float] | None:
        m = p.mean(axis=0)
        best = None
        for g in GENRES:
            if g in self.index:
                v = float(m[self.index[g]])
                if best is None or v > best[1]:
                    best = (g, v)
        return best


class ModelMissing(RuntimeError):
    pass


_tagger: AudioTagger | None = None
_lock = threading.Lock()


def get_tagger() -> AudioTagger:
    global _tagger
    with _lock:
        if _tagger is None:
            _tagger = AudioTagger()
        return _tagger


def reset() -> None:
    global _tagger
    with _lock:
        _tagger = None
