"""Tests for ezkaraoke.letters (title first-letter index)."""

from ezkaraoke.letters import LETTERS, compute_letter


def test_letters_is_az_plus_hash():
    assert LETTERS == [chr(c) for c in range(ord("A"), ord("Z") + 1)] + ["#"]
    assert len(LETTERS) == 27


def test_cjk_title_uses_pinyin():
    assert compute_letter("晴天") == "Q"


def test_ascii_letter_uppercases():
    assert compute_letter("Hello") == "H"
    assert compute_letter("hello") == "H"


def test_digit_first_is_hash():
    assert compute_letter("123abc") == "#"


def test_empty_is_hash():
    assert compute_letter("") == "#"


def test_cjk_with_surrounding_spaces():
    assert compute_letter(" 稻香 ") == "D"


def test_non_pinyin_unicode_is_hash():
    assert compute_letter("Übung") == "#"
