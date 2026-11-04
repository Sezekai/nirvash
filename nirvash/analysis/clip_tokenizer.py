"""CLIP byte-level BPE tokenizer (port of OpenAI CLIP's SimpleTokenizer, MIT licence).
Uses the vocab bundled in assets/ so CLIP works offline."""
from __future__ import annotations

import gzip
import html
from functools import lru_cache

import numpy as np
import regex as re

from ..core.paths import asset


@lru_cache()
def _bytes_to_unicode():
    bs = list(range(ord("!"), ord("~") + 1)) + list(range(ord("¡"), ord("¬") + 1)) + list(range(ord("®"), ord("ÿ") + 1))
    cs = bs[:]
    n = 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    return dict(zip(bs, [chr(c) for c in cs]))


def _pairs(word):
    return {(word[i], word[i + 1]) for i in range(len(word) - 1)}


class ClipTokenizer:
    SOT = 49406
    EOT = 49407

    def __init__(self):
        self.byte_encoder = _bytes_to_unicode()
        merges = gzip.open(asset("bpe_simple_vocab_16e6.txt.gz")).read().decode("utf-8").split("\n")
        merges = [tuple(m.split()) for m in merges[1:49152 - 256 - 2 + 1]]
        vocab = list(_bytes_to_unicode().values())
        vocab = vocab + [v + "</w>" for v in vocab]
        for m in merges:
            vocab.append("".join(m))
        vocab.extend(["<|startoftext|>", "<|endoftext|>"])
        self.encoder = dict(zip(vocab, range(len(vocab))))
        self.bpe_ranks = dict(zip(merges, range(len(merges))))
        self.cache = {"<|startoftext|>": "<|startoftext|>", "<|endoftext|>": "<|endoftext|>"}
        self.pat = re.compile(
            r"""<\|startoftext\|>|<\|endoftext\|>|'s|'t|'re|'ve|'m|'ll|'d|[\p{L}]+|[\p{N}]|[^\s\p{L}\p{N}]+""",
            re.IGNORECASE)

    def bpe(self, token: str) -> str:
        if token in self.cache:
            return self.cache[token]
        word = tuple(token[:-1]) + (token[-1] + "</w>",)
        pairs = _pairs(word)
        if not pairs:
            return token + "</w>"
        while True:
            bigram = min(pairs, key=lambda p: self.bpe_ranks.get(p, float("inf")))
            if bigram not in self.bpe_ranks:
                break
            first, second = bigram
            new_word = []
            i = 0
            while i < len(word):
                try:
                    j = word.index(first, i)
                    new_word.extend(word[i:j])
                    i = j
                except ValueError:
                    new_word.extend(word[i:])
                    break
                if word[i] == first and i < len(word) - 1 and word[i + 1] == second:
                    new_word.append(first + second)
                    i += 2
                else:
                    new_word.append(word[i])
                    i += 1
            word = tuple(new_word)
            if len(word) == 1:
                break
            pairs = _pairs(word)
        out = " ".join(word)
        self.cache[token] = out
        return out

    def encode(self, text: str) -> list[int]:
        text = " ".join(html.unescape(html.unescape(text)).split()).lower()
        ids = []
        for tok in re.findall(self.pat, text):
            tok = "".join(self.byte_encoder[b] for b in tok.encode("utf-8"))
            ids.extend(self.encoder[t] for t in self.bpe(tok).split(" "))
        return ids

    def batch(self, texts: list[str], length: int = 77) -> tuple[np.ndarray, np.ndarray]:
        ids = np.full((len(texts), length), self.EOT, np.int64)
        mask = np.zeros((len(texts), length), np.int64)
        for i, t in enumerate(texts):
            toks = [self.SOT] + self.encode(t)[: length - 2] + [self.EOT]
            ids[i, : len(toks)] = toks
            mask[i, : len(toks)] = 1
        return ids, mask
