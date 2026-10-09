"""Документи у PDF — однаково виглядають на будь-якому ПК, Word не потрібен.

Дані ті самі, що й для Word (docx_out.context), тому зміст PDF і .docx збігається.
Шрифт — Times New Roman із системи (C:\\Windows\\Fonts), в програму не вшивається;
на Linux — метрично сумісний Liberation Serif.
"""
from __future__ import annotations

import os
import sqlite3
from io import BytesIO
from pathlib import Path

from . import documents, docx_out
from .config import Paths

FONT, FONT_B, FONT_I = "Serif", "Serif-Bold", "Serif-Italic"
_FONT_FILES = [
    # (звичайний, жирний, курсив)
    ("times.ttf", "timesbd.ttf", "timesi.ttf"),
    ("LiberationSerif-Regular.ttf", "LiberationSerif-Bold.ttf", "LiberationSerif-Italic.ttf"),
]
_FONT_DIRS = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts",
              Path("/usr/share/fonts/truetype/liberation"), Path("/usr/share/fonts/liberation"),
              Path("/Library/Fonts"), Path("/System/Library/Fonts/Supplemental")]
_registered = False


class PdfError(Exception):
    pass


def _register_fonts() -> None:
    global _registered
    if _registered:
        return
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.fonts import addMapping
    for names in _FONT_FILES:
        for folder in _FONT_DIRS:
            files = [folder / n for n in names]
            if all(f.exists() for f in files):
                for alias, f in zip((FONT, FONT_B, FONT_I), files):
                    pdfmetrics.registerFont(TTFont(alias, str(f)))
                addMapping(FONT, 0, 0, FONT)
                addMapping(FONT, 1, 0, FONT_B)
                addMapping(FONT, 0, 1, FONT_I)
                addMapping(FONT, 1, 1, FONT_B)
                _registered = True
                return
    raise PdfError("Не знайдено шрифт Times New Roman (або Liberation Serif) — PDF сформувати "
                   "не вдалося. Скористайтеся документом Word.")


def _esc(text) -> str:
    return (str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


class _Kit:
    """Стилі й дрібні будівельні блоки сторінки."""

    def __init__(self):
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
        base = ParagraphStyle("base", fontName=FONT, fontSize=11, leading=13)
        self.s = {
            "p": base,
            "small": ParagraphStyle("small", parent=base, fontSize=8, leading=9.5),
            "right_small": ParagraphStyle("rs", parent=base, fontSize=8, leading=9.5, alignment=TA_RIGHT),
            "cap": ParagraphStyle("cap", parent=base, fontName=FONT_I, fontSize=7, leading=8, alignment=TA_CENTER),
            "cap_l": ParagraphStyle("capl", parent=base, fontName=FONT_I, fontSize=7, leading=8, alignment=TA_LEFT),
            "center": ParagraphStyle("c", parent=base, alignment=TA_CENTER),
            "right": ParagraphStyle("r", parent=base, alignment=TA_RIGHT),
            "title": ParagraphStyle("t", parent=base, fontName=FONT_B, fontSize=14, leading=17, alignment=TA_CENTER,
                                    spaceBefore=6, spaceAfter=2),
            "th": ParagraphStyle("th", parent=base, fontName=FONT_B, fontSize=7, leading=8, alignment=TA_CENTER),
            "th_n": ParagraphStyle("thn", parent=base, fontSize=7, leading=8, alignment=TA_CENTER),
            "td": ParagraphStyle("td", parent=base, fontSize=9, leading=10.5),
            "td_r": ParagraphStyle("tdr", parent=base, fontSize=9, leading=10.5, alignment=TA_RIGHT),
            "td_c": ParagraphStyle("tdc", parent=base, fontSize=9, leading=10.5, alignment=TA_CENTER),
            "mark": ParagraphStyle("m", parent=base, fontName=FONT_I, fontSize=8.5, leading=10),
        }

    def p(self, text, style="p"):
        from reportlab.platypus import Paragraph
        return Paragraph(text, self.s[style])

    def gap(self, h=4):
        from reportlab.platypus import Spacer
        return Spacer(1, h)

    def field(self, label: str, value) -> object:
        """«Підстава (мета) <u>текст</u>»."""
        return self.p(f"{label} <u>{_esc(value) or '&nbsp;' * 40}</u>")

    def line(self, label: str, total_cm: float, label_cm: float | None = None, value: str = ""):
        """«Висновок ________» — підпис і лінія до правого краю (не переноситься)."""
        from reportlab.lib.units import cm
        from reportlab.platypus import Table, TableStyle
        from reportlab.pdfbase.pdfmetrics import stringWidth
        lw = label_cm * cm if label_cm else stringWidth(label, FONT, 11) + 6
        t = Table([[self.p(label), self.p(_esc(value))]], colWidths=[lw, total_cm * cm - lw], hAlign="LEFT")
        t.setStyle(TableStyle([("LINEBELOW", (1, 0), (1, 0), 0.5, "black"), ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                               ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
        return t

    def signature(self, role: str, who: dict, width_cm=(4.6, 5.4, 3.0, 5.0), with_position=True):
        from reportlab.lib.units import cm
        from reportlab.platypus import Table, TableStyle
        pos = _esc(who.get("position_rank")) if with_position else ""
        rows = [[self.p(role, "td"), self.p(pos, "td"), self.p("____________", "td_c"), self.p(_esc(who.get("name")), "td")],
                ["", self.p("(посада, звання)" if with_position else "", "cap_l"), self.p("(підпис)", "cap"),
                 self.p("(власне ім'я та прізвище)", "cap_l")]]
        t = Table(rows, colWidths=[w * cm for w in width_cm])
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 1), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                               ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
        return t


def _draft_watermark(canvas, _doc):
    canvas.saveState()
    canvas.setFont(FONT_B, 70)
    canvas.setFillGray(0.88)
    canvas.translate(300, 420)
    canvas.rotate(45)
    canvas.drawCentredString(0, 0, "ЧЕРНЕТКА")
    canvas.restoreState()


def nakladna(ctx: dict) -> bytes:
    """Накладна (вимога), дод. 25 до Інструкції."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

    k = _Kit()
    story = [k.p("Додаток 25 до Інструкції з обліку військового майна у Збройних Силах України "
                 "(пункт 24 розділу IV)", "right_small"), k.gap(6)]

    left = [k.p(f"<b>{_esc(ctx['unit_name'])}</b>"), k.p("(найменування юридичної особи)", "cap_l")]
    if ctx["subunit_name"]:
        left.append(k.p(_esc(ctx["subunit_name"])))
    left.append(k.p(f"Код згідно з ЄДРПОУ {_esc(ctx['edrpou'])}"))
    head = Table([[left, k.p(f"Дійсна до {ctx['valid_until_long']}", "td_r")]], colWidths=[10 * cm, 8 * cm])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [head,
              k.p(f"Накладна (вимога) № {_esc(ctx['doc_no'])}", "title"),
              k.p(_esc(ctx["place"]) or "&nbsp;", "center"), k.p("(місце складання)", "cap"),
              k.p(ctx["doc_date_long"], "center"), k.p("(дата складання)", "cap"), k.gap(4),
              k.p(f"Дата операції {ctx['op_date_long']}"), k.gap(3)]
    for label, key in (("Служба забезпечення", "service"), ("Вид операції", "operation"),
                       ("Підстава (мета)", "basis"), ("Відповідальний одержувач", "recipient"),
                       ("Передає", "from_title"), ("Приймає", "to_title")):
        story.append(k.field(label, ctx[key]))
    story.append(k.gap(6))

    widths = [0.7, 3.8, 1.7, 1.3, 1.4, 1.7, 1.6, 1.6, 1.8, 2.4]  # разом 18 см
    th, n = k.s["th"], k.s["th_n"]
    rows = [
        [k.p("№ з/п", "th"), k.p("Назва військового майна або однорідна група (вид)", "th"),
         k.p("Код номен&shy;клатури", "th"), k.p("Одиниця виміру", "th"), k.p("Категорія (сорт)", "th"),
         k.p("Вартість за одиницю", "th"), k.p("Кількість", "th"), "", k.p("Сума", "th"), k.p("Примітка", "th")],
        ["", "", "", "", "", "", k.p("відправлено (вимага&shy;ється)", "th_n"),
         k.p("прийнято (відпущено)", "th_n"), "", ""],
        [k.p(str(i), "th_n") for i in range(1, 11)],
    ]
    del th, n
    for ln in ctx["lines"]:
        rows.append([k.p(str(ln["n"]), "td_c"), k.p(_esc(ln["name"]), "td"), k.p(_esc(ln["code"]), "td"),
                     k.p(_esc(ln["uom"]), "td_c"), k.p(_esc(ln["cat"]), "td_c"), k.p(ln["price"], "td_r"),
                     k.p(ln["req"], "td_r"), k.p(ln["qty"], "td_r"), k.p(ln["sum"], "td_r"),
                     k.p(_esc(ln["note"]), "td")])
    rows.append([k.p("<b>Всього</b>", "td")] + [""] * 7 + [k.p(f"<b>{ctx['total_sum']}</b>", "td_r"), ""])
    last = len(rows) - 1
    t = Table(rows, colWidths=[w * cm for w in widths], repeatRows=3)
    style = [("GRID", (0, 0), (-1, -1), 0.5, colors.black), ("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("VALIGN", (0, 0), (-1, 1), "MIDDLE"),
             ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
             ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
             ("SPAN", (6, 0), (7, 0)), ("SPAN", (0, last), (7, last))]
    style += [("SPAN", (c, 0), (c, 1)) for c in (0, 1, 2, 3, 4, 5, 8, 9)]
    t.setStyle(TableStyle(style))
    story += [t, k.gap(10), k.signature("Керівник (посадова особа, начальник служби)", ctx["head"]), k.gap(8),
              k.p(f"Всього передано <u>{_esc(ctx['total_qty_words'])}</u> {ctx['total_units_word']},"),
              k.p("(кількість прописом)", "cap"),
              k.p(f"на суму <u>{_esc(ctx['total_hrn_words'])}</u> грн {ctx['total_kop']} коп."),
              k.p("(сума прописом)", "cap"), k.gap(8),
              k.p("Матеріально відповідальні особи:"),
              k.signature("здав:", ctx["mvo_from"]), k.gap(3), k.signature("прийняв:", ctx["mvo_to"]), k.gap(8),
              k.p("Відмітка фінансово-економічного органу про відображення у регістрах бухгалтерського обліку:",
                  "small")]
    fin = Table([[k.p("Назва облікового регістру", "th_n"),
                  k.p("За дебетом рахунку (субрахунку, коду аналітичного обліку)", "th_n"),
                  k.p("За кредитом рахунку (субрахунку, коду аналітичного обліку)", "th_n"), k.p("Сума", "th_n")],
                 ["", "", "", ""], ["", "", "", ""]],
                colWidths=[5 * cm, 5 * cm, 5 * cm, 3 * cm], rowHeights=[None, 14, 14])
    fin.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
    story += [fin, k.gap(5),
              k.p("Особа, яка відобразила господарську операцію в бухгалтерському обліку", "small"),
              k.signature("", {}, width_cm=(0.4, 6.6, 3.0, 8.0)),
              k.p("«___» __________________ 20___ року", "small"), k.gap(6),
              k.signature("Помічник командира з фінансово-економічної роботи — начальник фінансової служби",
                          ctx["finance"], width_cm=(7.0, 3.0, 3.0, 5.0), with_position=False),
              k.gap(6)] + [k.p(_esc(row), "small") for row in ctx["copies_text"].splitlines()] + [
              k.gap(6), k.p(_esc(ctx["processing_mark"]), "mark")]

    buf = BytesIO()
    pad = 6  # внутрішній відступ рамки reportlab, pt
    pdf = SimpleDocTemplate(buf, pagesize=A4, leftMargin=2 * cm - pad, rightMargin=1 * cm - pad,
                            topMargin=1.3 * cm, bottomMargin=1.3 * cm,
                            title=f"Накладна (вимога) № {ctx['doc_no']}", author=ctx["unit_name"])
    on_page = _draft_watermark if ctx["is_draft"] else (lambda c, d: None)
    pdf.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue()


def _finance_block(k, ctx, wide: bool) -> list:
    """Відмітка фінансово-економічного органу і підпис начальника фінансової служби."""
    from reportlab.lib import colors
    from reportlab.lib.units import cm
    from reportlab.platypus import Table, TableStyle
    w = [6.5, 7.5, 7.5, 5.2] if wide else [5, 5, 5, 3]
    fin = Table([[k.p("Назва облікового регістру", "th_n"),
                  k.p("За дебетом рахунку (субрахунку, коду аналітичного обліку)", "th_n"),
                  k.p("За кредитом рахунку (субрахунку, коду аналітичного обліку)", "th_n"), k.p("Сума", "th_n")],
                 ["", "", "", ""], ["", "", "", ""]],
                colWidths=[x * cm for x in w], rowHeights=[None, 13, 13])
    fin.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
    from reportlab.platypus import KeepTogether
    return [KeepTogether([k.p("Відмітка фінансово-економічного органу про відображення у регістрах бухгалтерського обліку:",
                "small"), fin, k.gap(4),
            k.p("Особа, яка відобразила господарську операцію в бухгалтерському обліку  ______________  "
                "______________________________ «___» ____________ 20___ року", "small"),
            k.p("(підпис)" + "&nbsp;" * 28 + "(посада, власне ім'я та прізвище)", "cap"), k.gap(5),
            k.signature("Помічник командира з фінансово-економічної роботи — начальник фінансової служби",
                        ctx["finance"], width_cm=(9.0, 3.0, 3.0, 6.0) if wide else (7.0, 3.0, 3.0, 5.0),
                        with_position=False)])]   # блок не розривається між аркушами


def act(ctx: dict, kind: str) -> bytes:
    """Акт приймання-передачі основних засобів (дод. 23, kind='oz') або запасів (дод. 24, 'zap').
    Альбомний аркуш А4: у формі 15 / 12 граф."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

    oz = kind == "oz"
    k = _Kit()
    W = 26.7  # ширина тексту альбомного А4 з полями 2 і 1 см
    appendix, title, what = (("23", "Акт приймання-передачі основних засобів", "основні засоби") if oz else
                             ("24", "Акт приймання-передачі запасів", "запаси"))
    story = [k.p(f"Додаток {appendix} до Інструкції з обліку військового майна у Збройних Силах України "
                 "(пункт 24 розділу IV)", "right_small"), k.gap(4)]
    left = [k.p(f"<b>{_esc(ctx['unit_name'])}</b>"), k.p("(найменування юридичної особи)", "cap_l")]
    if ctx["subunit_name"]:
        left.append(k.p(_esc(ctx["subunit_name"])))
    left.append(k.p(f"Код згідно з ЄДРПОУ {_esc(ctx['edrpou'])}"))
    ap = ctx["approver"]
    right = [k.p("<b>ЗАТВЕРДЖУЮ</b>"), k.p(_esc(ap["position"]) or "_" * 45), k.p("(посада)", "cap_l"),
             k.p(_esc(" ".join(x for x in (ap["rank"], ap["name"]) if x)) or "_" * 45),
             k.p("(військове звання, власне ім'я та прізвище)", "cap_l"),
             k.p("М.П. ________________________"), k.p("(підпис)", "cap_l"),
             k.p("«___» ________________ 20___ року")]
    head = Table([[left, right]], colWidths=[16.7 * cm, 10 * cm])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [head, k.p(title, "title"), k.gap(3)]

    req = Table([[k.p(x, "th_n") for x in (
                    "Дата реєстрації документа", "Місце складання", "Номер документа",
                    "Дата початку приймання-передачі", "Дата закінчення приймання-передачі",
                    f"Найменування юридичної особи (ПІБ фізичної особи), що передає {what}",
                    f"Найменування юридичної особи (ПІБ фізичної особи), що приймає {what}",
                    "Служба забезпечення")],
                 [k.p(_esc(x), "td_c") for x in (ctx["reg_date"], ctx["place"], ctx["doc_no"], ctx["op_date"],
                                                 ctx["op_date"], ctx["from_party"], ctx["to_party"], ctx["service"])]],
                colWidths=[w * cm for w in (2.3, 2.6, 2.0, 2.8, 2.8, 5.6, 5.8, 2.8)])
    req.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    story += [req, k.gap(6)]

    def th(x):
        return k.p(x, "th")

    def sub(x):
        return k.p(x, "th_n")

    if oz:
        widths = [0.7, 4.6, 2.4, 1.3, 2.0, 1.3, 1.3, 1.3, 1.3, 2.2, 1.4, 1.5, 1.4, 2.2, 1.8]  # 26,7 см
        rows = [[th("№ з/п"), th("Назва військового майна"), th("Код номен&shy;клатури"), th("Номер партії"),
                 th("Первісна (переоцінена) вартість"), th("Відправлено (вимагається)"), "",
                 th("Прийнято (відпущено)"), "", th("Сума"), th("Знос"), "", th("Рік випуску (побудови)"),
                 th("Заводський номер"), th("Номер паспорта")],
                ["", "", "", "", "", sub("Кількість"), sub("Категорія"), sub("Кількість"), sub("Категорія"), "",
                 sub("за одиницю"), sub("всього"), "", "", ""]]
        groups = [(5, 6), (7, 8), (10, 11)]
        singles = [0, 1, 2, 3, 4, 9, 12, 13, 14]
        sum_col = 9
        for ln in ctx["lines"]:
            number = ln["serial_no"] or (f"інв. № {ln['inventory_no']}" if ln["inventory_no"] else "")
            rows.append([k.p(str(ln["n"]), "td_c"), k.p(_esc(ln["name"]), "td"), k.p(_esc(ln["code"]), "td"),
                         k.p(_esc(ln["batch"]), "td"), k.p(ln["price"], "td_r"), k.p(ln["req"], "td_r"),
                         k.p(ln["cat"], "td_c"), k.p(ln["qty"], "td_r"), k.p(ln["cat"], "td_c"),
                         k.p(ln["sum"], "td_r"), "", "", k.p(str(ln["year_made"]), "td_c"),
                         k.p(_esc(number), "td"), k.p(_esc(ln["passport_no"]), "td")])
    else:
        widths = [0.7, 6.3, 2.4, 1.6, 1.4, 2.2, 1.6, 1.3, 1.6, 1.3, 2.4, 3.9]  # 26,7 см
        rows = [[th("№ з/п"), th("Назва військового майна або однорідна група (вид)"), th("Код номен&shy;клатури"),
                 th("Номер партії"), th("Одиниця виміру"), th("Вартість за одиницю виміру"),
                 th("Відправлено (вимагається)"), "", th("Прийнято (відпущено)"), "", th("Сума"), th("Примітки")],
                ["", "", "", "", "", "", sub("Кількість"), sub("Категорія"), sub("Кількість"), sub("Категорія"),
                 "", ""]]
        groups = [(6, 7), (8, 9)]
        singles = [0, 1, 2, 3, 4, 5, 10, 11]
        sum_col = 10
        for ln in ctx["lines"]:
            rows.append([k.p(str(ln["n"]), "td_c"), k.p(_esc(ln["name"]), "td"), k.p(_esc(ln["code"]), "td"),
                         k.p(_esc(ln["batch"]), "td"), k.p(_esc(ln["uom"]), "td_c"), k.p(ln["price"], "td_r"),
                         k.p(ln["req"], "td_r"), k.p(ln["cat"], "td_c"), k.p(ln["qty"], "td_r"),
                         k.p(ln["cat"], "td_c"), k.p(ln["sum"], "td_r"), k.p(_esc(ln["note"]), "td")])
    rows.insert(2, [k.p(str(i), "th_n") for i in range(1, len(widths) + 1)])
    total = [k.p("<b>Всього</b>", "td")] + [""] * (len(widths) - 1)
    total[sum_col] = k.p(f"<b>{ctx['total_sum']}</b>", "td_r")
    rows.append(total)
    last = len(rows) - 1
    style = [("GRID", (0, 0), (-1, -1), 0.5, colors.black), ("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("VALIGN", (0, 0), (-1, 1), "MIDDLE"),
             ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
             ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
             ("SPAN", (0, last), (sum_col - 1, last))]
    style += [("SPAN", (a, 0), (b, 0)) for a, b in groups]
    style += [("SPAN", (c, 0), (c, 1)) for c in singles]
    t = Table(rows, colWidths=[w * cm for w in widths], repeatRows=3)
    t.setStyle(TableStyle(style))

    basis = _esc(ctx["basis"]) or "&nbsp;" * 80
    if oz:
        inspected = f"На підставі <u>{basis}</u> проведено огляд переліченого військового майна."
        inspected_cap = "(назва, дата та номер документа, на підставі якого здійснюється передача (приймання))"
    else:
        inspected = (f"На підставі <u>{basis}</u> проведено огляд військового майна у кількості "
                     f"<u>{_esc(ctx['total_qty_words'])}</u> {ctx['total_units_word']} на суму "
                     f"<u>{_esc(ctx['total_hrn_words'])}</u> грн {ctx['total_kop']} коп.")
        inspected_cap = ("(назва, дата та номер документа, на підставі якого здійснюється передача (приймання));"
                         " кількість і сума — прописом")
    story += [t, k.gap(6), k.p(inspected), k.p(inspected_cap, "cap_l"),
              k.line("Місцезнаходження військового майна у момент передачі (прийняття)", W),
              k.line("Військове майно визначеним вимогам відповідає / не відповідає (підкреслити необхідне)", W),
              k.line("Висновок", W), k.line("Перелік документації, що додається", W), k.gap(5),
              k.p("Начальник служби забезпечення (начальник обліково-операційного підрозділу) військової частини "
                  "(центру забезпечення):", "small"),
              k.signature("", ctx["head"], width_cm=(0.4, 9.0, 4.0, 7.0)), k.gap(3),
              k.p(f"Матеріально відповідальна особа, яка <u>{ctx['our_action']}</u> на відповідальне зберігання "
                  "військове майно, вказане у цьому акті за: № ________", "small"),
              k.signature("", ctx["mvo_our"], width_cm=(0.4, 9.0, 4.0, 7.0)), k.gap(3),
              k.p("Військове майно прийняв / здав (підкреслити необхідне):", "small"),
              k.signature("", ctx["mvo_other"], width_cm=(0.4, 9.0, 4.0, 7.0)), k.gap(6)]
    story += _finance_block(k, ctx, wide=True)
    story += [k.gap(6)] + [k.p(_esc(row), "small") for row in ctx["copies_text"].splitlines()]
    story += [k.gap(6), k.p(_esc(ctx["processing_mark"]), "mark")]

    buf = BytesIO()
    pad = 6  # внутрішній відступ рамки reportlab, pt
    pdf = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=2 * cm - pad, rightMargin=1 * cm - pad,
                            topMargin=1.2 * cm, bottomMargin=1.2 * cm, title=f"{title} № {ctx['doc_no']}",
                            author=ctx["unit_name"])
    on_page = _draft_watermark if ctx["is_draft"] else (lambda c, d: None)
    pdf.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue()


def naryad(ctx: dict) -> bytes:
    """Наряд на видавання (приймання) військового майна, дод. 5. Як у частинах: книжковий А4,
    усе на одному аркуші (реквізити, графи 1–10, відправлення, упаковка), підпис командира
    військової частини, «Усього найменувань: N (прописом)», розсилка примірників."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

    k = _Kit()
    W = 18.0
    x = ctx["order"]
    grid = [("GRID", (0, 0), (-1, -1), 0.5, colors.black), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2)]

    def req_table(heads, values, widths):
        t = Table([[k.p(h, "th_n") for h in heads], [k.p(_esc(v), "td_c") for v in values]],
                  colWidths=[w * cm for w in widths])
        t.setStyle(TableStyle(grid))
        return t

    story = [k.p("Додаток 5 до Інструкції з обліку військового майна у Збройних Силах України "
                 "(пункт 6 розділу III)", "right_small"),
             k.p(f"Дійсний до {ctx['valid_until_long']}"),
             k.p(f"НАРЯД № {_esc(ctx['doc_no'])}", "title"),
             k.p("<b>на видавання (приймання) військового майна</b>", "center"), k.gap(5),
             req_table(["Реєстраційний номер", "Номер аркуша", "Номер документа", "Дата документа",
                        "Підстава (мета) операції", "Дата операції", "Служба забезпечення органу військового управління"],
                       [ctx["reg_no"], "", ctx["doc_no"], ctx["doc_date"], ctx["basis"], ctx["op_date"], ctx["service"]],
                       [1.7, 1.3, 1.6, 1.8, 6.0, 1.8, 3.8]), k.gap(4),
             req_table(["Вантажовідправник", "Вантажоодержувач та його поштова адреса", "Вид транспорту",
                        "Номер транспорту", "Найменування транспортного документа", "Номер транспортного документа"],
                       [ctx["from_title"], ", ".join(v for v in (ctx["to_title"], ctx["to_address"]) if v),
                        x["transport_kind"], x["transport_no"], x["transport_doc_name"], x["transport_doc_no"]],
                       [3.6, 5.4, 2.0, 2.0, 2.8, 2.2]), k.gap(6)]

    widths = [0.7, 3.9, 1.8, 1.2, 1.7, 1.3, 1.75, 1.75, 2.4, 1.5]   # 18 см
    rows = [[k.p(h, "th") for h in (
                "№ з/п", "Найменування військового майна (індекс, номер креслення)", "Код номен&shy;клатури",
                "Оди&shy;ниця виміру", "Ціна за одиницю", "Кате&shy;горія (сорт)",
                "Кількість до вида&shy;вання (прий&shy;мання), відван&shy;таження",
                "Фактично видано (прий&shy;нято), відван&shy;тажено",
                "Назва, номер та дата документа, за яким здійснено видавання (приймання), відвантаження",
                "Примітка")],
            [k.p(str(i), "th_n") for i in range(1, 11)]]
    for ln in ctx["lines"]:
        rows.append([k.p(str(ln["n"]), "td_c"), k.p(_esc(ln["name"]), "td"), k.p(_esc(ln["code"]), "td"),
                     k.p(_esc(ln["uom"]), "td_c"), k.p(ln["price"], "td_r"), k.p(ln["cat"], "td_c"),
                     k.p(ln["qty"], "td_r"), k.p(_esc(ln.get("fact", "")), "td_r"),
                     k.p(_esc(ln.get("fact_docs", "")), "td"), k.p(_esc(ln["note"]), "td")])
    rows.append(["", k.p(f"<b>Усього найменувань: {ctx['count']} ({_esc(ctx['count_words'])})</b>", "td")]
                + [""] * 8)
    t = Table(rows, colWidths=[w * cm for w in widths], repeatRows=2)
    t.setStyle(TableStyle(grid + [("VALIGN", (0, 2), (-1, -1), "TOP")]))
    story += [t, k.gap(8)]

    # Те, що за формою на звороті, — тут же (у частинах наряд на одному аркуші).
    story += [k.line("Порядок відправлення", W, value=x["dispatch_order"]), k.gap(4)]
    guard = Table([[k.p("Строк прибуття варти з приймальником"), k.p(_esc(x["guard_term"])),
                    k.p("від військової частини"), k.p(_esc(x["guard_unit"]))]],
                  colWidths=[6.5 * cm, 3.5 * cm, 3.8 * cm, 4.2 * cm])
    guard.setStyle(TableStyle([("LINEBELOW", (1, 0), (1, 0), 0.5, "black"), ("LINEBELOW", (3, 0), (3, 0), 0.5, "black"),
                               ("VALIGN", (0, 0), (-1, -1), "BOTTOM"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story += [guard, k.gap(14)]

    # Підпис командира військової частини (затверджує документи) — ліворуч посада й звання,
    # праворуч «Ім'я ПРІЗВИЩЕ»; поруч — таблиця упаковки, як у формі.
    ap = ctx["approver"]
    sign = Table([[k.p(_esc(ap["position"]) or "_" * 30), k.p("____________", "td_c"), k.p(_esc(ap["name"]), "right")],
                  [k.p(_esc(ap["rank"])), k.p("(підпис)", "cap"), ""]],
                 colWidths=[8.0 * cm, 4.0 * cm, 6.0 * cm])
    sign.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [sign, k.gap(12)]
    pack = Table([[k.p(h), ""] for h in ("Упаковано місць", "Вид (характер) упаковки", "Маркування", "Маса",
                                         "Відправлено місць", "Дата відправлення")],
                 colWidths=[5.0 * cm, 2.5 * cm], rowHeights=[16] * 6)
    pack.setStyle(TableStyle(grid))
    left = [k.p("М. П.  «___» ________________ 20___ року"), k.gap(16)]
    left += [k.p(_esc(row)) for row in ctx["copies_text"].splitlines()]
    bottom = Table([[left, pack]], colWidths=[10.5 * cm, 7.5 * cm])
    bottom.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [bottom, k.gap(10), k.p(_esc(ctx["processing_mark"]), "mark")]

    buf = BytesIO()
    pad = 6
    pdf = SimpleDocTemplate(buf, pagesize=A4, leftMargin=2 * cm - pad, rightMargin=1 * cm - pad,
                            topMargin=1.3 * cm, bottomMargin=1.3 * cm, title=f"Наряд № {ctx['doc_no']}",
                            author=ctx["unit_name"])
    on_page = _draft_watermark if ctx["is_draft"] else (lambda c, d: None)
    pdf.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue()


# Акт якісного стану: 17 граф на альбомному А4 (26,7 см); ті самі — у tools/make_docx_templates.py.
CONDITION_WIDTHS = [0.7, 3.2, 1.7, 1.2, 1.2, 1.2, 1.55, 1.8, 1.1, 1.2, 3.2, 1.7, 1.2, 1.2, 1.2, 1.55, 1.8]
# Заголовки граф 1–17 (­ — м'який перенос: вузькі графи не рвуть слово де завгодно).
CONDITION_HEADS = ["№ з/п", "найменування військового майна, заводський номер", "код номен­клатури",
                   "одиниця виміру", "кате­горія", "кількість", "ціна за одиницю, грн", "сума, грн",
                   "експлуатується, років", "",
                   "найменування озброєння (техніки, майна)", "код номен­клатури", "одиниця виміру",
                   "кате­горія", "кількість", "залишкова вартість за одиницю, грн", "сума, грн"]
# Рядок реквізитів (як у формі): назва графи, код, поле контексту.
CONDITION_REQ = [("Ознака інформації", "000", ""), ("Реєстраційний номер", "001", "reg_no"),
                 ("Номер аркуша", "002", ""), ("Код документа", "003", ""), ("Номер документа", "005", "doc_no"),
                 ("Дата документа", "032", "doc_date"), ("Підстава (мета) операції", "045", "basis"),
                 ("Код операції", "004", ""), ("Дата операції", "034", "op_date"), ("Служба", "046", "service"),
                 ("Військова частина", "", "cond.unit")]
CONDITION_REQ_WIDTHS = [2.0, 2.2, 1.8, 1.8, 2.2, 2.2, 5.3, 1.8, 2.2, 3.0, 2.2]


def _ctx_get(ctx: dict, path: str) -> str:
    value = ctx
    for part in path.split("."):
        value = value.get(part, "") if isinstance(value, dict) else ""
    return str(value or "")


def condition(ctx: dict) -> bytes:
    """Акт якісного (технічного) стану, дод. 1 до Порядку списання військового майна (наказ МОУ № 81).
    Альбомний А4: табличка рахунків, «ЗАТВЕРДЖУЮ», реквізити з кодами, графи 1–17 («Списати» /
    «Оприбуткувати»), висновок комісії, підписи комісії, висновок старшого начальника, прийняття
    отриманого на відповідальне зберігання."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

    k = _Kit()
    W = 26.7
    c = ctx["cond"]
    grid = [("GRID", (0, 0), (-1, -1), 0.5, colors.black), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
            ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]
    story = [k.p("Додаток 1 до Порядку списання військового майна у Збройних Силах України "
                 "(пункт 1.5 розділу I)", "right_small"), k.gap(4)]

    accounts = Table([[k.p(x, "th_n") for x in ("Номенклатурний номер", "Основний рахунок", "Кореспондентський рахунок")],
                      ["", "", ""]], colWidths=[3.6 * cm, 3.0 * cm, 3.6 * cm], rowHeights=[None, 0.6 * cm])
    accounts.setStyle(TableStyle(grid))
    ap = ctx["approver"]
    approve = [k.p("<b>ЗАТВЕРДЖУЮ</b>"), k.p(_esc(ap["position"]) or "_" * 45),
               k.p(_esc(" ".join(x for x in (ap["rank"], ap["name"]) if x)) or "_" * 45),
               k.p("(посада, військове звання, власне ім'я та прізвище)", "cap_l"),
               k.p("«___» ________________ 20___ року"), k.p("М.П.")]
    head = Table([[accounts, approve]], colWidths=[16.7 * cm, 10 * cm])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [head, k.p(f"Акт якісного (технічного) стану № {_esc(ctx['doc_no'])}", "title")]
    if c["group_name"]:
        story.append(k.p(_esc(c["group_name"]), "center"))
    story.append(k.gap(4))

    req = Table([[k.p(x[0], "th_n") for x in CONDITION_REQ], [k.p(x[1], "th_n") for x in CONDITION_REQ],
                 [k.p(_esc(_ctx_get(ctx, x[2])), "td_c") if x[2] else "" for x in CONDITION_REQ]],
                colWidths=[w * cm for w in CONDITION_REQ_WIDTHS])
    req.setStyle(TableStyle(grid))
    story += [req, k.gap(3), k.p("У результаті огляду встановлено:", "center"), k.gap(2)]

    def th(x):
        return k.p(x, "th_n")

    heads = CONDITION_HEADS

    rows = [[th("№ з/п"), th("<b>Списати</b>")] + [""] * 8 + [th("<b>Оприбуткувати</b>")] + [""] * 6,
            [""] + [th(h) for h in heads[1:8]] + [th(heads[8]), ""] + [th(h) for h in heads[10:]],
            [""] * 8 + [th("за нормою"), th("фак­тично")] + [""] * 7,
            [th(str(i)) for i in range(1, 18)]]
    for r in c["rows"]:
        rows.append([k.p(str(r["n"]), "td_c"), k.p(_esc(r["name"]), "td"), k.p(_esc(r["code"]), "td_c"),
                     k.p(_esc(r["uom"]), "td_c"), k.p(r["cat"], "td_c"), k.p(r["qty"], "td_r"),
                     k.p(r["price"], "td_r"), k.p(r["sum"], "td_r"), k.p(_esc(r["ynorm"]), "td_c"),
                     k.p(_esc(r["yfact"]), "td_c"), k.p(_esc(r["in_name"]), "td"), k.p(_esc(r["in_code"]), "td_c"),
                     k.p(_esc(r["in_uom"]), "td_c"), k.p(r["in_cat"], "td_c"), k.p(r["in_qty"], "td_r"),
                     k.p(r["in_price"], "td_r"), k.p(r["in_sum"], "td_r")])
    total = [""] * 17
    total[1] = k.p("<b>Усього</b>", "td_c")
    total[5], total[7] = k.p(f"<b>{c['out_qty']}</b>", "td_r"), k.p(f"<b>{c['out_sum']}</b>", "td_r")
    total[14], total[16] = k.p(f"<b>{c['in_qty']}</b>", "td_r"), k.p(f"<b>{c['in_sum']}</b>", "td_r")
    rows.append(total)
    style = grid + [("VALIGN", (0, 4), (-1, -1), "TOP"),
                    ("SPAN", (0, 0), (0, 2)), ("SPAN", (1, 0), (9, 0)), ("SPAN", (10, 0), (16, 0)),
                    ("SPAN", (8, 1), (9, 1))]
    style += [("SPAN", (col, 1), (col, 2)) for col in list(range(1, 8)) + list(range(10, 17))]
    t = Table(rows, colWidths=[w * cm for w in CONDITION_WIDTHS], repeatRows=4)
    t.setStyle(TableStyle(style))
    story += [t, k.gap(8)]

    story.append(k.p(f"Висновок комісії: {_esc(c['commission_conclusion']) or '_' * 150}"))
    story += [k.gap(8), k.p("Голова комісії:"), k.signature("", c["head"], width_cm=(0.4, 11.0, 4.0, 7.0)),
              k.gap(4), k.p("Члени комісії:")]
    for m in c["members"]:
        story += [k.signature("", m, width_cm=(0.4, 11.0, 4.0, 7.0)), k.gap(2)]
    story += [k.gap(6), k.p(f"Висновок старшого начальника: {_esc(c['senior_conclusion']) or '_' * 130}"),
              k.gap(4), k.signature("", c["senior"], width_cm=(0.4, 11.0, 4.0, 7.0)),
              k.p("«___» ________________ 20___ року          М.П."), k.gap(8),
              k.p("Отримані від розбирання вузли, прилади, запасні частини, деталі та інше майно, вказане "
                  "в графах 11–17, на відповідальне зберігання прийняв"),
              k.signature("", c["keeper"], width_cm=(0.4, 11.0, 4.0, 7.0)),
              k.p("«___» ________________ 20___ року")]
    story += [k.gap(6)] + [k.p(_esc(row), "small") for row in ctx["copies_text"].splitlines()]
    story += [k.gap(6), k.p(_esc(ctx["processing_mark"]), "mark")]

    buf = BytesIO()
    pad = 6
    pdf = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=2 * cm - pad, rightMargin=1 * cm - pad,
                            topMargin=1.2 * cm, bottomMargin=1.2 * cm,
                            title=f"Акт якісного (технічного) стану № {ctx['doc_no']}", author=ctx["unit_name"])
    on_page = _draft_watermark if ctx["is_draft"] else (lambda c_, d: None)
    pdf.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue()


BUILDERS = {"nakladna": nakladna,
            "act_condition": condition,
            "naryad": naryad,
            "act_oz": lambda ctx: act(ctx, "oz"),
            "act_zap": lambda ctx: act(ctx, "zap")}


def render(conn: sqlite3.Connection, paths: Paths, doc_id: int, form_code: str) -> tuple[bytes, str]:
    doc = documents.get(conn, doc_id)
    if form_code not in {f.code for f in docx_out.available_forms(conn, doc_id)} or form_code not in BUILDERS:
        raise docx_out.DocxError("Для цього документа такої форми немає")
    _register_fonts()
    data = BUILDERS[form_code](docx_out.context(conn, doc_id, form_code))
    form = docx_out.FORMS[form_code]
    name = docx_out._safe(f"{form.title} № {doc['doc_no']} від {documents.fmt_date(doc['doc_date'])}"
                          + (" (чернетка)" if doc["status"] == "draft" else "")) + ".pdf"
    try:
        folder = paths.output / str(doc["reg_year"])
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(data)
    except OSError:
        pass
    return data, name
