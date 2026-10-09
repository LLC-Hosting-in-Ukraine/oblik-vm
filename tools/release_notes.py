"""Нотатки випуску для GitHub Releases: розділ версії з CHANGELOG.md + як перевірити файл.

Запуск: python tools/release_notes.py v0.5.0 > NOTES.md
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"

FOOTER = """
---

**Як оновити:** замініть `OblikVM.exe` новою версією, базу (`oblik.db`) не чіпайте — програма
сама зробить резервну копію і оновить структуру бази.

**Перевірка файлу:** порівняйте SHA-256 з `SHA256SUMS.txt` — у PowerShell:
`Get-FileHash .\\OblikVM.exe -Algorithm SHA256`.

Програма — допоміжний інструмент, не офіційна система обліку; не вносьте інформацію з обмеженим
доступом (таємну, ДСК).
"""


def section(text: str, version: str) -> str:
    """Розділ «## X.Y.Z — …» до наступного «## »."""
    m = re.search(rf"^## {re.escape(version)}\b.*?$(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not m:
        raise SystemExit(f"У CHANGELOG.md немає розділу «## {version}»")
    return m.group(1).strip()


def main() -> int:
    version = sys.argv[1].lstrip("v") if len(sys.argv) > 1 else ""
    if not version:
        raise SystemExit("Вкажіть версію: release_notes.py v0.5.0")
    sys.stdout.reconfigure(encoding="utf-8")
    print(section(CHANGELOG.read_text(encoding="utf-8"), version))
    print(FOOTER)
    return 0


if __name__ == "__main__":
    sys.exit(main())
