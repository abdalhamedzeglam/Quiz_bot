"""معالجات الأدمن: إضافة سؤال (معالج خطوات)، بنك الأسئلة، فتح/قفل الأبواب، إحصائيات، تقرير الأصعب."""
import asyncio
import logging
import re
from typing import List, Optional, Tuple

from telebot.types import InlineKeyboardButton as Btn
from telebot.types import InlineKeyboardMarkup as Markup

import config
import database as db
import ui
from report import generate_pdf_report
from state_manager import sessions
from tg import answer_cb, bot, render_message, safe_delete, tg_call

log = logging.getLogger("quizbot.admin")

_pdf_sem = asyncio.Semaphore(1)       # تقرير PDF واحد في كل مرة (يستهلك CPU)
_OPTION_PREFIX = re.compile(r"^\s*\(?[أبجدهوA-D][\)\.]\s+")   # يحذف "أ)" و"B." من أول سطر الخيار

MAX_QUESTION_CHARS = 1500
MAX_OPTION_CHARS = 120
MAX_EXPLANATION_CHARS = 600


def is_admin(uid: int) -> bool:
    return uid in config.ADMIN_IDS


def _photo(msg) -> bool:
    return bool(getattr(msg, "photo", None))


async def _show(msg, text: str, kb: Optional[Markup], image: Optional[str] = None) -> Tuple[int, bool]:
    return await render_message(msg.chat.id, msg.message_id, _photo(msg), text, kb, image)


def _chapter_title(ch_id: int) -> str:
    return next((c["title"] for c in db.get_chapters() if c["id"] == ch_id), f"الباب {ch_id}")


# ===========================================================================
# الأوامر
# ===========================================================================
@bot.message_handler(commands=["admin"])
async def cmd_admin(msg):
    uid = msg.from_user.id
    if not is_admin(uid):
        return
    sessions.clear_admin(uid)
    await tg_call(bot.send_message, msg.chat.id, ui.ADMIN_HOME_TEXT,
                  parse_mode="HTML", reply_markup=ui.admin_home_markup())


# ===========================================================================
# الأزرار (كلها تبدأ بـ ad:)
# ===========================================================================
@bot.callback_query_handler(func=lambda c: (c.data or "").startswith(ui.CB_ADMIN + ":"))
async def on_admin_callback(call):
    uid = call.from_user.id
    if not is_admin(uid):
        await answer_cb(call.id, "⛔ غير مصرّح", True)
        return
    msg = call.message
    if msg is None:
        await answer_cb(call.id, "الرسالة قديمة، أرسل /admin", True)
        return

    async with sessions.lock(("admin", uid)):
        try:
            await _admin_route(call, uid, msg, call.data.split(":")[1:])
        except Exception:
            log.exception("admin callback failed data=%s", call.data)
            await answer_cb(call.id)
            try:
                await tg_call(bot.send_message, msg.chat.id, "⚠️ حدث خطأ، راجع السجلات.")
            except Exception:
                pass


async def _admin_route(call, uid: int, msg, p: List[str]) -> None:
    cmd = p[0] if p else ""
    chat_id = msg.chat.id

    # ---------------------------------------------------------------- عام
    if cmd == "home":
        sessions.clear_admin(uid)
        await answer_cb(call.id)
        await _show(msg, ui.ADMIN_HOME_TEXT, ui.admin_home_markup())
        return

    if cmd == "close":
        sessions.clear_admin(uid)
        await answer_cb(call.id)
        await safe_delete(chat_id, msg.message_id)
        return

    # ------------------------------------------------- تحديث البيانات يدوياً
    if cmd == "sync":
        ok = await db.refresh_content()          # نردّ مرة واحدة فقط بعد الانتهاء (تيليجرام لا يقبل ردّين)
        if ok:
            n = sum(len(db.get_chapter_questions(c["id"])) for c in db.get_chapters())
            await answer_cb(call.id, f"✅ تم التحديث: {len(db.get_chapters())} باب و{n} سؤال", True)
        else:
            await answer_cb(call.id, "❌ فشل التحديث، راجع السجلات", True)
        return

    # ------------------------------------------------------------ إحصائيات
    if cmd == "stats":
        await answer_cb(call.id)
        users, attempts, qn = await db.get_bot_stats()
        lines = [f"📊 <b>الإحصائيات</b>\n",
                 f"👥 الطلاب: <b>{users}</b>",
                 f"📝 المحاولات المسجلة: <b>{attempts}</b>",
                 f"📚 الأسئلة: <b>{qn}</b>",
                 f"⚙️ جلسات نشطة الآن: <b>{sessions.active_count()}</b>\n"]
        for c in db.get_chapters():
            n = len(db.get_chapter_questions(c["id"]))
            lines.append(f"{'🟢' if c['is_active'] else '🔴'} {ui.esc(ui.clip(c['title'], 50))} — {n}")
        await _show(msg, "\n".join(lines), ui.back_markup())
        return

    # ------------------------------------------------------ فتح/قفل الأبواب
    if cmd in ("chs", "tg"):
        if cmd == "tg":
            new_state = await db.toggle_chapter(int(p[1]))
            if new_state is None:
                await answer_cb(call.id, "⚠️ فشل التحديث، راجع السجلات", True)
            else:
                await answer_cb(call.id, "🟢 تم فتح الباب" if new_state else "🔴 تم قفل الباب")
        else:
            await answer_cb(call.id)
        kb = Markup()
        for c in db.get_chapters():
            kb.row(Btn(f"{'🟢' if c['is_active'] else '🔴'} {ui.clip(c['title'], 50)}",
                       callback_data=f"ad:tg:{c['id']}"))
        kb.row(Btn("🔙 رجوع", callback_data="ad:home"))
        await _show(msg, "🔓 <b>اضغط على الباب لفتحه أو قفله:</b>", kb)
        return

    # ---------------------------------------------------------- بنك الأسئلة
    if cmd == "bank":
        await answer_cb(call.id)
        kb = Markup()
        for c in db.get_chapters():
            n = len(db.get_chapter_questions(c["id"]))
            kb.row(Btn(f"📖 {ui.clip(c['title'], 45)} ({n})", callback_data=f"ad:bk:{c['id']}:0"))
        kb.row(Btn("🔙 رجوع", callback_data="ad:home"))
        await _show(msg, "🗂️ <b>اختر الباب لتصفّح أسئلته:</b>", kb)
        return

    if cmd == "bk":
        await answer_cb(call.id)
        await _show_bank_question(msg, int(p[1]), int(p[2]))
        return

    if cmd == "dq":                                          # طلب تأكيد الحذف
        await answer_cb(call.id)
        qid, ch_id, i = p[1], p[2], p[3]
        kb = Markup()
        kb.row(Btn("✅ نعم، احذف", callback_data=f"ad:dy:{qid}:{ch_id}:{i}"),
               Btn("↩️ تراجع", callback_data=f"ad:bk:{ch_id}:{i}"))
        await tg_call(bot.edit_message_reply_markup, chat_id, msg.message_id, reply_markup=kb)
        return

    if cmd == "dy":                                          # تنفيذ الحذف
        ok = await db.delete_question(int(p[1]))
        await answer_cb(call.id, "🗑️ تم الحذف" if ok else "❌ فشل الحذف")
        await _show_bank_question(msg, int(p[2]), int(p[3]))
        return

# -------------------------------------------------------- تعديل السؤال
    if cmd == "eq":                                          # فتح قائمة خيارات التعديل
        await answer_cb(call.id)
        qid, ch_id, i = int(p[1]), int(p[2]), int(p[3])
        q = db.get_question(qid)
        if not q:
            await answer_cb(call.id, "⚠️ السؤال غير موجود", True)
            return
        kb = Markup()
        kb.row(Btn("📝 نص السؤال / الصورة", callback_data=f"ad:eqc:{qid}:{ch_id}:{i}"))
        if q.get("image_file_id"):
            kb.row(Btn("🗑️ إزالة الصورة (جعله نصياً)", callback_data=f"ad:eq_rmimg:{qid}:{ch_id}:{i}"))
        if q["kind"] == "tf":
            kb.row(Btn("🔘 تبديل الإجابة (صح / خطأ)", callback_data=f"ad:eq_tf:{qid}:{ch_id}:{i}"))
        else:
            kb.row(Btn("🔘 الخيارات والإجابة الصحيحة", callback_data=f"ad:eqo:{qid}:{ch_id}:{i}"))
        kb.row(Btn("💡 شرح الإجابة", callback_data=f"ad:eqe:{qid}:{ch_id}:{i}"))
        kb.row(Btn("🔙 رجوع للسؤال", callback_data=f"ad:bk:{ch_id}:{i}"))
        await _show(msg, f"✏️ <b>تعديل السؤال #{qid}</b>\n\nاختر ما تريد تعديله:", kb)
        return

    if cmd == "eq_rmimg":                                    # حذف الصورة فقط وتحويل السؤال لنصي
        qid, ch_id, i = int(p[1]), int(p[2]), int(p[3])
        q = db.get_question(qid)
        if not q:
            await answer_cb(call.id, "⚠️ السؤال غير موجود", True)
            return
        if not (q.get("question_text") or "").strip():
            await answer_cb(call.id, "⚠️ السؤال بلا نص! أضف نصاً أولاً ثم احذف الصورة.", True)
            return
        ok = await db.update_question(qid, image_file_id="")
        await answer_cb(call.id, "🗑️ تم حذف الصورة بنجاح!" if ok else "❌ فشل التحديث")
        await _show_bank_question(msg, ch_id, i)
        return

    if cmd == "eqc":                                         # طلب إرسال نص/صورة جديدة
        await answer_cb(call.id)
        qid, ch_id, i = int(p[1]), int(p[2]), int(p[3])
        kb = Markup()
        kb.row(Btn("❌ إلغاء", callback_data=f"ad:bk:{ch_id}:{i}"))
        mid, is_photo = await _show(msg, "📝 أرسل <b>السؤال الجديد</b> الآن:\n(يمكنك إرسال صورة جديدة أو نص فقط)", kb)
        sessions.set_admin(uid, "EQ_CONTENT", {"qid": qid, "ch": ch_id, "i": i, "mid": mid, "is_photo": is_photo})
        return

    if cmd == "eq_tf":                                       # تبديل مباشر لأسئلة صح/خطأ مع فحص الأمان
        qid, ch_id, i = int(p[1]), int(p[2]), int(p[3])
        q = db.get_question(qid)
        if not q:
            await answer_cb(call.id, "⚠️ السؤال غير موجود", True)
            return
        new_ans = 1 if q["correct_index"] == 0 else 0
        ok = await db.update_question(qid, correct_index=new_ans)
        await answer_cb(call.id, "✅ تم تغيير الإجابة الصحيحة!" if ok else "❌ فشل التحديث")
        await _show_bank_question(msg, ch_id, i)
        return

    if cmd == "eqo":                                         # تعديل الخيارات
        await answer_cb(call.id)
        qid, ch_id, i = int(p[1]), int(p[2]), int(p[3])
        kb = Markup()
        kb.row(Btn("❌ إلغاء", callback_data=f"ad:bk:{ch_id}:{i}"))
        mid, is_photo = await _show(msg, "🔘 أرسل <b>الخيارات الجديدة</b> (كل خيار في سطر مستقل أو اختصار مثل <code>.4</code>):", kb)
        sessions.set_admin(uid, "EQ_OPTIONS", {"qid": qid, "ch": ch_id, "i": i, "mid": mid, "is_photo": is_photo})
        return

    if cmd == "eq_ans":                                      # حفظ الإجابة الصحيحة بعد إرسال الخيارات
        st = sessions.get_admin(uid)
        if not st or st["state"] != "EQ_CORRECT":
            await answer_cb(call.id)
            await _show_bank_question(msg, int(p[2]), int(p[3]))
            return
        d = st["data"]
        correct_idx = int(p[1])
        ok = await db.update_question(d["qid"], options=d["options"], correct_index=correct_idx)
        sessions.clear_admin(uid)
        await answer_cb(call.id, "✅ تم حفظ التعديل بنجاح!" if ok else "❌ فشل التعديل")
        await _show_bank_question(msg, d["ch"], d["i"])
        return

    if cmd == "eqe":                                         # طلب إرسال الشرح
        await answer_cb(call.id)
        qid, ch_id, i = int(p[1]), int(p[2]), int(p[3])
        kb = Markup()
        kb.row(Btn("⏭️ حذف الشرح (بدون شرح)", callback_data=f"ad:eqe_skip:{qid}:{ch_id}:{i}"))
        kb.row(Btn("❌ إلغاء", callback_data=f"ad:bk:{ch_id}:{i}"))
        mid, is_photo = await _show(msg, "💡 أرسل <b>الشرح الجديد</b> الآن، أو اضغط «حذف الشرح»:", kb)
        sessions.set_admin(uid, "EQ_EXPL", {"qid": qid, "ch": ch_id, "i": i, "mid": mid, "is_photo": is_photo})
        return

    if cmd == "eqe_skip":                                    # تفريغ الشرح
        qid, ch_id, i = int(p[1]), int(p[2]), int(p[3])
        ok = await db.update_question(qid, explanation="")
        sessions.clear_admin(uid)
        await answer_cb(call.id, "🗑️ تم إزالة الشرح" if ok else "❌ فشل التحديث")
        await _show_bank_question(msg, ch_id, i)
        return

    # ------------------------------------------------------------- التقرير
    if cmd == "rep":
        await answer_cb(call.id)
        kb = Markup()
        kb.row(Btn("📚 كل الأبواب", callback_data="ad:rp:all"))
        for c in db.get_chapters():
            kb.row(Btn(ui.clip(c["title"], 50), callback_data=f"ad:rp:{c['id']}"))
        kb.row(Btn("🔙 رجوع", callback_data="ad:home"))
        await _show(msg, f"📑 <b>تقرير الأسئلة الأصعب</b>\n(نسبة خطأ أكبر من 50% وعدد محاولات ≥ {config.REPORT_MIN_ATTEMPTS})",
                    kb)
        return

    if cmd == "rp":
        await answer_cb(call.id, "⏳ جاري إعداد التقرير...")
        ch_id = None if p[1] == "all" else int(p[1])
        await _send_report(chat_id, ch_id)
        return

    # ------------------------------------------------------ معالج إضافة سؤال
    if cmd == "add":
        await answer_cb(call.id)
        sessions.set_admin(uid, "W_CHAPTER", {})
        kb = Markup()
        for c in db.get_chapters():
            kb.row(Btn(ui.clip(c["title"], 50), callback_data=f"ad:wc:{c['id']}"))
        kb.row(Btn("❌ إلغاء", callback_data="ad:home"))
        await _show(msg, "➕ <b>إضافة سؤال</b>\n\nاختر الباب:", kb)
        return

    if cmd in ("wc", "wmore"):
        await answer_cb(call.id)
        if cmd == "wc":
            ch_id = int(p[1])
        else:
            st = sessions.get_admin(uid)
            ch_id = (st or {}).get("data", {}).get("ch")
            if ch_id is None:
                await _show(msg, ui.ADMIN_HOME_TEXT, ui.admin_home_markup())
                return
        sessions.set_admin(uid, "W_KIND", {"ch": ch_id})
        kb = Markup()
        kb.row(Btn("✅❌ صح / خطأ", callback_data="ad:wk:tf"),
               Btn("🔘 اختيار من متعدد", callback_data="ad:wk:choice"))
        kb.row(Btn("❌ إلغاء", callback_data="ad:home"))
        await _show(msg, f"➕ <b>{ui.esc(ui.clip(_chapter_title(ch_id), 60))}</b>\n\nنوع السؤال؟", kb)
        return

    st = sessions.get_admin(uid)

    if cmd == "wk":
        await answer_cb(call.id)
        if not st or st["state"] != "W_KIND":
            await _show(msg, "⚠️ انتهت الجلسة. اضغط «إضافة سؤال» من جديد.", ui.back_markup())
            return
        st["data"]["kind"] = p[1] if p[1] in ("tf", "choice") else "choice"
        st["state"] = "W_CONTENT"
        await _show(msg,
                    "➕ أرسل <b>السؤال</b> الآن:\n"
                    "• صورة (ويمكنك كتابة نص السؤال في وصف الصورة)، أو\n"
                    "• رسالة نصية.",
                    _cancel_markup())
        return

    if cmd == "wo":                                          # اختيار الإجابة الصحيحة
        await answer_cb(call.id)
        if not st or st["state"] != "W_CORRECT":
            await _show(msg, "⚠️ انتهت الجلسة. اضغط «إضافة سؤال» من جديد.", ui.back_markup())
            return
        st["data"]["correct"] = int(p[1])
        st["state"] = "W_EXPL"
        kb = Markup()
        kb.row(Btn("⏭️ بدون شرح", callback_data="ad:wskip"))
        kb.row(Btn("❌ إلغاء", callback_data="ad:home"))
        await _show(msg, "📝 أرسل <b>شرح الإجابة</b> (يظهر للطالب بعد أن يجيب)، أو اضغط «بدون شرح».", kb)
        return

    if cmd == "wskip":
        await answer_cb(call.id)
        if not st or st["state"] != "W_EXPL":
            await _show(msg, "⚠️ انتهت الجلسة. اضغط «إضافة سؤال» من جديد.", ui.back_markup())
            return
        st["data"]["expl"] = ""
        await _save_question(msg.chat.id, uid, st["data"])
        return

    await answer_cb(call.id)


def _cancel_markup() -> Markup:
    kb = Markup()
    kb.row(Btn("❌ إلغاء", callback_data="ad:home"))
    return kb


async def _show_bank_question(msg, ch_id: int, i: int) -> None:
    qs = db.get_chapter_questions(ch_id)
    if not qs:
        await _show(msg, "🗂️ هذا الباب لا يحتوي أسئلة.", ui.back_markup("ad:bank"))
        return
    i = max(0, min(i, len(qs) - 1))
    text, kb, image = ui.build_bank_view(qs[i], i, len(qs), ch_id)
    await _show(msg, text, kb, image)


# ===========================================================================
# إدخال الأدمن (نصوص وصور) أثناء المعالج
# ===========================================================================
@bot.message_handler(
    content_types=["text", "photo"],
    func=lambda m: m.from_user.id in config.ADMIN_IDS and sessions.get_admin(m.from_user.id) is not None,
)
async def on_admin_input(msg):
    uid, chat_id = msg.from_user.id, msg.chat.id
    if (msg.text or "").startswith("/"):
        return
    async with sessions.lock(("admin", uid)):
        st = sessions.get_admin(uid)
        if st is None:
            return
        state, d = st["state"], st["data"]
        try:
            if state == "W_CONTENT":
                await _on_content(msg, st)
            elif state == "W_OPTIONS":
                await _on_options(msg, st)
            elif state == "W_EXPL":
                await _on_explanation(msg, uid, d)
            elif state == "EQ_CONTENT":
                await _on_edit_content(msg, uid, d)
            elif state == "EQ_OPTIONS":
                await _on_edit_options(msg, st)
            elif state == "EQ_EXPL":
                await _on_edit_explanation(msg, uid, d)
        except Exception:
            log.exception("admin input failed state=%s", state)
            await tg_call(bot.send_message, chat_id, "⚠️ حدث خطأ، راجع السجلات.")


async def _reply(msg, text: str, kb: Optional[Markup] = None) -> None:
    await tg_call(bot.send_message, msg.chat.id, text, parse_mode="HTML", reply_markup=kb)


async def _on_content(msg, st) -> None:
    d = st["data"]
    if msg.photo:
        d["image"], d["text"] = msg.photo[-1].file_id, (msg.caption or "").strip()
    elif msg.text:
        d["image"], d["text"] = None, msg.text.strip()
    else:
        await _reply(msg, "⚠️ أرسل صورة أو نصاً.", _cancel_markup())
        return
    if len(d["text"]) > MAX_QUESTION_CHARS:
        await _reply(msg, f"⚠️ نص السؤال طويل ({len(d['text'])} حرفاً). الحد الأقصى {MAX_QUESTION_CHARS}.",
                     _cancel_markup())
        return
    if not d["text"] and not d["image"]:
        await _reply(msg, "⚠️ السؤال فارغ.", _cancel_markup())
        return

    if d["kind"] == "tf":
        d["options"] = ["صح", "خطأ"]
        await _ask_correct(msg, st)
    else:
        st["state"] = "W_OPTIONS"
        await _reply(
            msg,
            "🔘 <b>طريقة إدخال الخيارات:</b>\n\n"
            "<b>1️⃣ خيارات نصية من عندك:</b> أرسل الخيارات كل خيار في سطر مستقل (من 2 إلى 6 خيارات).\n\n"
            "━━━━━━━━━━━━\n"
            "<b>2️⃣ إذا كانت الخيارات موجودة في الصورة:</b>\n"
            "أرسل فقط <b>نقطة متبوعة بعدد الخيارات</b> لإنشائها تلقائياً (أ، ب، ج...):\n"
            "• أرسل <code>.2</code> لإنشاء (أ ، ب)\n"
            "• أرسل <code>.3</code> لإنشاء (أ ، ب ، ج)\n"
            "• أرسل <code>.4</code> لإنشاء (أ ، ب ، ج ، د)\n"
            "• أرسل <code>.5</code> أو <code>.6</code>",
            _cancel_markup()
        )


async def _on_options(msg, st) -> None:
    if not msg.text:
        await _reply(msg, "⚠️ أرسل الخيارات نصاً، أو اختصاراً مثل .4", _cancel_markup())
        return

    raw = msg.text.strip()

    # تحويل الأرقام العربية إلى إنجليزية إن وُجدت (مثلاً: .٤ -> .4)
    norm = raw.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))

    # فحص الاختصار السريع: نقطة متبوعة برقم من 2 إلى 6
    if norm.startswith(".") and norm[1:].strip().isdigit():
        num = int(norm[1:].strip())
        if 2 <= num <= 6:
            st["data"]["options"] = [ui.label(i) for i in range(num)]
            await _ask_correct(msg, st)
            return
        else:
            await _reply(msg, "⚠️ عدد الخيارات السريعة يجب أن يكون بين 2 و 6 (مثال: <code>.4</code>).", _cancel_markup())
            return

    # الطريقة اليدوية العادية (إرسال أسطر نصية)
    opts = [_OPTION_PREFIX.sub("", line).strip() for line in msg.text.splitlines()]
    opts = [o for o in opts if o]
    if not (2 <= len(opts) <= 6):
        await _reply(msg, f"⚠️ وصلني {len(opts)} خيار. المطلوب من 2 إلى 6، كل خيار في سطر (أو استخدم الاختصار مثل .4).", _cancel_markup())
        return
    if any(len(o) > MAX_OPTION_CHARS for o in opts):
        await _reply(msg, f"⚠️ أحد الخيارات أطول من {MAX_OPTION_CHARS} حرفاً.", _cancel_markup())
        return
    if len({o.casefold() for o in opts}) != len(opts):
        await _reply(msg, "⚠️ يوجد خياران متطابقان. أعد إرسال الخيارات.", _cancel_markup())
        return
    st["data"]["options"] = opts
    await _ask_correct(msg, st)


async def _ask_correct(msg, st) -> None:
    st["state"] = "W_CORRECT"
    opts = st["data"]["options"]
    kb = Markup()
    if st["data"]["kind"] == "tf":
        kb.row(Btn("✅ صح", callback_data="ad:wo:0"), Btn("❌ خطأ", callback_data="ad:wo:1"))
    else:
        for i, o in enumerate(opts):
            btn_text = f"🔘 {o}" if o == ui.label(i) else f"{ui.label(i)}) {ui.clip(o, 45)}"
            kb.row(Btn(btn_text, callback_data=f"ad:wo:{i}"))
    kb.row(Btn("❌ إلغاء", callback_data="ad:home"))
    await _reply(msg, "✅ ما هي <b>الإجابة الصحيحة</b>؟", kb)


async def _on_explanation(msg, uid: int, d: dict) -> None:
    if not msg.text:
        await _reply(msg, "⚠️ أرسل الشرح نصاً أو اضغط «بدون شرح».")
        return
    expl = msg.text.strip()
    if len(expl) > MAX_EXPLANATION_CHARS:
        await _reply(msg, f"⚠️ الشرح طويل ({len(expl)}). الحد الأقصى {MAX_EXPLANATION_CHARS}.")
        return
    d["expl"] = expl
    await _save_question(msg.chat.id, uid, d)


async def _save_question(chat_id: int, uid: int, d: dict) -> None:
    ok = await db.add_question(
        chapter_id=d["ch"], kind=d["kind"], question_text=d.get("text", ""),
        image_file_id=d.get("image"), options=d["options"], correct_index=d["correct"],
        explanation=d.get("expl", ""),
    )
    kb = Markup()
    if ok:
        sessions.set_admin(uid, "W_KIND_DONE", {"ch": d["ch"]})
        kb.row(Btn("➕ سؤال آخر في نفس الباب", callback_data="ad:wmore"))
        kb.row(Btn("🏠 لوحة التحكم", callback_data="ad:home"))
        n = len(db.get_chapter_questions(d["ch"]))
        text = f"✅ <b>تم حفظ السؤال.</b>\nعدد أسئلة «{ui.esc(ui.clip(_chapter_title(d['ch']), 40))}» الآن: {n}"
    else:
        kb.row(Btn("🏠 لوحة التحكم", callback_data="ad:home"))
        text = "❌ فشل الحفظ. راجع سجلات Render (Logs) وتأكد من تشغيل schema.sql."
        sessions.clear_admin(uid)
    await tg_call(bot.send_message, chat_id, text, parse_mode="HTML", reply_markup=kb)


# ===========================================================================
# تقرير PDF للأسئلة الأصعب
# ===========================================================================
async def _send_report(chat_id: int, ch_id: Optional[int]) -> None:
    rows = await db.get_hard_questions(ch_id)
    if not rows:
        await tg_call(bot.send_message, chat_id,
                      f"✅ لا توجد أسئلة تجاوزت 50% خطأ بعدد محاولات ≥ {config.REPORT_MIN_ATTEMPTS} حتى الآن.")
        return

    items = []
    for r in rows:
        total, wrong = int(r["total"]), int(r["wrong"])
        items.append({
            "id": r["q_id"],
            "answer": r.get("correct_text") or "—",
            "desc": r.get("desc_text") or "—",
            "total": total,
            "wrong": wrong,
            "wrong_pct": round(100 * wrong / total, 1) if total else 0,
        })
    title = "كل الأبواب" if ch_id is None else _chapter_title(ch_id)

    async with _pdf_sem:
        buf = await asyncio.get_running_loop().run_in_executor(None, generate_pdf_report, title, items)
    buf.name = "hard_questions.pdf"
    await tg_call(bot.send_document, chat_id, buf, caption=f"📑 {len(items)} سؤال تعثّر فيه الطلاب")

    # أسئلة الصور لا يظهر نصها في الـ PDF، فنرسل أصعب 5 منها كصور
    for r in [r for r in rows if r.get("image_file_id")][:5]:
        pct = round(100 * int(r["wrong"]) / int(r["total"])) if int(r["total"]) else 0
        await tg_call(bot.send_photo, chat_id, r["image_file_id"],
                      caption=f"#{r['q_id']} — خطأ {pct}% — الإجابة: {r.get('correct_text') or '—'}")


async def _on_edit_content(msg, uid: int, d: dict) -> None:
    text = (msg.caption or msg.text or "").strip()
    img = msg.photo[-1].file_id if msg.photo else None

    # حذف رسالة الأدمن المدخلة فوراً لإبقاء الشات نظيفاً
    await safe_delete(msg.chat.id, msg.message_id)

    if not text and not img:
        err_kb = Markup()
        err_kb.row(Btn("❌ إلغاء", callback_data=f"ad:bk:{d['ch']}:{d['i']}"))
        await render_message(
            msg.chat.id, d.get("mid"), d.get("is_photo", False),
            "⚠️ <b>خطأ:</b> لم يتم إرسال نص أو صورة!\nأرسل السؤال مجدداً:", err_kb, None
        )
        return

    # فحص طول النص لمنع تجاوز حدود تيليجرام
    if text and len(text) > MAX_QUESTION_CHARS:
        err_kb = Markup()
        err_kb.row(Btn("❌ إلغاء", callback_data=f"ad:bk:{d['ch']}:{d['i']}"))
        await render_message(
            msg.chat.id, d.get("mid"), d.get("is_photo", False),
            f"⚠️ <b>خطأ:</b> نص السؤال طويل جداً ({len(text)} حرفاً). الحد الأقصى هو {MAX_QUESTION_CHARS} حرفاً.\nأعد إرساله مختصراً:", err_kb, None
        )
        return

    # تحديث الحقول المرسلة فقط دون مسح الحقول الأخرى
    fields = {}
    if text:
        fields["question_text"] = text
    if img:
        fields["image_file_id"] = img

    ok = await db.update_question(d["qid"], **fields)
    sessions.clear_admin(uid)

    if not ok:
        err_kb = Markup()
        err_kb.row(Btn("🔙 رجوع للبنك", callback_data=f"ad:bk:{d['ch']}:{d['i']}"))
        await render_message(
            msg.chat.id, d.get("mid"), d.get("is_photo", False),
            "⚠️ <b>لم يُحفظ التعديل:</b> إما أن الاتصال تعثر، أو أن التعديل يجعل السؤال غير صالح.", err_kb, None
        )
        return

    # تحديث رسالة البنك الأصلية في مكانها
    qs = db.get_chapter_questions(d["ch"])
    if qs and 0 <= d["i"] < len(qs):
        t, kb, im = ui.build_bank_view(qs[d["i"]], d["i"], len(qs), d["ch"])
        await render_message(msg.chat.id, d.get("mid"), d.get("is_photo", False), t, kb, im)


async def _on_edit_options(msg, st) -> None:
    # حذف رسالة الأدمن المدخلة فوراً
    await safe_delete(msg.chat.id, msg.message_id)

    if not msg.text:
        return

    raw = msg.text.strip().translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    if raw.startswith(".") and raw[1:].strip().isdigit():
        num = int(raw[1:].strip())
        if 2 <= num <= 6:
            opts = [ui.label(i) for i in range(num)]
        else:
            err_kb = Markup()
            err_kb.row(Btn("❌ إلغاء", callback_data=f"ad:bk:{st['data']['ch']}:{st['data']['i']}"))
            await render_message(
                msg.chat.id, st["data"].get("mid"), st["data"].get("is_photo", False),
                "⚠️ <b>خطأ:</b> الاختصار يجب أن يكون بين <code>.2</code> و <code>.6</code>.\nأعد المحاولة:", err_kb, None
            )
            return
    else:
        opts = [_OPTION_PREFIX.sub("", line).strip() for line in msg.text.splitlines() if line.strip()]
        if not (2 <= len(opts) <= 6):
            err_kb = Markup()
            err_kb.row(Btn("❌ إلغاء", callback_data=f"ad:bk:{st['data']['ch']}:{st['data']['i']}"))
            await render_message(
                msg.chat.id, st["data"].get("mid"), st["data"].get("is_photo", False),
                f"⚠️ <b>خطأ:</b> وصلني {len(opts)} خيارات. المطلوب من 2 إلى 6، كل خيار في سطر.\nأعد المحاولة:", err_kb, None
            )
            return
        if len({o.casefold() for o in opts}) != len(opts):
            err_kb = Markup()
            err_kb.row(Btn("❌ إلغاء", callback_data=f"ad:bk:{st['data']['ch']}:{st['data']['i']}"))
            await render_message(
                msg.chat.id, st["data"].get("mid"), st["data"].get("is_photo", False),
                "⚠️ <b>خطأ:</b> يوجد خياران متطابقان!\nأعد إرسال الخيارات بدون تكرار:", err_kb, None
            )
            return

    st["data"]["options"] = opts
    st["state"] = "EQ_CORRECT"
    kb = Markup()
    for i, o in enumerate(opts):
        btn_text = f"🔘 {o}" if o == ui.label(i) else f"{ui.label(i)}) {ui.clip(o, 45)}"
        kb.row(Btn(btn_text, callback_data=f"ad:eq_ans:{i}:{st['data']['ch']}:{st['data']['i']}"))
    kb.row(Btn("❌ إلغاء", callback_data=f"ad:bk:{st['data']['ch']}:{st['data']['i']}"))

    mid, is_photo = await render_message(
        msg.chat.id, st["data"].get("mid"), st["data"].get("is_photo", False),
        "✅ اختر الآن <b>الإجابة الصحيحة</b> الجديدة:", kb, None
    )
    st["data"]["mid"] = mid
    st["data"]["is_photo"] = is_photo


async def _on_edit_explanation(msg, uid: int, d: dict) -> None:
    await safe_delete(msg.chat.id, msg.message_id)

    if not msg.text:
        return

    expl = msg.text.strip()
    if len(expl) > MAX_EXPLANATION_CHARS:
        err_kb = Markup()
        err_kb.row(Btn("⏭️ حذف الشرح", callback_data=f"ad:eqe_skip:{d['qid']}:{d['ch']}:{d['i']}"))
        err_kb.row(Btn("❌ إلغاء", callback_data=f"ad:bk:{d['ch']}:{d['i']}"))
        await render_message(
            msg.chat.id, d.get("mid"), d.get("is_photo", False),
            f"⚠️ <b>الشرح طويل جداً</b> ({len(expl)} حرفاً). الحد الأقصى {MAX_EXPLANATION_CHARS}.\nأرسل شرحاً مختصراً:", err_kb, None
        )
        return

    ok = await db.update_question(d["qid"], explanation=expl)
    sessions.clear_admin(uid)

    if not ok:
        err_kb = Markup()
        err_kb.row(Btn("🔙 رجوع للبنك", callback_data=f"ad:bk:{d['ch']}:{d['i']}"))
        await render_message(
            msg.chat.id, d.get("mid"), d.get("is_photo", False),
            "⚠️ <b>فشل حفظ الشرح:</b> يرجى المحاولة مرة أخرى لاحقاً.", err_kb, None
        )
        return

    qs = db.get_chapter_questions(d["ch"])
    if qs and 0 <= d["i"] < len(qs):
        t, kb, im = ui.build_bank_view(qs[d["i"]], d["i"], len(qs), d["ch"])
        await render_message(msg.chat.id, d.get("mid"), d.get("is_photo", False), t, kb, im)
