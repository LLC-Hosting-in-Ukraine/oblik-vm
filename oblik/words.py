"""Числа прописом українською: суми (гривні, копійки) і кількість.

    number_words(1234)            -> «одна тисяча двісті тридцять чотири»
    money_words(123456)           -> «одна тисяча двісті тридцять чотири гривні 56 копійок»
    qty_words(2500)               -> «дві цілих п'ять десятих»  (кількість у тисячних)
"""
from __future__ import annotations

_UNITS_M = ["", "один", "два", "три", "чотири", "п'ять", "шість", "сім", "вісім", "дев'ять"]
_UNITS_F = ["", "одна", "дві", "три", "чотири", "п'ять", "шість", "сім", "вісім", "дев'ять"]
_UNITS_N = ["", "одне", "два", "три", "чотири", "п'ять", "шість", "сім", "вісім", "дев'ять"]   # «одне найменування»
_TEENS = ["десять", "одинадцять", "дванадцять", "тринадцять", "чотирнадцять", "п'ятнадцять",
          "шістнадцять", "сімнадцять", "вісімнадцять", "дев'ятнадцять"]
_TENS = ["", "", "двадцять", "тридцять", "сорок", "п'ятдесят", "шістдесят", "сімдесят",
         "вісімдесят", "дев'яносто"]
_HUNDREDS = ["", "сто", "двісті", "триста", "чотириста", "п'ятсот", "шістсот", "сімсот",
             "вісімсот", "дев'ятсот"]

# (форма для 1, для 2–4, для 5+ і 11–14), рід
_SCALES = [
    (("тисяча", "тисячі", "тисяч"), "f"),
    (("мільйон", "мільйони", "мільйонів"), "m"),
    (("мільярд", "мільярди", "мільярдів"), "m"),
]


def plural(n: int, forms: tuple[str, str, str]) -> str:
    """Форма слова за числом: 1 гривня, 2 гривні, 5 гривень, 11 гривень, 21 гривня."""
    n = abs(n) % 100
    if 11 <= n <= 14:
        return forms[2]
    last = n % 10
    if last == 1:
        return forms[0]
    if 2 <= last <= 4:
        return forms[1]
    return forms[2]


def _triad(n: int, gender: str) -> list[str]:
    words = [_HUNDREDS[n // 100]]
    rest = n % 100
    if 10 <= rest <= 19:
        words.append(_TEENS[rest - 10])
    else:
        words.append(_TENS[rest // 10])
        words.append({"f": _UNITS_F, "n": _UNITS_N}.get(gender, _UNITS_M)[rest % 10])
    return [w for w in words if w]


def number_words(n: int, gender: str = "f") -> str:
    """Ціле невід'ємне число словами. gender — рід останнього слова: 'f' (гривня, одиниця),
    'm' (рядок) або 'n' (найменування: «одне», «два»)."""
    if n < 0:
        raise ValueError("від'ємне число")
    if n == 0:
        return "нуль"
    parts: list[str] = []
    triads = []
    while n:
        triads.append(n % 1000)
        n //= 1000
    if len(triads) > len(_SCALES) + 1:
        raise ValueError("завелике число")
    for i in range(len(triads) - 1, -1, -1):
        t = triads[i]
        if not t:
            continue
        if i == 0:
            parts += _triad(t, gender)
        else:
            forms, g = _SCALES[i - 1]
            parts += _triad(t, g)
            parts.append(plural(t, forms))
    return " ".join(parts)


def money_words(kop: int) -> str:
    """«одна тисяча двісті тридцять чотири гривні 56 копійок»."""
    hrn, k = divmod(kop, 100)
    return (f"{number_words(hrn)} {plural(hrn, ('гривня', 'гривні', 'гривень'))} "
            f"{k:02d} {plural(k, ('копійка', 'копійки', 'копійок'))}")


def hrn_words(kop: int) -> str:
    """Лише гривні словами — для форм «на суму ___ грн __ коп.»."""
    return number_words(kop // 100)


_FRACTIONS = {1: ("десята", "десятих", "десятих"), 2: ("сота", "сотих", "сотих"),
              3: ("тисячна", "тисячних", "тисячних")}


def qty_words(milli: int) -> str:
    """Кількість (у тисячних) словами: «сто двадцять п'ять», «дві цілих п'ять десятих»."""
    whole, frac = divmod(milli, 1000)
    if not frac:
        return number_words(whole)
    digits = f"{frac:03d}".rstrip("0")
    places, value = len(digits), int(digits)
    whole_part = f"{number_words(whole)} {plural(whole, ('ціла', 'цілих', 'цілих'))}"
    return f"{whole_part} {number_words(value)} {plural(value, _FRACTIONS[places])}"


def capitalize(text: str) -> str:
    return text[:1].upper() + text[1:]
