"""
Shared title relevance scoring, used by both MatchService (final ranking)
and source clients that want to pre-filter their own results locally (see
e.g. app/client/source/movie/example_movie.py).

Scores are always ints, 0-100. Not ``fuzz.WRatio``: it blends in
``partial_token_set_ratio``, which compares only the tokens two strings
share - so a short query sharing even one common word with an unrelated
title can trivially score high. Instead we take the best of ratio/
token_sort_ratio, plus partial_ratio only when every query word actually
appears in the candidate.
"""
from rapidfuzz import fuzz, process

from app.util.text_utils import normalize_for_scoring

_ROMAN_NUMERALS = frozenset(
    "i ii iii iv v vi vii viii ix x xi xii xiii xiv xv xvi xvii xviii xix xx".split()
)

# Multiplier applied when the query and a candidate each name a specific,
# different installment number (e.g. "Episode I" vs "Episode II").
_INSTALLMENT_MISMATCH_PENALTY = 0.8


def _installment_numbers(normalized_text: str) -> set[str]:
    return {token for token in normalized_text.split() if token.isdigit() or token in _ROMAN_NUMERALS}


def _query_tokens_present_in_candidate(query_norm: str, candidate_norm: str) -> bool:
    return all(token in candidate_norm for token in query_norm.split())


def _composite_scorer(s1: str, s2: str, *, processor=None, score_cutoff=None) -> float:
    score = max(fuzz.ratio(s1, s2), fuzz.token_sort_ratio(s1, s2))
    if _query_tokens_present_in_candidate(s1, s2):
        score = max(score, fuzz.partial_ratio(s1, s2))

    query_numbers = _installment_numbers(s1)
    candidate_numbers = _installment_numbers(s2)
    if query_numbers and candidate_numbers and query_numbers.isdisjoint(candidate_numbers):
        score *= _INSTALLMENT_MISMATCH_PENALTY

    return score


def rank_titles(query: str, candidate_titles: list[str]) -> list[tuple[int, int]]:
    """Scores every candidate against the query and returns unfiltered
    ``(index, score)`` pairs, sorted best-first. Ties are broken by a plain
    ``fuzz.ratio`` pass."""
    if not candidate_titles:
        return []

    query_norm = normalize_for_scoring(query)
    normalized = [normalize_for_scoring(t) for t in candidate_titles]

    matches = process.extract(query_norm, normalized, scorer=_composite_scorer, score_cutoff=0, limit=None)

    ranked = []
    for _text, score, idx in matches:
        tiebreak = fuzz.ratio(query_norm, normalized[idx])
        ranked.append((idx, round(score), round(tiebreak)))

    ranked.sort(key=lambda item: (item[1], item[2]), reverse=True)
    return [(idx, score) for idx, score, _tiebreak in ranked]


def title_match_score(query_title: str, candidate_title: str) -> int:
    """Single pass/fail relevance score between two titles."""
    query_norm = normalize_for_scoring(query_title)
    candidate_norm = normalize_for_scoring(candidate_title)
    return round(_composite_scorer(query_norm, candidate_norm))
