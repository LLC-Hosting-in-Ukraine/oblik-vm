"""Текст, числа й гроші українською.

Домовленість щодо зберігання в базі:
  * гроші — цілі копійки (INTEGER), щоб не було похибок округлення;
  * кількість — цілі тисячні частки одиниці (INTEGER), щоб можна було
    обліковувати метри, кілограми, літри з трьома знаками після коми.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

_UK_ALPHABET = "абвгґдеєжзиіїйклмнопрстуфхцчшщьюя"
_UK_ORDER = {ch: i for i, ch in enumerate(_UK_ALPHABET)}


def uk_sort_key(text: str | None) -> tuple:
    """Ключ сортування за українською абеткою (ґ після г, є після е, і/ї після и)."""
    if not text:
        return ()
    key = []
    for ch in text.lower():
        if ch in _UK_ORDER:
            key.append((1, _UK_ORDER[ch]))
        elif ch == "'" or ch == "’" or ch == "ʼ":
            continue  # апостроф не впливає на порядок
        else:
            key.append((0 if ch.isdigit() or ch.isspace() else 2, ord(ch)))
    return tuple(key)


def uk_collate(a: str, b: str) -> int:
    ka, kb = uk_sort_key(a), uk_sort_key(b)
    return (ka > kb) - (ka < kb)


def lower(text: str | None) -> str | None:
    """Нижній регістр для пошуку (SQLite LIKE не вміє кирилицю)."""
    return text.lower() if text is not None else None


# --- Гроші --------------------------------------------------------------------

def _to_decimal(text: str) -> Decimal:
    cleaned = (
        text.replace(" ", "").replace(" ", "").replace("~", "")
        .replace("грн", "").replace(",", ".").strip()
    )
    if not cleaned:
        raise ValueError("порожнє значення")
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"«{text}» — не число") from exc


def parse_money(text: str | None) -> int | None:
    """«1 234,56» → 123456 копійок. Порожнє → None."""
    if text is None or not str(text).strip():
        return None
    value = _to_decimal(str(text))
    if value < 0:
        raise ValueError("сума не може бути від'ємною")
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_money(kop: int | None) -> str:
    """123456 → «1 234,56»."""
    if kop is None:
        return ""
    sign = "-" if kop < 0 else ""
    hrn, k = divmod(abs(kop), 100)
    return f"{sign}{hrn:,}".replace(",", " ") + f",{k:02d}"


# --- Кількість ----------------------------------------------------------------

QTY_SCALE = 1000


def parse_qty(text: str | None) -> int | None:
    """«2,5» → 2500 (тисячних). Порожнє → None."""
    if text is None or not str(text).strip():
        return None
    value = _to_decimal(str(text))
    if value < 0:
        raise ValueError("кількість не може бути від'ємною")
    scaled = value * QTY_SCALE
    if scaled != scaled.to_integral_value():
        raise ValueError("не більше трьох знаків після коми")
    return int(scaled)


def format_qty(milli: int | None) -> str:
    """2500 → «2,5»; 3000 → «3»."""
    if milli is None:
        return ""
    whole, frac = divmod(abs(milli), QTY_SCALE)
    sign = "-" if milli < 0 else ""
    if frac == 0:
        return f"{sign}{whole}"
    return f"{sign}{whole},{frac:03d}".rstrip("0")


# --- Особи --------------------------------------------------------------------

def person_short(last: str, first: str, middle: str | None = None) -> str:
    """Прізвище та ініціали: «Петренко П.П.»."""
    initials = first[:1] + "." + (middle[:1] + "." if middle else "")
    return f"{last} {initials}"


def person_signature(last: str, first: str) -> str:
    """Власне ім'я та прізвище, як у підписах документів: «Петро ПЕТРЕНКО»."""
    return f"{first} {last.upper()}"
