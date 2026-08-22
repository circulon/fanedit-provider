"""
Text utilities that replace Plex-framework built-ins used by the original
agent (Contents/Code/__init__.py and common.py):

- ``String.StripDiacritics``  -> :func:`strip_diacritics`
- ``String.LevenshteinDistance`` -> :func:`levenshtein_distance`
- ``String.Quote(..., usePlus=True)`` -> handled via ``urllib.parse.quote_plus``
- ``common.parse_flexible_date`` -> :func:`parse_flexible_date` (ported almost verbatim)
"""
import re
import unicodedata
from datetime import date
from functools import lru_cache


def strip_diacritics(text: str) -> str:
    """Remove diacritical marks, e.g. 'Amélie' -> 'Amelie'."""
    if not text:
        return text
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalize_for_scoring(text: str) -> str:
    """Case/diacritic/punctuation-insensitive form used before any fuzzy
    title comparison (see app/util/scoring.py) - so e.g. "Star Wars:
    Episode I" and "Star Wars Episode I" are the same string as far as
    matching is concerned, not a 1-2 point penalty for a colon.

    rapidfuzz's own ``utils.default_process`` does something similar but
    doesn't collapse the whitespace left behind by a removed punctuation
    mark (a title with a colon ends up with a double space where the
    query - which never had the colon - has a single one), which is
    exactly the gap that would otherwise cost a couple of points on an
    otherwise-exact match. This collapses that whitespace too.
    """
    if not text:
        return ""
    text = strip_diacritics(text).lower()
    text = _NON_ALNUM_RE.sub(" ", text)
    return text.strip()


@lru_cache(maxsize=4096)
def levenshtein_distance(a: str, b: str) -> int:
    """Classic Levenshtein edit distance between two strings."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    previous_row = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current_row = [i]
        for j, char_b in enumerate(b, start=1):
            insertions = previous_row[j] + 1
            deletions = current_row[j - 1] + 1
            substitutions = previous_row[j - 1] + (char_a != char_b)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


def slugify(value: str) -> str:
    """Produce an ASCII, GUID-safe slug (``[a-zA-Z0-9_-]``) from free text."""
    value = strip_diacritics(value or "")
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value or "unknown"


_DATE_FORMATS = (
    "%B %Y",  # Full month: "January 2022"
    "%b %Y",  # Abbreviated month: "Dec 2022"
    "%b. %Y",  # Abbreviated month with period: "Aug. 2022"
    "%Y",  # Year only: "2008"
)


def parse_flexible_date(date_string: str | None) -> date | None:
    """Parse dates in various formats to a :class:`datetime.date`.

    Ported directly from ``Contents/Code/common.py::parse_flexible_date`` in
    the original Plex agent, translated from Python 2 to Python 3.
    """
    from datetime import datetime

    if not date_string:
        return None

    date_string = date_string.strip()

    # Handle year range format (e.g., "2003/2004" or "2012 / 2013 / 2014")
    if "/" in date_string:
        parts = [part.strip() for part in date_string.split("/")]
        is_valid_year_format = all(re.match(r"^\d{4}$", part) for part in parts)
        if is_valid_year_format and parts:
            latest_year = parts[-1]
            return datetime.strptime(latest_year, "%Y").date()

    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(date_string, fmt).date()
        except ValueError:
            continue

    raise ValueError(f"Could not parse date string: '{date_string}'")
