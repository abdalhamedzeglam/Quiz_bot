# report.py
"""توليد تقرير الأسئلة الأصعب كملف PDF يدعم اللغة العربية والجداول."""
import io
import logging
import os
from typing import Any, Dict, List
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

log = logging.getLogger("quizbot.report")
_HERE = os.path.dirname(os.path.abspath(__file__))

# تهيئة الخط العربي: خط المشروع أولاً (مجلد fonts/) ثم خطوط النظام
_FONT_NAME = "Helvetica"
_CANDIDATE_FONTS = [
    os.path.join(_HERE, "fonts", "Amiri-Regular.ttf"),
    os.path.join(_HERE, "fonts", "NotoNaskhArabic-Regular.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
    "C:\\Windows\\Fonts\\tahoma.ttf",
]

for _p in _CANDIDATE_FONTS:
    if os.path.exists(_p):
        try:
            pdfmetrics.registerFont(TTFont("ArabicFont", _p))
            _FONT_NAME = "ArabicFont"
            break
        except Exception:
            pass
if _FONT_NAME == "Helvetica":
    log.warning("لم يُعثر على خط عربي: ضع Amiri-Regular.ttf في مجلد fonts/ وإلا ستظهر الحروف مربعات في الـ PDF")


def _ar(text: Any, limit: int = 0) -> str:
    """
    يجهّز نصاً للطباعة داخل Paragraph:
      1) اقتطاع (حتى لا يلتفّ النص العربي على سطرين فتنعكس أسطره)
      2) تشكيل الحروف العربية وترتيب الاتجاه
      3) escape لرموز < > & كي لا تُفسَّر كوسوم (مهم في الأسئلة الرياضية)
    """
    if text is None:
        return "—"
    s = " ".join(str(text).split())
    if not s:
        return "—"
    if limit and len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display

        s = get_display(arabic_reshaper.reshape(s))
    except Exception:
        pass
    return escape(s)


def generate_pdf_report(title: str, items: List[Dict[str, Any]]) -> io.BytesIO:
    """توليد ملف PDF في الذاكرة وإرجاعه كـ BytesIO."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        rightMargin=25,
        leftMargin=25,
        topMargin=25,
        bottomMargin=25,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ReportTitle",
        parent=styles["Heading1"],
        fontName=_FONT_NAME,
        fontSize=15,
        leading=18,
        alignment=1,  # وسط
        textColor=colors.HexColor("#1A365D"),
    )
    cell_style = ParagraphStyle("CellText", fontName=_FONT_NAME, fontSize=9, leading=11, alignment=1)

    elements = [
        Paragraph(_ar(f"تقرير الأسئلة الأصعب — {title}", 90), title_style),
        Spacer(1, 15),
    ]

    # ترويسة الجدول (من اليسار لليمين بصرياً؛ العمود الأول عند اليمين هو الرقم)
    headers = [
        _ar("الخطأ / الإجمالي"),
        _ar("نسبة الخطأ"),
        _ar("الإجابة الصحيحة"),
        _ar("نص السؤال / الوصف"),
        "#",
    ]
    table_data = [headers]

    for it in items:
        table_data.append([
            f"{it['wrong']}/{it['total']}",
            f"{it['wrong_pct']}%",
            Paragraph(_ar(it.get("answer", "—"), 28), cell_style),
            Paragraph(_ar(it.get("desc", "—"), 48), cell_style),
            str(it["id"]),
        ])

    col_widths = [80, 65, 120, 240, 30]
    t = Table(table_data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2B6CB0")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, -1), _FONT_NAME),
        ("FONTSIZE", (0, 0), (-1, 0), 10),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E0")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F7FAFC")]),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))

    elements.append(t)
    doc.build(elements)
    buf.seek(0)
    return buf
