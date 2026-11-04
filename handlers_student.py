"""معالجات الطالب: القائمة، الاختبار (آلة حالات)، النتيجة، المراجعة."""
import logging
from typing import Any, Dict
import random

import config
import database as db
import ui
from state_manager import sessions
from tg import answer_cb, bot, render_message, tg_call

log = logging.getLogger("quizbot.student")

KNOWN_KINDS = {ui.CB_ANSWER, ui.CB_NEXT, ui.CB_SKIP, ui.CB_END,
               ui.CB_CHAPTER, ui.CB_RETRY, ui.CB_MENU, ui.CB_LOCKED}


def _menu_view(uid: int):
    chapters = db.get_chapters()
    counts = {c["id"]: len(db.get_chapter_questions(c["id"])) for c in chapters}
    return ui.build_menu_view(chapters, counts, uid in config.ADMIN_IDS)


# ===========================================================================
# العرض: يُنتج الرسالة الحية للجلسة من حالتها الحالية
# ===========================================================================
async def present(chat_id: int, s: Dict[str, Any]) -> None:
    """يعرض حالة الجلسة الحالية (سؤال / سؤال مُجاب / نتيجة) بتعديل رسالة واحدة."""
    # تخطَّ الأسئلة المحذوفة من البنك أثناء الجلسة
    while s["idx"] < len(s["q_ids"]) and db.get_question(s["q_ids"][s["idx"]]) is None:
        s["idx"] += 1
        s["state"], s["chosen"] = "ask", None

    s["dirty"] = True
    if s["idx"] >= len(s["q_ids"]):
        res = sessions.compute_result(s, lambda i: db.get_question(i) is not None)
        s["state"] = "done"
        s["retry_ids"] = res["retry_ids"]
        text, kb = ui.build_summary_view(res)
        s["msg_id"], s["msg_is_photo"] = await render_message(
            chat_id, s["msg_id"], s["msg_is_photo"], text, kb, None)
        s["shown_qid"] = None
        s["dirty"] = False
        return

    q = db.get_question(s["q_ids"][s["idx"]])
    alive = [i for i in s["q_ids"] if db.get_question(i) is not None]
    chosen = s["chosen"] if s["state"] == "answered" else None
    text, kb, image = ui.build_question_view(
        q, alive.index(q["id"]) + 1, len(alive), s["idx"], chosen, is_last=(q["id"] == alive[-1]))
    s["msg_id"], s["msg_is_photo"] = await render_message(
        chat_id, s["msg_id"], s["msg_is_photo"], text, kb, image,
        same_media=(s["shown_qid"] == q["id"]))
    s["shown_qid"] = q["id"]
    s["dirty"] = False


# ===========================================================================
# الأوامر
# ===========================================================================
@bot.message_handler(commands=["start"])
async def cmd_start(msg):
    uid = msg.from_user.id
    sessions.clear(uid)
    sessions.clear_admin(uid)
    db.queue_user(uid, msg.from_user.first_name, msg.from_user.username)
    text, kb = _menu_view(uid)
    await tg_call(bot.send_message, msg.chat.id, text, parse_mode="HTML", reply_markup=kb)


# ===========================================================================
# الأزرار
# ===========================================================================
@bot.callback_query_handler(func=lambda c: not (c.data or "").startswith(ui.CB_ADMIN + ":"))
async def on_callback(call):
    uid = call.from_user.id
    data = call.data or ""
    msg = call.message
    if msg is None:
        await answer_cb(call.id, "الرسالة قديمة، أرسل /start", True)
        return
    if sessions.throttled(uid):          # ضغط متكرر بسرعة: يُتجاهل بصمت
        await answer_cb(call.id)
        return

    async with sessions.lock(uid):        # إجراءات الطالب الواحد تُنفَّذ بالتسلسل
        try:
            await _route(call, uid, msg, data)
        except Exception:
            log.exception("callback failed uid=%s data=%s", uid, data)
            await answer_cb(call.id)
            try:
                await tg_call(bot.send_message, msg.chat.id,
                              "⚠️ حدث خطأ مؤقت. حاول مرة أخرى أو أرسل /start")
            except Exception:
                pass


async def _route(call, uid: int, msg, data: str) -> None:
    chat_id, mid = msg.chat.id, msg.message_id
    kind, _, rest = data.partition(":")

    if kind not in KNOWN_KINDS:
        await answer_cb(call.id, "هذه رسالة قديمة، أرسل /start", True)
        return

    if kind == ui.CB_LOCKED:
        msg_text = "⏳ هذا الباب قيد إعداد الأسئلة حالياً" if rest == "empty" else "🔒 هذا الباب مقفل حالياً"
        await answer_cb(call.id, msg_text, True)
        return

    if kind == ui.CB_MENU:                                   # العودة للقائمة
        sessions.clear(uid)
        await answer_cb(call.id)
        text, kb = _menu_view(uid)
        await render_message(chat_id, mid, bool(getattr(msg, "photo", None)), text, kb, None)
        return

    if kind == ui.CB_CHAPTER:                                # بدء باب
        chapter = next((c for c in db.get_chapters() if str(c["id"]) == rest), None)
        if chapter is None or not chapter["is_active"]:
            await answer_cb(call.id, "🔒 هذا الباب مقفل حالياً", True)
            return
        qs = db.get_chapter_questions(chapter["id"])
        if not qs:
            await answer_cb(call.id, "لا توجد أسئلة في هذا الباب بعد", True)
            return
        await answer_cb(call.id)
        q_ids = [q["id"] for q in qs]
        random.shuffle(q_ids)
        s = sessions.start_quiz(uid, chapter["id"], q_ids)
        s["msg_id"], s["msg_is_photo"] = mid, bool(getattr(msg, "photo", None))
        await present(chat_id, s)
        return

    s = sessions.get(uid)
    if s is None:
        await answer_cb(call.id, "⌛ انتهت الجلسة، أرسل /start", True)
        return

    if mid != s["msg_id"]:                                   # رسالة قديمة لا تتحكم بالجلسة الحالية
        await answer_cb(call.id)
        return

    if kind == ui.CB_RETRY:                                  # مراجعة الأخطاء والمتخطّى
        ids = [i for i in s["retry_ids"] if db.get_question(i) is not None]
        if s["state"] != "done" or not ids:
            await answer_cb(call.id)
            return
        await answer_cb(call.id)
        random.shuffle(ids)
        s2 = sessions.start_quiz(uid, s["chapter_id"], ids)
        s2["msg_id"], s2["msg_is_photo"] = s["msg_id"], s["msg_is_photo"]
        await present(chat_id, s2)
        return

    if kind == ui.CB_END:                                    # إنهاء مبكر → النتيجة
        if s["state"] == "done":
            await answer_cb(call.id)
            return
        await answer_cb(call.id)
        s["idx"], s["state"], s["chosen"] = len(s["q_ids"]), "ask", None
        await present(chat_id, s)
        return

    # ---- من هنا: الأزرار الخاصة بسؤال معيّن (a / n / s) ----
    if s["idx"] >= len(s["q_ids"]):                          # لا سؤال حالي (انتهت الجلسة أو فشل عرض النتيجة)
        await answer_cb(call.id)
        if s["state"] != "done":
            await present(chat_id, s)
        return
    parts = rest.split(":")
    token = int(parts[0]) if parts and parts[0].isdigit() else -1
    expected_state = "answered" if kind == ui.CB_NEXT else "ask"
    if token != s["idx"] or s["state"] != expected_state:     # ضغطة قديمة أو مكررة
        await answer_cb(call.id)
        if s["dirty"] and s["state"] != "done":              # فشل العرض السابق: أعد المزامنة
            await present(chat_id, s)
        return

    if kind == ui.CB_ANSWER:
        q = db.get_question(s["q_ids"][s["idx"]])
        k = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else -1
        if q is None or not (0 <= k < len(q["options"])):
            await answer_cb(call.id, "تم تعديل هذا السؤال")
            s["idx"] += 1
            await present(chat_id, s)
            return
        is_correct = (k == q["correct_index"])
        s["answers"][q["id"]] = is_correct
        s["state"], s["chosen"] = "answered", k
        db.queue_attempt(uid, s["chapter_id"], q["id"], is_correct)
        await answer_cb(call.id, "✅ صحيح!" if is_correct else "❌ خطأ")
        await present(chat_id, s)
        return

    if kind == ui.CB_NEXT:
        s["idx"] += 1
        s["state"], s["chosen"] = "ask", None
        await answer_cb(call.id)
        await present(chat_id, s)
        return

    if kind == ui.CB_SKIP:
        s["idx"] += 1
        s["state"], s["chosen"] = "ask", None
        await answer_cb(call.id)
        await present(chat_id, s)
        return
