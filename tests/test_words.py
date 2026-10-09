"""Числа прописом."""
from __future__ import annotations

import pytest

from oblik.words import money_words, number_words, plural, qty_words


@pytest.mark.parametrize("n, text", [
    (0, "нуль"), (1, "одна"), (2, "дві"), (11, "одинадцять"), (21, "двадцять одна"),
    (100, "сто"), (215, "двісті п'ятнадцять"), (1000, "одна тисяча"), (2000, "дві тисячі"),
    (5000, "п'ять тисяч"), (11000, "одинадцять тисяч"), (21001, "двадцять одна тисяча одна"),
    (1234, "одна тисяча двісті тридцять чотири"),
    (1_000_000, "один мільйон"), (2_500_000, "два мільйони п'ятсот тисяч"),
    (1_002_003, "один мільйон дві тисячі три"),
])
def test_number_words(n, text):
    assert number_words(n) == text


def test_masculine():
    assert number_words(2, "m") == "два" and number_words(21, "m") == "двадцять один"


def test_plural():
    forms = ("гривня", "гривні", "гривень")
    assert [plural(n, forms) for n in (1, 2, 5, 11, 12, 21, 22, 25, 111, 101)] == [
        "гривня", "гривні", "гривень", "гривень", "гривень", "гривня", "гривні", "гривень",
        "гривень", "гривня"]


def test_money_words():
    assert money_words(123456) == "одна тисяча двісті тридцять чотири гривні 56 копійок"
    assert money_words(101) == "одна гривня 01 копійка"
    assert money_words(500) == "п'ять гривень 00 копійок"
    assert money_words(32236250) == "триста двадцять дві тисячі триста шістдесят дві гривні 50 копійок"


def test_qty_words():
    assert qty_words(125000) == "сто двадцять п'ять"
    assert qty_words(2500) == "дві цілих п'ять десятих"
    assert qty_words(1250) == "одна ціла двадцять п'ять сотих"
    assert qty_words(305125) == "триста п'ять цілих сто двадцять п'ять тисячних"


def test_neuter():
    assert number_words(1, "n") == "одне" and number_words(2, "n") == "два"
    assert number_words(21, "n") == "двадцять одне"
