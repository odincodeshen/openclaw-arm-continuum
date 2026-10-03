"""Fit /rag passages into a token budget.

On a CPU-only model, reading the prompt is what makes /rag slow (about 40
tokens/s on an Orion O6, so 3,800 tokens of passages take 95 s). Two
settings cap it, both off (0) by default:

- ``rag_passage_tokens``: each passage is cut to the run of sentences that
  shares the most words with the question.
- ``rag_context_tokens``: passages are then kept best-first -- files named in
  the question, then by search score -- until the total is reached. At least
  one passage is always kept.

The kept passages stay in their original sections and order, so the prompt,
the Sources line and the passage buttons all describe what the model read.
"""

from __future__ import annotations

import re

from openclaw_runtime.llm_client import estimate_tokens

# The "[label · source #n score=…] " header each passage gets in the prompt.
HEADER_TOKENS = 20

_SENTENCE_END = re.compile(r"(?<=[.!?。！？；;])\s+|(?<=[。！？；])|\n+")
_LATIN_WORD = re.compile(r"[A-Za-z0-9]{2,}")
_CJK = re.compile(r"[⺀-鿿豈-﫿가-힯]+")
_STOP = {
    "the", "and", "for", "are", "was", "what", "which", "who", "how", "does", "did", "with", "that", "this",
    "from", "about", "into", "have", "has", "can", "you", "your", "there", "their", "when", "where", "why",
    "is", "of", "to", "in", "on", "at", "it", "be", "as", "by", "or", "an", "do",
}


def terms(text: str) -> set[str]:
    """Words for matching: lower-case Latin words and digits, plus character
    pairs for Chinese / Japanese / Korean text, which has no spaces."""
    found = {w.lower() for w in _LATIN_WORD.findall(text)} - _STOP
    for run in _CJK.findall(text):
        found.update(run[i:i + 2] for i in range(len(run) - 1))
        if len(run) == 1:
            found.add(run)
    return found


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_END.split(text) if s and s.strip()]


def focus_passage(text: str, query: str, max_tokens: int) -> str:
    """The part of ``text`` closest to ``query`` within ``max_tokens``: the
    run of whole sentences sharing the most words with the question (the
    earliest on a tie). "… " / " …" mark where text was left out."""
    if max_tokens <= 0 or estimate_tokens(text) <= max_tokens:
        return text
    sentences = split_sentences(text)
    wanted = terms(query)
    scores = [len(terms(s) & wanted) for s in sentences]
    sizes = [estimate_tokens(s) for s in sentences]
    best = (-1, 0, 0)  # (score, start, end)
    for start in range(len(sentences)):
        total = score = 0
        end = start
        while end < len(sentences) and total + sizes[end] <= max_tokens:
            total += sizes[end]
            score += scores[end]
            end += 1
        if end == start:  # one sentence longer than the budget on its own
            score, end = scores[start], start + 1
        if score > best[0]:
            best = (score, start, end)
    _, start, end = best
    body = " ".join(sentences[start:end])
    cut_tail = end < len(sentences)
    if estimate_tokens(body) > max_tokens:  # a single over-long sentence
        body, cut_tail = _cut_to_tokens(body, max_tokens), True
    return ("… " if start > 0 else "") + body + (" …" if cut_tail else "")


def _cut_to_tokens(text: str, max_tokens: int) -> str:
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if estimate_tokens(text[:mid]) <= max_tokens:
            low = mid
        else:
            high = mid - 1
    return text[:low].rstrip()


def drop_weak_hits(
    labelled_hits: list[tuple[str, list[dict]]],
    margin: float,
    *,
    keep_labels: tuple[str, ...] = ("filename_match",),
) -> list[tuple[str, list[dict]]]:
    """Only hits scoring within ``margin`` of the best hit (cosine scores).
    Plain /rag takes a few hits from every category whether or not they
    match; this drops the ones far below the best. Files named in the
    question (``keep_labels``, which carry no score) always stay. 0 = off."""
    if margin <= 0:
        return labelled_hits
    scores = [float(h.get("score") or 0) for label, hits in labelled_hits if label not in keep_labels for h in hits]
    if not scores:
        return labelled_hits
    floor = max(scores) - margin
    return [
        (label, hits if label in keep_labels else [h for h in hits if float(h.get("score") or 0) >= floor])
        for label, hits in labelled_hits
    ]


def _text(hit: dict) -> str:
    return str((hit.get("payload") or {}).get("text") or "").strip()


def _with_text(hit: dict, text: str) -> dict:
    return {**hit, "payload": {**(hit.get("payload") or {}), "text": text}}


def fit_passages(
    labelled_hits: list[tuple[str, list[dict]]],
    query: str,
    *,
    context_tokens: int,
    passage_tokens: int,
    priority_labels: tuple[str, ...] = ("filename_match",),
) -> list[tuple[str, list[dict]]]:
    """``labelled_hits`` narrowed to the budget (see the module docstring).
    Unchanged when both limits are 0. Identical passages are kept once."""
    if context_tokens <= 0 and passage_tokens <= 0:
        return labelled_hits
    candidates = []  # (priority, -score, section, index, hit)
    seen_texts: set[str] = set()
    for section, (label, hits) in enumerate(labelled_hits):
        for index, hit in enumerate(hits):
            text = _text(hit)
            if not text or text in seen_texts:
                continue
            seen_texts.add(text)
            focused = focus_passage(text, query, passage_tokens)
            priority = 0 if label in priority_labels else 1
            candidates.append((priority, -float(hit.get("score") or 0), section, index,
                               _with_text(hit, focused) if focused != text else hit))
    candidates.sort(key=lambda c: (c[0], c[1], c[2], c[3]))
    kept: set[tuple[int, int]] = set()
    chosen: dict[tuple[int, int], dict] = {}
    total = 0
    for _priority, _score, section, index, hit in candidates:
        cost = estimate_tokens(_text(hit)) + HEADER_TOKENS
        if context_tokens > 0 and kept and total + cost > context_tokens:
            continue
        kept.add((section, index))
        chosen[(section, index)] = hit
        total += cost
    return [
        (label, [chosen[(section, index)] for index in range(len(hits)) if (section, index) in kept])
        for section, (label, hits) in enumerate(labelled_hits)
    ]
