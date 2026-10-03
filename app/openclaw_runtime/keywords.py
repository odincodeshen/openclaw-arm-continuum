"""Keyword (BM25-style) vectors for Qdrant's sparse-vector search.

Dense embeddings miss what keyword matching finds easily: command names,
model codes, numbers, names, and English terms inside a Chinese question.
On one bot's own documents, the right passage was in the top 5 for 20% of
questions by vector and 93% by keywords; for Chinese questions about English
notes, 10% against 76% (scripts/rag_retrieval_eval.py).

Each passage gets a sparse vector named ``kw``:

- terms are lower-case Latin words and digits, plus character pairs for
  Chinese, Japanese and Korean (``rag_budget.term_list``);
- each term's index is a stable 31-bit hash;
- its value is the BM25 term-frequency part.

Qdrant applies the IDF part itself (``"modifier": "idf"``), so scores follow
the collection's own vocabulary. A query vector holds each query term once
with value 1.0.
"""

from __future__ import annotations

import zlib

from openclaw_runtime.rag_budget import term_list

VECTOR_NAME = "kw"
SPARSE_CONFIG = {VECTOR_NAME: {"modifier": "idf"}}
K1 = 1.2
B = 0.75
AVERAGE_TERMS = 250  # a typical ~1,800-character chunk; fixes BM25's length normalisation


def _index(term: str) -> int:
    return zlib.crc32(term.encode("utf-8")) & 0x7FFFFFFF


def term_counts(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for term in term_list(text):
        counts[term] = counts.get(term, 0) + 1
    return counts


def _sparse(weights: dict[int, float]) -> dict:
    indices = sorted(weights)
    return {"indices": indices, "values": [round(weights[i], 4) for i in indices]}


def document_vector(text: str) -> dict:
    """The passage's keyword vector ({"indices", "values"}; empty when the
    text has no terms)."""
    counts = term_counts(text)
    length = sum(counts.values()) or 1
    norm = K1 * (1 - B + B * length / AVERAGE_TERMS)
    weights: dict[int, float] = {}
    for term, tf in counts.items():
        index = _index(term)
        weights[index] = weights.get(index, 0.0) + tf * (K1 + 1) / (tf + norm)
    return _sparse(weights)


def query_vector(text: str) -> dict:
    return _sparse({_index(term): 1.0 for term in term_counts(text)})
