"""Документи Word за шаблонами (docxtpl).

Стандартні шаблони вшиті в програму (oblik/docx_templates/). При першому формуванні шаблон
копіюється в папку templates_docx/ поруч із програмою — там його можна відкрити у Word,
змінити оформлення, і програма заповнюватиме вже змінений. Поля — {{ назва }}, перелік —
FIELDS (показується на сторінці «Шаблони Word»). Готові файли зберігаються в output/<рік>/.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import date
from io import BytesIO
from pathlib import Path

from . import balances, documents, locations, orders, refs, settings
from .config import Paths, bundle_dir
from .documents import CATEGORY_TITLES
from .textutil import format_money, format_qty, person_short, person_signature
from .words import hrn_words, money_words, number_words, plural, qty_words

MONTHS = ["січня", "лютого", "березня", "квітня", "травня", "червня", "липня", "серпня",
          "вересня", "жовтня", "листопада", "грудня"]


@dataclass(frozen=True)
class Form:
    code: str
    title: str
    appendix: str
    file: str


FORMS = {f.code: f for f in [
    Form("nakladna", "Накладна (вимога)", "дод. 25", "nakladna.docx"),
    Form("act_oz", "Акт приймання-передачі основних засобів", "дод. 23", "act_oz.docx"),
    Form("act_zap", "Акт приймання-передачі запасів", "дод. 24", "act_zap.docx"),
    Form("naryad", "Наряд на видавання (приймання) військового майна", "дод. 5", "naryad.docx"),
    Form("act_condition", "Акт якісного (технічного) стану", "дод. 1 до Порядку списання",
         "act_condition.docx"),
]}

# Які форми можна сформувати для якого документа (акти — лише якщо є рядки свого класу).
DOC_FORMS = {
    "receipt": ["act_oz", "act_zap"],
    "transfer": ["nakladna", "act_oz"],
    "dispatch": ["nakladna", "act_oz", "act_zap"],
    "order": ["naryad"],
    "condition": ["act_condition"],
}

# Акт ОЗ — основні засоби й МНМА, акт запасів — решта (п. 1 пояснень до дод. 23, 24).
FIXED_CLASSES = {"fixed", "low_value"}
FORM_CLASSES = {"act_oz": lambda cls: cls in FIXED_CLASSES,
                "act_zap": lambda cls: cls not in FIXED_CLASSES}


def available_forms(conn: sqlite3.Connection, doc_id: int) -> list[Form]:
    """Форми для документа: акт пропонується, лише якщо в документі є рядки його класу."""
    doc = documents.get(conn, doc_id)
    classes = {r[0] for r in conn.execute(
        "SELECT n.accounting_class FROM document_lines l JOIN nomenclature n ON n.id = l.nomenclature_id "
        "WHERE l.document_id = ?", (doc_id,))}
    result = []
    for code in DOC_FORMS.get(doc["doc_type"], []):
        test = FORM_CLASSES.get(code)
        if test is None or any(test(c) for c in classes):
            result.append(FORMS[code])
    return result


class DocxError(Exception):
    pass


# --- Шаблони ---------------------------------------------------------------------

def default_template(form: Form) -> Path:
    return bundle_dir() / "oblik" / "docx_templates" / form.file


# Попередні стандартні версії шаблонів (SHA-256). Копію, яка збігається з однією з них,
# користувач не змінював — її можна сміливо оновити до нової стандартної.
PREVIOUS_STANDARD = {
    "nakladna.docx": {"08d5551ac40c339d0e811341896b01fcb013a7be67827d34b492b8fc82b39b76",
                      "4f56972fe2b0f7e485b323ce06cae9282045d33efb8b306c12eadf68e2818c17",
                      "ec81c36edda9af00af2da53edf2426d77d08f0a9c47be2da99bc117307714ac2"},
    "act_oz.docx": {"8de28d0226dd541b591cc1b8e93f01ed998e0c5bff2fe86df76624349aefbc06"},
    "act_zap.docx": {"1d5e691f3a8b7e8f6bd2e37ea5b8d0c7497d826029e9f4cb9379edff30409721"},
    "naryad.docx": {"c6193035479f352731ac9f798ebba4a49a4979f226c733e5620b3fce6d9e7fc5",   # альбомний
                    "b5638d75c89599ad7d2e86adb51db7050612d9fe376d97e4e744c5c864032cab",
                    "64cf4b240513826db7fa8d584fc225a77fc3be66dd7ba480943350b070e5f500"},
}
_REGISTRY = ".standard.json"   # які стандартні версії програма поклала в templates_docx


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _registry(paths: Paths) -> dict:
    try:
        return json.loads((paths.templates_docx / _REGISTRY).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _remember(paths: Paths, form: Form, sha: str) -> None:
    reg = _registry(paths)
    reg[form.file] = sha
    try:
        (paths.templates_docx / _REGISTRY).write_text(json.dumps(reg, indent=1), encoding="utf-8")
    except OSError:
        pass


def template_status(paths: Paths, form: Form) -> str:
    """missing — копії ще немає; standard — стандартна поточна; old_standard — стандартна
    попередньої версії (оновиться сама); modified — змінена користувачем."""
    user = paths.templates_docx / form.file
    if not user.exists():
        return "missing"
    sha = _sha(user)
    if sha == _sha(default_template(form)):
        return "standard"
    if sha == _registry(paths).get(form.file) or sha in PREVIOUS_STANDARD.get(form.file, set()):
        return "old_standard"
    return "modified"


def template_path(paths: Paths, form: Form) -> Path:
    """Шаблон користувача (templates_docx/). Немає або це незмінена стандартна копія старої
    версії програми — кладемо поточну стандартну. Змінений користувачем не чіпаємо."""
    user = paths.templates_docx / form.file
    status = template_status(paths, form)
    if status in ("missing", "old_standard"):
        try:
            paths.templates_docx.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(default_template(form), user)
            _remember(paths, form, _sha(user))
        except OSError:
            return default_template(form)
    return user


def reset_template(paths: Paths, form: Form) -> Path | None:
    """Повернути стандартний шаблон; змінений користувачем зберегти поруч як .bak."""
    user = paths.templates_docx / form.file
    backup = None
    if user.exists():
        backup = user.with_suffix(f".{date.today():%Y%m%d}.bak.docx")
        shutil.copyfile(user, backup)
    paths.templates_docx.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(default_template(form), user)
    _remember(paths, form, _sha(user))
    return backup


# --- Дані для шаблону --------------------------------------------------------------

def date_long(iso: str | None) -> str:
    """«08» жовтня 2026 року."""
    if not iso:
        return "«___» ____________ 20___ року"
    d = date.fromisoformat(iso[:10])
    return f"«{d.day:02d}» {MONTHS[d.month - 1]} {d.year} року"


def signer(conn, person_id: int | None) -> dict:
    """Дані підписанта; для невідомого — порожні рядки (заповнять від руки)."""
    empty = {"name": "", "position": "", "rank": "", "position_rank": "", "full": "", "short": "",
             "initials": ""}
    if not person_id:
        return empty
    p = conn.execute("SELECT * FROM persons WHERE id = ?", (person_id,)).fetchone()
    if p is None:
        return empty
    full = " ".join(x for x in (p["last_name"], p["first_name"], p["middle_name"]) if x)
    return {
        "name": person_signature(p["last_name"], p["first_name"]),
        "position": p["position"] or "",
        "rank": p["rank"] or "",
        "position_rank": ", ".join(x for x in (p["position"], p["rank"]) if x),
        "full": f"{p['rank']} {full}" if p["rank"] else full,
        "short": refs.Labels(conn).get("persons", person_id),
        "initials": person_short(p["last_name"], p["first_name"], p["middle_name"]),   # Прізвище І.П.
    }


def _mvo_of(conn, location_id: int) -> int | None:
    """Хто здає / приймає: МВО місця обліку або сама особа."""
    loc = locations.get(conn, location_id)
    if loc["kind"] == "unit":
        return conn.execute("SELECT mvo_person_id FROM units WHERE id = ?", (loc["unit_id"],)).fetchone()[0]
    if loc["kind"] == "person":
        return loc["person_id"]
    return None


def operation(conn, doc) -> str:
    """Вид операції для шапки накладної."""
    src = locations.kind(conn, doc["from_location_id"])
    dst = locations.kind(conn, doc["to_location_id"])
    if doc["doc_type"] == "dispatch":
        return "Передача іншій військовій частині"
    if src == "unit" and dst == "person":
        return "Видача в користування"
    if src == "person" and dst == "unit":
        return "Повернення з користування"
    if src == "person" and dst == "person":
        return "Передача між військовослужбовцями"
    return "Переміщення всередині військової частини"


def units_word(milli: int) -> str:
    """«одиниця / одиниці / одиниць» за кількістю; дробова — «одиниці» (дві цілих п'ять десятих одиниці)."""
    if milli % 1000:
        return "одиниці"
    return plural(milli // 1000, ("одиниця", "одиниці", "одиниць"))


def _numbers_note(item, note: str | None) -> str:
    parts = []
    if item is not None:
        if item["serial_no"]:
            parts.append(f"зав. № {item['serial_no']}")
        if item["inventory_no"]:
            parts.append(f"інв. № {item['inventory_no']}")
    if note:
        parts.append(note)
    return "; ".join(parts)


def _address(conn, location_id: int) -> str:
    """Поштова адреса контрагента (для «вантажоодержувач та його поштова адреса»)."""
    loc = locations.get(conn, location_id)
    if loc["kind"] != "counterparty":
        return ""
    row = conn.execute("SELECT address FROM counterparties WHERE id = ?", (loc["counterparty_id"],)).fetchone()
    return (row["address"] or "") if row else ""


def _party(conn, location_id: int, s: dict) -> str:
    """Хто передає / приймає (для актів): контрагент — його назва; своє місце — частина й місце."""
    loc = locations.get(conn, location_id)
    title = locations.title(conn, location_id, with_owner=False)
    if loc["kind"] in ("unit", "person"):
        own = s.get("subunit_name") or s.get("unit_name") or ""
        return f"{own}, {title}" if own else title
    return title


def context(conn: sqlite3.Connection, doc_id: int, form_code: str | None = None) -> dict:
    """Поля для шаблону. Для акта — лише рядки його класу (ОЗ або запаси), підсумки по них."""
    doc = documents.get(conn, doc_id)
    s = settings.load(conn)
    labels = refs.Labels(conn)
    lines = []
    total_kop = total_qty = 0
    keep = FORM_CLASSES.get(form_code or "")
    # Наряд: графи 8–9 — що фактично видано і за якими документами (з документів за нарядом).
    fact = orders.print_columns(conn, doc_id) if doc["doc_type"] == "order" else []
    n = 0
    for ln in documents.lines(conn, doc_id):
        nom = conn.execute("SELECT * FROM nomenclature WHERE id = ?", (ln["nomenclature_id"],)).fetchone()
        if keep is not None and not keep(nom["accounting_class"]):
            continue
        n += 1
        item = (conn.execute("SELECT * FROM items WHERE id = ?", (ln["item_id"],)).fetchone()
                if ln["item_id"] else None)
        value = balances.value_kop(ln["qty_m"], ln["price_kop"])
        total_kop += value
        total_qty += ln["qty_m"]
        req = ln["qty_requested_m"] if ln["qty_requested_m"] is not None else ln["qty_m"]
        lines.append({
            "n": n, "name": nom["name"], "code": nom["code"] or "",
            "batch": ln["batch_no"] or "",
            "year_made": (item["year_made"] or "") if item else "",
            "passport_no": (item["passport_no"] or "") if item else "",
            "wear_unit": "", "wear_total": "",   # знос програма не веде — заповнюється від руки
            "fact": fact[n - 1][0] if fact else "", "fact_docs": fact[n - 1][1] if fact else "",
            "nato_code": nom["nato_code"] or "", "uom": nom["uom"],
            "cat": CATEGORY_TITLES.get(ln["category"], ""),
            "price": format_money(ln["price_kop"]), "qty": format_qty(ln["qty_m"]),
            "req": format_qty(req), "sum": format_money(value),
            "serial_no": (item["serial_no"] or "") if item else "",
            "inventory_no": (item["inventory_no"] or "") if item else "",
            "note": _numbers_note(item, ln["note"]),
        })
    service_id = doc["service_id"] or s.get("default_service_id")
    if doc["status"] == "posted":
        mark = (f"Оброблено в електронній формі {documents.fmt_date(doc['posted_at'][:10])}, "
                f"виконавець: {s.get('operator_name') or '____________'}")
    else:
        mark = "Документ не проведено (чернетка) — відмітку про обробку буде проставлено після проведення."
    return {
        "unit_name": s.get("unit_name") or "", "unit_code": s.get("unit_code") or "",
        "subunit_name": s.get("subunit_name") or "", "edrpou": s.get("edrpou") or "",
        "place": s.get("place") or "",
        "doc_no": doc["doc_no"], "reg_no": doc["reg_no"],
        "doc_date": documents.fmt_date(doc["doc_date"]), "doc_date_long": date_long(doc["doc_date"]),
        "op_date": documents.fmt_date(doc["op_date"]), "op_date_long": date_long(doc["op_date"]),
        "valid_until": documents.fmt_date(doc["valid_until"]), "valid_until_long": date_long(doc["valid_until"]),
        "service": labels.get("services", service_id),
        "operation": operation(conn, doc),
        "basis": doc["basis"] or "", "note": doc["note"] or "",
        "recipient": signer(conn, doc["recipient_person_id"])["full"],
        "from_title": locations.title(conn, doc["from_location_id"], with_owner=False),
        "to_title": locations.title(conn, doc["to_location_id"], with_owner=False),
        "from_party": _party(conn, doc["from_location_id"], s),
        "to_party": _party(conn, doc["to_location_id"], s),
        "reg_date": documents.fmt_date((doc["posted_at"] or doc["doc_date"])[:10]),
        "to_address": _address(conn, doc["to_location_id"]),
        "order": documents.order_extra(doc),
        "copies": documents.copies(conn, doc),
        "copies_text": documents.copies_text(documents.copies(conn, doc)),
        "lines": lines,
        "count": len(lines), "count_words": number_words(len(lines), "n"),   # «одне найменування»
        "total_sum": format_money(total_kop), "total_sum_words": money_words(total_kop),
        "total_hrn_words": hrn_words(total_kop), "total_kop": f"{total_kop % 100:02d}",
        "total_qty": format_qty(total_qty), "total_qty_words": qty_words(total_qty),
        "total_units_word": units_word(total_qty),
        "head": signer(conn, s.get("service_head_person_id") or s.get("approver_person_id")),
        "approver": signer(conn, s.get("approver_person_id")),
        "service_head": signer(conn, s.get("service_head_person_id")),
        "finance": signer(conn, s.get("finance_person_id")),
        "mvo_from": signer(conn, _mvo_of(conn, doc["from_location_id"])),
        "mvo_to": signer(conn, _mvo_of(conn, doc["to_location_id"])),
        # Акт: МВО нашої сторони (приймає при надходженні, видає при передачі) й інша сторона.
        "mvo_our": signer(conn, _mvo_of(conn, doc["to_location_id"] if doc["doc_type"] == "receipt"
                                         else doc["from_location_id"])),
        "mvo_other": signer(conn, _mvo_of(conn, doc["from_location_id"] if doc["doc_type"] == "receipt"
                                           else doc["to_location_id"])),
        "our_action": "приймає" if doc["doc_type"] == "receipt" else "видає",
        "operator": s.get("operator_name") or "",
        "processing_mark": mark,
        "is_draft": doc["status"] == "draft",
        "cond": _condition(conn, doc) if doc["doc_type"] == "condition" else None,
    }


def _condition(conn, doc) -> dict:
    """Акт якісного (технічного) стану: рядки парами «списати | оприбуткувати» (рядок N — N-те
    списане і N-те оприбутковане), підсумки обох частин, комісія й висновки."""
    def row(ln):
        nom = conn.execute("SELECT * FROM nomenclature WHERE id = ?", (ln["nomenclature_id"],)).fetchone()
        item = (conn.execute("SELECT * FROM items WHERE id = ?", (ln["item_id"],)).fetchone()
                if ln["item_id"] else None)
        name = nom["name"]
        if item is not None and item["serial_no"]:
            name += f", зав. № {item['serial_no']}"
        elif item is not None and item["inventory_no"]:
            name += f", інв. № {item['inventory_no']}"
        return {"name": name, "code": nom["code"] or "", "uom": nom["uom"],
                "cat": CATEGORY_TITLES.get(ln["category"], ""), "qty": format_qty(ln["qty_m"]),
                "price": format_money(ln["price_kop"]),
                "sum": format_money(balances.value_kop(ln["qty_m"], ln["price_kop"])),
                "ynorm": ln["years_norm"] or "", "yfact": ln["years_fact"] or ""}

    all_lines = documents.lines(conn, doc["id"])
    out = [ln for ln in all_lines if not ln["direction"]]
    inn = [ln for ln in all_lines if ln["direction"]]
    blank = {k: "" for k in ("name", "code", "uom", "cat", "qty", "price", "sum", "ynorm", "yfact")}
    rows = []
    for i in range(max(len(out), len(inn))):
        o = row(out[i]) if i < len(out) else blank
        n = row(inn[i]) if i < len(inn) else blank
        rows.append({"n": i + 1, **o, **{f"in_{k}": v for k, v in n.items()}})

    def total(lns):
        return (format_qty(sum(ln["qty_m"] for ln in lns)) if lns else "",
                format_money(sum(balances.value_kop(ln["qty_m"], ln["price_kop"]) for ln in lns)) if lns else "")

    ex = documents.condition_extra(doc)
    s = settings.load(conn)
    return {
        "rows": rows,
        "out_qty": total(out)[0], "out_sum": total(out)[1],
        "in_qty": total(inn)[0], "in_sum": total(inn)[1],
        "group_name": ex["group_name"],
        "commission_conclusion": ex["commission_conclusion"],
        "senior_conclusion": ex["senior_conclusion"],
        "head": signer(conn, ex["commission_head"]),
        "members": [signer(conn, m) for m in ex["commission_members"]] or [signer(conn, None)] * 2,
        "senior": signer(conn, ex["senior_person"]),
        "keeper": signer(conn, _mvo_of(conn, doc["from_location_id"])),   # прийняв на відповідальне зберігання
        "unit": s.get("unit_code") or s.get("subunit_name") or s.get("unit_name") or "",
    }


FIELDS: list[tuple[str, str]] = [
    ("unit_name", "Найменування юридичної особи (налаштування)"),
    ("subunit_name", "Частина (підрозділ), де ведеться облік"),
    ("unit_code", "Умовне найменування"), ("edrpou", "Код ЄДРПОУ"),
    ("place", "Місце складання документів"),
    ("doc_no", "Номер документа"), ("reg_no", "Реєстраційний номер"),
    ("doc_date / doc_date_long", "Дата складання: 08.10.2026 / «08» жовтня 2026 року"),
    ("op_date / op_date_long", "Дата операції (так само)"),
    ("valid_until / valid_until_long", "Дійсний до (так само; порожньо — «___» … 20___ року)"),
    ("service", "Служба забезпечення"), ("operation", "Вид операції (видача, повернення, передача…)"),
    ("basis", "Підстава (мета)"), ("note", "Примітка до документа"),
    ("recipient", "Відповідальний одержувач: звання, прізвище, ім'я, по батькові"),
    ("from_title / to_title", "Передає / Приймає"),
    ("lines", "Рядки: {%tr for l in lines %} … {%tr endfor %}. У рядку: l.n, l.name, l.code, "
              "l.nato_code, l.uom, l.cat, l.price, l.req (відправлено), l.qty (прийнято), l.sum, "
              "l.serial_no, l.inventory_no, l.year_made, l.passport_no, l.batch, "
              "l.note (номери зразків і примітка). В акті — лише рядки його класу"),
    ("reg_date", "Дата реєстрації документа (дата проведення)"),
    ("from_party / to_party", "Для актів: хто передає / приймає — контрагент або частина з місцем обліку"),
    ("mvo_our / mvo_other / our_action", "Для актів: МВО нашої сторони, інша сторона, «приймає» або «видає»"),
    ("to_address", "Поштова адреса вантажоодержувача (з довідника «Контрагенти»)"),
    ("copies_text", "«Виконано у N примірниках: Примірник № 1 – до …» — по рядках (список примірників документа)"),
    ("order.transport_kind / .transport_no / .transport_doc_name / .transport_doc_no",
     "Наряд: вид і номер транспорту, найменування й номер транспортного документа"),
    ("order.dispatch_order / .guard_term / .guard_unit", "Наряд: порядок відправлення, строк прибуття варти, від якої частини"),
    ("<особа>.initials", "Прізвище та ініціали: «Петренко П.П.» (для підписів наряду)"),
    ("count / count_words", "Кількість найменувань (рядків): 1 / одне"),
    ("total_sum", "Сума всього: 1 234,56"),
    ("total_sum_words", "Сума прописом повністю: одна тисяча двісті тридцять чотири гривні 56 копійок"),
    ("total_hrn_words / total_kop", "Для «на суму ___ грн __ коп.»: гривні прописом / копійки цифрами"),
    ("total_qty / total_qty_words", "Кількість усього: 125 / сто двадцять п'ять"),
    ("total_units_word", "Слово «одиниця» у потрібній формі: одиниця / одиниці / одиниць"),
    ("head, approver, service_head, finance", "Керівник (начальник служби або командир), командир, "
                                               "начальник служби, начальник фінансової служби"),
    ("mvo_from, mvo_to", "МВО, що здав / прийняв (МВО місця обліку або сама особа)"),
    ("<особа>.name / .position / .rank / .position_rank / .full", "Наприклад, head.name — «Іван ПЕТРЕНКО», "
                                                                 "head.position_rank — посада, звання"),
    ("cond.rows", "Акт якісного стану: {%tr for r in cond.rows %} … — r.n, графи 2–10 (r.name, r.code, r.uom, "
                  "r.cat, r.qty, r.price, r.sum, r.ynorm, r.yfact) і 11–17 (r.in_name, r.in_code, r.in_uom, "
                  "r.in_cat, r.in_qty, r.in_price, r.in_sum)"),
    ("cond.out_qty / .out_sum / .in_qty / .in_sum", "Акт якісного стану: «Усього» — списати / оприбуткувати"),
    ("cond.group_name / .commission_conclusion / .senior_conclusion",
     "Акт якісного стану: найменування майна в заголовку, висновок комісії, висновок старшого начальника"),
    ("cond.head / cond.members / cond.senior / cond.keeper",
     "Акт якісного стану: голова комісії, члени (список: {% for m in cond.members %}), старший начальник, "
     "хто прийняв отримане на відповідальне зберігання (МВО місця)"),
    ("cond.unit", "Акт якісного стану: військова частина (умовне найменування з налаштувань)"),
    ("operator", "Хто веде облік (налаштування)"),
    ("processing_mark", "Відмітка про обробку: дата проведення і виконавець (п. 9 розд. II Інструкції)"),
]


# --- Формування -------------------------------------------------------------------

def _safe(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "-", text).strip()


def render(conn: sqlite3.Connection, paths: Paths, doc_id: int, form_code: str) -> tuple[bytes, str, Path | None]:
    """Заповнити шаблон. Повертає (вміст .docx, ім'я файлу, куди збережено копію або None)."""
    from docxtpl import DocxTemplate

    doc = documents.get(conn, doc_id)
    if form_code not in {f.code for f in available_forms(conn, doc_id)}:
        raise DocxError("Для цього документа такої форми немає")
    form = FORMS[form_code]
    tpl = DocxTemplate(str(template_path(paths, form)))
    try:
        tpl.render(context(conn, doc_id, form_code))
    except Exception as exc:  # помилка в шаблоні, зміненому користувачем
        raise DocxError(f"Не вдалося заповнити шаблон «{form.file}»: {exc}. Перевірте поля в "
                        "шаблоні або поверніть стандартний.") from exc
    buf = BytesIO()
    tpl.save(buf)
    data = buf.getvalue()
    name = _safe(f"{form.title} № {doc['doc_no']} від {documents.fmt_date(doc['doc_date'])}"
                 + (" (чернетка)" if doc["status"] == "draft" else "")) + ".docx"
    saved = None
    try:
        folder = paths.output / str(doc["reg_year"])
        folder.mkdir(parents=True, exist_ok=True)
        saved = folder / name
        saved.write_bytes(data)
    except OSError:
        saved = None
    return data, name, saved
