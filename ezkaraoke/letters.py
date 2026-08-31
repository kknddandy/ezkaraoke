"""Title first-letter computation for the A–Z song index."""

from __future__ import annotations

LETTERS: list[str] = [chr(c) for c in range(ord("A"), ord("Z") + 1)] + ["#"]

try:
    from pypinyin import lazy_pinyin

    _PYPINYIN = True
except ImportError:
    _PYPINYIN = False


def compute_letter(title: str) -> str:
    """A-Z letter for a song title; '#' for anything else.

    ASCII letters map to themselves (upper-cased). CJK (and other
    non-ASCII) characters map via pinyin of the first character.
    Digits/symbols/no pinyin -> '#'.
    """
    text = title.strip()
    if not text:
        return "#"
    ch = text[0]
    if ch.isascii():
        up = ch.upper()
        return up if up.isalpha() else "#"
    if not _PYPINYIN:
        return "#"
    try:
        p = lazy_pinyin(ch)
    except Exception:  # noqa: BLE001 - pypinyin is best-effort
        return "#"
    if p and p[0]:
        c = p[0][0].upper()
        if c.isascii() and c.isalpha():
            return c
    return "#"


def pinyin_key(text: str) -> list[str]:
    """Pinyin-aware sort key for *text* (toneless, per character).

    Non-CJK characters are kept as-is; falls back to the raw text when
    pypinyin is unavailable. List form so keys compare lexicographically.
    """
    if not _PYPINYIN:
        return [text]
    try:
        return lazy_pinyin(text) or [text]
    except Exception:  # noqa: BLE001 - pypinyin is best-effort
        return [text]
