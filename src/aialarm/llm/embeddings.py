"""Эмбеддинги для дедупа и семантического префильтра.

Абстракция `Embedder` с двумя реализациями:
- SentenceTransformerEmbedder — качественная многоязычная модель (прод, опц. зависимость).
- HashingTfidfEmbedder — детерминированный char-ngram TF-IDF без внешних зависимостей;
  достаточно для дедупа почти-идентичных заголовков и грубого префильтра офлайн.

Выбор реализации автоматический: если установлен sentence-transformers — берём его.
Trade-off: TF-IDF ловит лексические совпадения, но слабее на перефразировках; для
продового качества дедупа/фильтра поставьте extra `embeddings`.
"""
from __future__ import annotations

import hashlib
import math
import re
from functools import lru_cache
from typing import Protocol

import numpy as np

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_HASH_DIM = 512


def cosine(a: list[float] | np.ndarray, b: list[float] | np.ndarray) -> float:
    va, vb = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    na, nb = np.linalg.norm(va), np.linalg.norm(vb)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(va, vb) / (na * nb))


class Embedder(Protocol):
    def embed(self, text: str) -> list[float]: ...
    def embed_many(self, texts: list[str]) -> list[list[float]]: ...


def _char_ngrams(text: str, n: int = 3) -> list[str]:
    text = re.sub(r"\s+", " ", text.lower().strip())
    words = _TOKEN_RE.findall(text)
    grams: list[str] = list(words)  # слова как отдельные признаки
    for w in words:
        padded = f" {w} "
        grams += [padded[i : i + n] for i in range(len(padded) - n + 1)]
    return grams


class HashingTfidfEmbedder:
    """Хэширующий TF-IDF по char-ngram. Детерминирован, без обучения и зависимостей."""

    def __init__(self, dim: int = _HASH_DIM):
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        vec = np.zeros(self.dim, dtype=np.float32)
        grams = _char_ngrams(text)
        if not grams:
            return vec.tolist()
        for g in grams:
            h = int(hashlib.md5(g.encode("utf-8")).hexdigest(), 16)
            idx = h % self.dim
            sign = 1.0 if (h >> 1) & 1 else -1.0
            vec[idx] += sign
        # сглаживание частот (аналог tf) + L2-норма
        vec = np.sign(vec) * np.log1p(np.abs(vec))
        norm = np.linalg.norm(vec)
        if norm:
            vec /= norm
        return vec.tolist()

    def embed(self, text: str) -> list[float]:
        return self._vec(text)

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"):
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_name)

    def embed(self, text: str) -> list[float]:
        return self._model.encode(text, normalize_embeddings=True).tolist()

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return self._model.encode(texts, normalize_embeddings=True).tolist()


@lru_cache(maxsize=1)
def get_embedder() -> Embedder:
    try:
        import sentence_transformers  # noqa: F401

        return SentenceTransformerEmbedder()
    except Exception:
        return HashingTfidfEmbedder()
