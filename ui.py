"""بناء نصوص الرسائل ولوحات الأزرار (دوال نقية بلا شبكة، سهلة الاختبار)."""
import html
from typing import Dict, List, Optional, Tuple

from telebot.types import InlineKeyboardButton as Btn
from telebot.types import InlineKeyboardMarkup as Markup

LETTERS = ("أ", "ب", "ج", "د", "هـ", "و")
TEXT_LIMIT = 3900      # حد رسالة النص في تيليجرام 4096
CAPTION_LIMIT = 1000   # حد كابشن الصورة 1024

# ---- بادئات callback_data ----
CB_ANSWER, CB_NEXT, CB_SKIP, CB_END = "a", "n", "s", "x"
CB_CHAPTER, CB_RETRY, CB_MENU, CB_LOCKED = "c", "r", "m", "z"
CB_ADMIN = "ad"


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def clip(text: Optional[str], limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


def label(i: int) -> str:
    return LETTERS[i] if i < len(LETTERS) else str(i + 1)


def progress_bar(pos: int, total: int, length: int = 8) -> str:
    filled = round(length * pos / total) if total else 0
    return "🟩" * filled + "⬜" * (length - filled)


def _tf_label(opt: str) -> str:
    if opt.strip() == "صح":
        return "✅ صح"
    if opt.strip() == "خطأ":
        return "❌ خطأ"
    return clip(opt, 20)


# ---------------------------------------------------------------------------
# عرض السؤال
# ---------------------------------------------------------------------------
def _layout(q: dict) -> Tuple[bool, bool]:
    """(is_tf, use_letters): الأزرار تعرض الحروف إن كانت الخيارات طويلة أو كثيرة."""
    is_tf = q["kind"] == "tf"
    options = q["options"]
    use_letters = (not is_tf) and (len(options) > 4 or max(len(o) for o in options) > 18)
    return is_tf, use_letters


def _compose(q: dict, pos: int, total: int, chosen: Optional[int], show_list: bool, caps: Dict[str, int]) -> str:
    options = q["options"]
    correct = q["correct_index"]
    answered = chosen is not None

    parts: List[str] = [f"📊 {progress_bar(pos, total)}  <b>{pos}/{total}</b>"]

    qt = clip(q.get("question_text"), caps["q"])
    if qt:
        parts.append(f"❓ {esc(qt)}")
    if not answered and q.get("hint"):
        parts.append(f"💡 {esc(clip(q['hint'], caps['hint']))}")

    if show_list:
        lines = []
        for i, opt in enumerate(options):
            if answered:
                mark = "✅" if i == correct else ("❌" if i == chosen else "▫️")
            else:
                mark = "🔹"
            opt_label = f"({label(i)})" if opt == label(i) else f"{label(i)}) {esc(clip(opt, caps['opt']))}"
            lines.append(f"{mark} {opt_label}")
        parts.append("\n".join(lines))

    if answered:
        if chosen == correct:
            parts.append("✅ <b>إجابة صحيحة!</b>")
        else:
            parts.append("❌ <b>إجابة خاطئة</b>")
            if not show_list:
                parts.append(f"الإجابة الصحيحة: <b>{esc(clip(options[correct], caps['opt']))}</b>")
        if q.get("explanation"):
            parts.append(f"📝 {esc(clip(q['explanation'], caps['expl']))}")

    return "\n\n".join(parts)


def build_question_view(
    q: dict, pos: int, total: int, token: int, chosen: Optional[int] = None, is_last: bool = False
) -> Tuple[str, Markup, Optional[str]]:
    """
    يبني (النص، الأزرار، معرّف الصورة) لسؤال.
      pos/total : الترتيب الظاهر للطالب.
      token     : رقم السؤال داخل الجلسة، يُضمَّن في الأزرار لتجاهل الضغطات القديمة.
      chosen    : رقم الخيار الذي اختاره الطالب (None = لم يجب بعد).
    يضمن أن النص لا يتجاوز حد تيليجرام بعد الـ escape.
    """
    image = q.get("image_file_id")
    limit = CAPTION_LIMIT if image else TEXT_LIMIT
    is_tf, use_letters = _layout(q)
    answered = chosen is not None
    show_list = (not is_tf) and (use_letters or answered)

    base = {"q": 500 if image else 900, "hint": 160, "expl": 400, "opt": 110}
    text = ""
    for scale in (1.0, 0.7, 0.5, 0.3, 0.15, 0.05):
        caps = {k: max(int(v * scale), 20) for k, v in base.items()}
        text = _compose(q, pos, total, chosen, show_list, caps)
        if len(text) <= limit:
            break

    # صمام أمان حاسم لمنع تجاوز حد تيليجرام لكابشن الصور (1024 حرفا
# صمام أمان حاسم لمنع تجاوز حد تيليجرام لكابشن الصور وتجنب كسر وسوم HTML
# صمام أمان حاسم لمنع تجاوز حد كابشن الصور وتجنب كسر وسوم HTML والتشفير المزدوج
    if image and len(text) > 1020:
        tight_caps = {"q": 140, "hint": 40, "expl": 80, "opt": 35}
        text = _compose(q, pos, total, chosen, show_list, tight_caps)
        if len(text) > 1020:
            import re
            plain = re.sub(r"<[^>]*>", "", text)
            # فك التشفير السابق لمنع تحول &lt; إلى &amp;lt; ثم إعادة التشفير بأمان
            unescaped = html.unescape(plain)
            text = esc(unescaped[:1000].rstrip()) + "..."

    kb = Markup()
    options = q["options"]
    if answered:
        kb.row(Btn("🏁 عرض النتيجة" if is_last else "⏭️ التالي", callback_data=f"{CB_NEXT}:{token}"))
        kb.row(Btn("🏁 إنهاء", callback_data=f"{CB_END}:end"),
               Btn("🏠 القائمة", callback_data=f"{CB_MENU}:menu"))
        return text, kb, image

    if is_tf:
        kb.row(*[Btn(_tf_label(o), callback_data=f"{CB_ANSWER}:{token}:{i}") for i, o in enumerate(options)])
    elif use_letters:
        btns = [Btn(label(i), callback_data=f"{CB_ANSWER}:{token}:{i}") for i in range(len(options))]
        for i in range(0, len(btns), 3):
            kb.row(*btns[i:i + 3])
    else:
        btns = [Btn(clip(o, 40), callback_data=f"{CB_ANSWER}:{token}:{i}") for i, o in enumerate(options)]
        for i in range(0, len(btns), 2):
            kb.row(*btns[i:i + 2])

    kb.row(Btn("⏭️ تخطي", callback_data=f"{CB_SKIP}:{token}"),
           Btn("🏁 إنهاء", callback_data=f"{CB_END}:end"))
    kb.row(Btn("🏠 القائمة الرئيسية", callback_data=f"{CB_MENU}:menu"))
    return text, kb, image

def build_summary_view(res: dict) -> Tuple[str, Markup]:
    total, correct, wrong, skipped = res["total"], res["correct"], res["wrong"], res["skipped"]
    kb = Markup()
    if total == 0:
        kb.row(Btn("📖 الأبواب", callback_data=f"{CB_MENU}:menu"))
        return "لا توجد أسئلة في هذه الجلسة.", kb

    pct = round(100 * correct / total)
    emoji = "🏆" if pct >= 90 else "🎉" if pct >= 75 else "👍" if pct >= 50 else "💪"
    text = (
        f"{emoji} <b>انتهى التدريب!</b>\n\n"
        f"✅ صحيحة: <b>{correct}</b>\n"
        f"❌ خاطئة: <b>{wrong}</b>\n"
        f"⏭️ متخطّاة: <b>{skipped}</b>\n\n"
        f"📈 النسبة: <b>{pct}%</b>"
    )
    if res["retry_ids"]:
        kb.row(Btn(f"🔄 مراجعة الأخطاء والمتخطّى ({len(res['retry_ids'])})", callback_data=f"{CB_RETRY}:retry"))
    kb.row(Btn("📖 الأبواب", callback_data=f"{CB_MENU}:menu"))
    return text, kb


def build_menu_view(chapters: List[dict], counts: Dict[int, int], is_admin: bool) -> Tuple[str, Optional[Markup]]:
    if not chapters:
        return "⏳ جارٍ تجهيز البيانات، أرسل /start بعد لحظات.", None
    kb = Markup()
    for ch in chapters:
        title = clip(ch["title"], 55)
        count = counts.get(ch["id"], 0)
        if ch["is_active"] and count > 0:
            kb.row(Btn(f"📖 {title} ({count})", callback_data=f"{CB_CHAPTER}:{ch['id']}"))
        elif ch["is_active"] and count == 0:
            kb.row(Btn(f"🔒 {title} (قيد الإعداد)", callback_data=f"{CB_LOCKED}:empty"))
        else:
            kb.row(Btn(f"🔒 {title}", callback_data=f"{CB_LOCKED}:off"))
    if is_admin:
        kb.row(Btn("👑 لوحة التحكم", callback_data=f"{CB_ADMIN}:home"))
    return "👋 <b>أهلاً بك!</b>\nاختر الباب الذي تريد التدريب عليه:", kb

# ---------------------------------------------------------------------------
# لوحة الأدمن
# ---------------------------------------------------------------------------
ADMIN_HOME_TEXT = "👑 <b>لوحة التحكم</b>"


def admin_home_markup() -> Markup:
    kb = Markup()
    kb.row(Btn("➕ إضافة سؤال", callback_data="ad:add"), Btn("🗂️ بنك الأسئلة", callback_data="ad:bank"))
    kb.row(Btn("🔓 فتح/قفل الأبواب", callback_data="ad:chs"), Btn("📑 تقرير الأصعب", callback_data="ad:rep"))
    kb.row(Btn("📊 إحصائيات", callback_data="ad:stats"), Btn("🔄 تحديث البيانات", callback_data="ad:sync"))
    kb.row(Btn("❌ إغلاق", callback_data="ad:close"))
    return kb


def back_markup(target: str = "ad:home") -> Markup:
    kb = Markup()
    kb.row(Btn("🔙 رجوع", callback_data=target))
    return kb


def build_bank_view(q: dict, i: int, n: int, ch_id: int) -> Tuple[str, Markup, Optional[str]]:
    kind = "صح/خطأ" if q["kind"] == "tf" else "اختيار من متعدد"
    lines = [f"🗂️ <b>{i + 1}/{n}</b> • #{q['id']} • {kind}"]
    qt = clip(q.get("question_text"), 450)
    lines.append(f"❓ {esc(qt)}" if qt else "❓ (سؤال بصورة)")
    opts = []
    for k, opt in enumerate(q["options"]):
        mark = "✅" if k == q["correct_index"] else "▫️"
        opt_str = f"({label(k)})" if opt == label(k) else f"{label(k)}) {esc(clip(opt, 80))}"
        opts.append(f"{mark} {opt_str}")
    lines.append("\n".join(opts))
    if q.get("explanation"):
        lines.append(f"📝 {esc(clip(q['explanation'], 200))}")
    text = "\n\n".join(lines)

    kb = Markup()
    nav = []
    if i > 0:
        nav.append(Btn("◀️", callback_data=f"ad:bk:{ch_id}:{i - 1}"))
    if i < n - 1:
        nav.append(Btn("▶️", callback_data=f"ad:bk:{ch_id}:{i + 1}"))
    if nav:
        kb.row(*nav)
    kb.row(Btn("✏️ تعديل", callback_data=f"ad:eq:{q['id']}:{ch_id}:{i}"),
           Btn("🗑️ حذف", callback_data=f"ad:dq:{q['id']}:{ch_id}:{i}"))
    kb.row(Btn("🔙 الأبواب", callback_data="ad:bank"))
    return text, kb, q.get("image_file_id")
