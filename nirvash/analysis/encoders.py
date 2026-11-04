"""CLAP (audio<->text) and CLIP (image<->text) encoders on ONNX Runtime."""
from __future__ import annotations

import threading

import numpy as np

from ..core import log as _log
from ..core.settings import get_settings
from . import mel, models
from .runtime import make_session, pick_output

log = _log.get("encoders")


def _l2(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, np.float32)
    return x / np.maximum(np.linalg.norm(x, axis=-1, keepdims=True), 1e-8)


class ModelMissing(RuntimeError):
    pass


class Clap:
    def __init__(self):
        d = models.model_dir(models.CLAP)
        if d is None:
            raise ModelMissing("The CLAP model isn't downloaded yet (Settings > AI models).")
        from tokenizers import Tokenizer

        device = get_settings().get("models.device", "auto")
        self.audio, self.provider = make_session(d / "audio_model.onnx", device)
        self.text, _ = make_session(d / "text_model.onnx", device)
        self.tok = Tokenizer.from_file(str(d / "tokenizer.json"))
        self.tok.enable_padding(pad_id=1, pad_token="<pad>")
        self.tok.enable_truncation(max_length=77)
        self.a_in = [i.name for i in self.audio.get_inputs()]
        self.a_out = pick_output(self.audio, "audio_embeds")
        self.t_in = [i.name for i in self.text.get_inputs()]
        self.t_out = pick_output(self.text, "text_embeds")
        self._text_cache: dict[str, np.ndarray] = {}
        self.batch = 8
        try:
            self.embed_audio([np.zeros(48000, np.float32)] * 2)
            self.embed_text(["music"])
        except Exception as e:
            # Some graphics drivers can't run every layer; the processor always can.
            log.warning("CLAP failed on %s (%s); using the processor", self.provider, e)
            self.audio, self.provider = make_session(d / "audio_model.onnx", "cpu")
            self.text, _ = make_session(d / "text_model.onnx", "cpu")
            self._text_cache.clear()
            self.batch = 8
        log.info("CLAP ready on %s", self.provider)

    def embed_audio(self, chunks: list[np.ndarray]) -> np.ndarray:
        """Mono 48 kHz chunks (<=10 s each) -> (n, 512) normalised embeddings."""
        out = []
        i = 0
        while i < len(chunks):
            part = chunks[i:i + self.batch]
            feats = mel.features(part)
            feeds = {self.a_in[0]: feats}
            for name in self.a_in[1:]:
                if "longer" in name:
                    feeds[name] = np.zeros((len(part), 1), bool)
            try:
                res = self.audio.run([self.a_out], feeds)[0]
            except Exception:
                if self.batch > 1:
                    self.batch = 1
                    continue
                raise
            out.append(res)
            i += len(part)
        return _l2(np.concatenate(out)) if out else np.zeros((0, 512), np.float32)

    def embed_text(self, texts: list[str]) -> np.ndarray:
        missing = [t for t in texts if t not in self._text_cache]
        if missing:
            enc = self.tok.encode_batch(missing)
            ids = np.array([e.ids for e in enc], np.int64)
            mask = np.array([e.attention_mask for e in enc], np.int64)
            feeds = {}
            for name in self.t_in:
                feeds[name] = mask if "mask" in name else ids
            res = _l2(self.text.run([self.t_out], feeds)[0])
            for t, v in zip(missing, res):
                self._text_cache[t] = v
        return np.stack([self._text_cache[t] for t in texts])


class Clip:
    MEAN = np.array([0.48145466, 0.4578275, 0.40821073], np.float32)
    STD = np.array([0.26862954, 0.26130258, 0.27577711], np.float32)
    SIZE = 224

    def __init__(self):
        d = models.model_dir(models.CLIP)
        if d is None:
            raise ModelMissing("CLIP wasn't found (Collection Sorter's copy is gone). Download it in Settings > AI models.")
        from .clip_tokenizer import ClipTokenizer

        device = get_settings().get("models.device", "auto")
        self.vision, self.provider = make_session(d / "vision_model.onnx", device)
        self.text, _ = make_session(d / "text_model.onnx", device)
        self.tok = ClipTokenizer()
        self.v_in = self.vision.get_inputs()[0].name
        self.v_out = pick_output(self.vision, "image_embeds")
        self.t_in = [i.name for i in self.text.get_inputs()]
        self.t_out = pick_output(self.text, "text_embeds")
        self._text_cache: dict[str, np.ndarray] = {}
        try:
            self.embed_images([np.zeros((self.SIZE, self.SIZE, 3), np.uint8)])
            self.embed_text(["a photo"])
        except Exception as e:
            log.warning("CLIP failed on %s (%s); using the processor", self.provider, e)
            self.vision, self.provider = make_session(d / "vision_model.onnx", "cpu")
            self.text, _ = make_session(d / "text_model.onnx", "cpu")
            self._text_cache.clear()
        log.info("CLIP ready on %s (from %s)", self.provider, d)

    def embed_images(self, rgb: list[np.ndarray]) -> np.ndarray:
        """(224,224,3) uint8 RGB images -> (n, 512) normalised embeddings."""
        if not rgb:
            return np.zeros((0, 512), np.float32)
        x = np.stack([(im.astype(np.float32) / 255.0 - self.MEAN) / self.STD for im in rgb]).transpose(0, 3, 1, 2)
        out = []
        for i in range(0, len(x), 8):
            out.append(self.vision.run([self.v_out], {self.v_in: np.ascontiguousarray(x[i:i + 8])})[0])
        return _l2(np.concatenate(out))

    def embed_text(self, texts: list[str]) -> np.ndarray:
        missing = [t for t in texts if t not in self._text_cache]
        if missing:
            ids, mask = self.tok.batch(missing)
            feeds = {}
            for name in self.t_in:
                feeds[name] = mask if "mask" in name else ids
            res = _l2(self.text.run([self.t_out], feeds)[0])
            for t, v in zip(missing, res):
                self._text_cache[t] = v
        return np.stack([self._text_cache[t] for t in texts])


_clap: Clap | None = None
_clip: Clip | None = None
_lock = threading.Lock()


def get_clap() -> Clap:
    global _clap
    with _lock:
        if _clap is None:
            _clap = Clap()
        return _clap


def get_clip() -> Clip:
    global _clip
    with _lock:
        if _clip is None:
            _clip = Clip()
        return _clip


def reset() -> None:
    global _clap, _clip
    with _lock:
        _clap = None
        _clip = None
