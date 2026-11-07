"""Text processing utilities for medication names and descriptions."""

from __future__ import annotations

import re
import urllib.parse

from .constants import ARABIC_RE, NAME_STOPWORDS


def split_languages(value: str | None) -> tuple[str, str]:
    """Split mixed text into an English part and an Arabic part. Numbers stay in both."""
    if not value:
        return "", ""
    collapsed = re.sub(r"\s+", " ", value).strip(" \t\r\n-|:/")
    english = re.sub(r"\s+", " ", ARABIC_RE.sub(" ", collapsed)).strip(" -|:/")
    arabic = re.sub(r"\s+", " ", re.sub(r"[A-Za-z]+", " ", collapsed)).strip(" -|:/")
    if not re.search(r"[A-Za-z]", english):
        english = ""
    if not ARABIC_RE.search(arabic or ""):
        arabic = ""
    return english, arabic


def english_text(value: str | None) -> str:
    """Drop Arabic script and collapse whitespace. Keep Latin text, numbers, and units."""
    if not value:
        return ""
    cleaned = ARABIC_RE.sub(" ", value)
    cleaned = cleaned.replace("\xa0", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" \t\r\n-|:/")
    return cleaned


def normalize_name(value: str | None) -> str:
    text = english_text(value).upper()
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def strength_agrees(ministry_name: str, candidate: ProductRecord) -> bool:
    """Reject a name match when both sides state a strength and none of the numbers overlap."""
    wanted = {number for number in re.findall(r"\d+", ministry_name) if len(number) >= 2}
    available = {
        number
        for number in re.findall(
            r"\d+",
            " ".join([
                candidate.trade_name or "",
                candidate.composition or "",
                candidate.strength or "",
                candidate.pack or "",
            ]),
        )
        if len(number) >= 2
    }
    if wanted and available and wanted.isdisjoint(available):
        return False
    return True


def name_score(left: str, right: str, manufacturer: str = "") -> int:
    """Score how likely two trade names refer to the same medicine.

    Uses subset matching so that a manufacturer name like 'Panadol Extra'
    still matches the Ministry name 'Panadol' (score 80) rather than being
    rejected because the token sets differ.
    """
    a = normalize_name(left)
    b = normalize_name(right)
    if not a or not b:
        return 0
    if a == b:
        return 100
    maker_tokens = {
        token
        for token in normalize_name(manufacturer).split()
        if token not in {"PHARMACEUTICAL", "PHARMACEUTICALS", "INDUSTRIES", "INDUSTRY", "LABS", "LABORATORIES"}
    }
    tokens_a = {
        token for token in a.split()
        if token not in NAME_STOPWORDS and token not in maker_tokens and not token.isdigit() and len(token) > 1
    }
    tokens_b = {
        token for token in b.split()
        if token not in NAME_STOPWORDS and token not in maker_tokens and not token.isdigit() and len(token) > 1
    }
    if not tokens_a or not tokens_b:
        return 0

    # Exact token-set match (order-independent)
    if tokens_a == tokens_b:
        if max(len(token) for token in tokens_a) < 3:
            return 0
        return 90

    # Subset match: one name's meaningful tokens are contained in the other
    overlap = tokens_a & tokens_b
    if not overlap:
        return 0
    if max(len(token) for token in overlap) < 3:
        return 0

    smaller = min(tokens_a, tokens_b, key=len)
    larger = max(tokens_a, tokens_b, key=len)
    if smaller.issubset(larger):
        # The shorter name is fully contained in the longer one
        ratio = len(smaller) / len(larger)
        return int(60 + 30 * ratio)

    # Partial overlap: score proportional to shared tokens
    jaccard = len(overlap) / len(tokens_a | tokens_b)
    if jaccard < 0.4:
        return 0
    return int(50 + 40 * jaccard)


def ascii_url(value: str) -> str:
    """Percent-encode non-ASCII characters so stored links stay ASCII."""
    value = (value or "").strip()
    if not value:
        return ""
    parts = urllib.parse.urlsplit(value)
    if not parts.scheme:
        return value
    path = urllib.parse.quote(urllib.parse.unquote(parts.path), safe="/")
    query = urllib.parse.quote(urllib.parse.unquote(parts.query), safe="=&")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, query, ""))


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "unknown"
