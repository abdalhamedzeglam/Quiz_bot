"""
طبقة البيانات (Supabase).

المبدأ الذي يجعل البوت يتحمل أعداداً كبيرة:
  1) القراءة: كل الأبواب والأسئلة تُحمَّل في الذاكرة وتُحدَّث كل دقيقة (وفوراً عند تعديل الأدمن).
     الطالب لا يُرسل أي طلب إلى Supabase أثناء التدريب إطلاقاً.
  2) الكتابة: محاولات الطلاب والمستخدمون الجدد تتجمّع في الذاكرة وتُرسل دفعة واحدة كل ثانيتين،
     وأي صف فاسد لا يعطّل بقية الدفعة.
"""
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from supabase import create_client
from supabase.lib.client_options import ClientOptions

import config

log = logging.getLogger("quizbot.db")

supabase = None
if config.SUPABASE_URL and config.SUPABASE_KEY:
    supabase = create_client(
        config.SUPABASE_URL,
        config.SUPABASE_KEY,
        options=ClientOptions(postgrest_client_timeout=10),
    )
else:
    log.warning("SUPABASE_URL / SUPABASE_KEY غير مضبوطين: قاعدة البيانات معطّلة")

_executor = ThreadPoolExecutor(max_workers=config.DB_THREADS, thread_name_prefix="db")


async def _run(fn):
    return await asyncio.get_running_loop().run_in_executor(_executor, fn)


# ===========================================================================
# 1) المحتوى (أبواب + أسئلة) في الذاكرة
# ===========================================================================
class Content:
    """نسخة غير قابلة للتعديل من المحتوى؛ تُستبدل كاملةً عند كل تحديث (ذرّياً)."""

    __slots__ = ("chapters", "by_id", "by_chapter")

    def __init__(self, chapters: List[dict], questions: List[dict]):
        self.chapters = sorted(chapters, key=lambda c: c["id"])
        self.by_id = {q["id"]: q for q in questions}
        self.by_chapter: Dict[int, List[dict]] = {}
        for q in sorted(questions, key=lambda q: q["id"]):
            self.by_chapter.setdefault(q["chapter_id"], []).append(q)


_content = Content([], [])
_content_ready = False
_refresh_lock = asyncio.Lock()


def normalize_question(row: dict) -> Optional[dict]:
    """يتحقق من صف سؤال؛ يُرجع None إن كان غير صالح (يُتجاهل ولا يُعرض للطلاب)."""
    opts = row.get("options")
    if not isinstance(opts, list):
        return None
    opts = [str(o).strip() for o in opts]
    if not (2 <= len(opts) <= 6) or any(not o for o in opts):
        return None
    ci = row.get("correct_index")
    if isinstance(ci, bool) or not isinstance(ci, int) or not (0 <= ci < len(opts)):
        return None
    kind = row.get("kind") if row.get("kind") in ("tf", "choice") else "choice"
    text = (row.get("question_text") or "").strip()
    image = (row.get("image_file_id") or "").strip() or None
    if not text and not image:
        return None
    return {
        "id": row["id"],
        "chapter_id": row["chapter_id"],
        "kind": kind,
        "question_text": text,
        "image_file_id": image,
        "hint": (row.get("hint") or "").strip(),
        "options": opts,
        "correct_index": ci,
        "explanation": (row.get("explanation") or "").strip(),
    }


def _fetch_all(table: str) -> List[dict]:
    """يجلب كل الصفوف على دفعات (حد PostgREST الافتراضي 1000 صف)."""
    rows: List[dict] = []
    start, step = 0, 1000
    while True:
        res = supabase.table(table).select("*").order("id").range(start, start + step - 1).execute()
        batch = res.data or []
        rows.extend(batch)
        if len(batch) < step:
            return rows
        start += step


def _load_all() -> Tuple[List[dict], List[dict]]:
    return _fetch_all("chapters"), _fetch_all("questions")


async def refresh_content() -> bool:
    """يعيد تحميل المحتوى. عند الفشل يبقى المحتوى القديم قيد الاستخدام."""
    global _content, _content_ready
    if not supabase:
        return False
    async with _refresh_lock:
        try:
            chapters, rows = await _run(_load_all)
        except Exception as e:
            log.error("refresh_content failed: %s", e)
            return False
        valid, invalid = [], 0
        for row in rows:
            q = normalize_question(row)
            if q:
                valid.append(q)
            else:
                invalid += 1
        if invalid:
            log.warning("تم تجاهل %d سؤالاً غير صالح (راجع schema.sql القسم 3)", invalid)
        _content = Content(chapters, valid)
        _content_ready = True
        return True


def content_ready() -> bool:
    return _content_ready


def get_chapters() -> List[dict]:
    return [c.copy() for c in _content.chapters]


def get_chapter_questions(chapter_id: int) -> List[dict]:
    return _content.by_chapter.get(chapter_id, [])


def get_question(question_id: int) -> Optional[dict]:
    return _content.by_id.get(question_id)


# ===========================================================================
# 2) عمليات الأدمن (كتابة ثم تحديث فوري للذاكرة)
# ===========================================================================
async def add_question(
    chapter_id: int,
    kind: str,
    question_text: str,
    image_file_id: Optional[str],
    options: List[str],
    correct_index: int,
    explanation: str,
) -> bool:
    if not supabase:
        return False
    row = {
        "chapter_id": chapter_id,
        "kind": kind,
        "question_text": question_text or "",
        "image_file_id": image_file_id,
        "hint": "",
        "options": options,
        "correct_index": correct_index,
        "explanation": explanation or "",
    }
    try:
        await _run(lambda: supabase.table("questions").insert(row).execute())
    except Exception as e:
        log.error("add_question: %s", e)
        return False
    await refresh_content()
    return True


async def delete_question(question_id: int) -> bool:
    if not supabase:
        return False
    try:
        await _run(lambda: supabase.table("questions").delete().eq("id", question_id).execute())
    except Exception as e:
        log.error("delete_question: %s", e)
        return False
    await refresh_content()
    return True


async def update_question(question_id: int, **fields) -> bool:
    if not supabase:
        return False
    # تنظيف الحقول التي قيمتها None حتى لا نمسح الحقول التي لم تتغير
    clean_fields = {k: v for k, v in fields.items() if v is not None}
    if not clean_fields:
        return False
    try:
        await _run(lambda: supabase.table("questions").update(clean_fields).eq("id", question_id).execute())
    except Exception as e:
        log.error("update_question: %s", e)
        return False
    await refresh_content()
    return True


async def toggle_chapter(chapter_id: int) -> Optional[bool]:
    """يبدّل حالة الباب ذرّياً (دالة SQL) ويُرجع الحالة الجديدة."""
    if not supabase:
        return None
    try:
        res = await _run(lambda: supabase.rpc("toggle_chapter", {"p_id": chapter_id}).execute())
        data = res.data
        if isinstance(data, list):
            data = data[0] if data else None
            if isinstance(data, dict):
                data = next(iter(data.values()), None)
        new_state = bool(data) if data is not None else None
    except Exception as e:
        log.error("toggle_chapter: %s", e)
        return None
    await refresh_content()
    return new_state


async def get_bot_stats() -> Tuple[int, int, int]:
    """(الطلاب، المحاولات، الأسئلة) بطلب واحد."""
    if not supabase:
        return 0, 0, len(_content.by_id)
    try:
        res = await _run(lambda: supabase.rpc("bot_stats", {}).execute())
        row = res.data[0] if isinstance(res.data, list) else res.data
        return int(row["users"]), int(row["attempts"]), int(row["questions"])
    except Exception as e:
        log.error("get_bot_stats: %s", e)
        return 0, 0, len(_content.by_id)


async def get_hard_questions(chapter_id: Optional[int] = None) -> List[dict]:
    if not supabase:
        return []
    params: Dict[str, Any] = {"p_min": config.REPORT_MIN_ATTEMPTS}
    if chapter_id is not None:
        params["p_chapter"] = chapter_id
    try:
        res = await _run(lambda: supabase.rpc("hard_questions", params).execute())
        return res.data or []
    except Exception as e:
        log.error("get_hard_questions: %s", e)
        return []


# ===========================================================================
# 3) كتابات مجمَّعة: محاولات الطلاب + المستخدمون الجدد
# ===========================================================================
MAX_PENDING = 20000

_pending_attempts: Dict[Tuple[int, int], dict] = {}
_pending_users: Dict[int, dict] = {}
_known_users: set = set()
_flush_lock = asyncio.Lock()


def queue_attempt(user_id: int, chapter_id: int, question_id: int, is_correct: bool) -> None:
    """أول محاولة فقط تُحتسب: setdefault + ignore_duplicates في القاعدة."""
    if len(_pending_attempts) >= MAX_PENDING:
        log.error("طابور المحاولات ممتلئ؛ تم تجاهل محاولة")
        return
    key = (user_id, question_id)
    if key not in _pending_attempts:
        _pending_attempts[key] = {"user_id": user_id, "chapter_id": chapter_id,
                                  "question_id": question_id, "is_correct": is_correct}


def queue_user(user_id: int, first_name: Optional[str], username: Optional[str]) -> None:
    if user_id in _known_users:
        return
    if len(_known_users) > 200_000:
        _known_users.clear()
    _known_users.add(user_id)
    _pending_users[user_id] = {"user_id": user_id, "first_name": first_name or "", "username": username or ""}


def pending_counts() -> Tuple[int, int]:
    return len(_pending_attempts), len(_pending_users)


def _is_permanent(e: Exception) -> bool:
    """خطأ خاص بصف معيّن (قيد/نوع بيانات): إعادة المحاولة لن تنفع، يجب عزل الصف."""
    code = str(getattr(e, "code", "") or "")
    return code[:2] in ("22", "23")


async def _upsert(table: str, rows: List[dict], on_conflict: str) -> None:
    def _do():
        return supabase.table(table).upsert(rows, on_conflict=on_conflict, ignore_duplicates=True).execute()

    await _run(_do)


async def _send_chunk(table: str, items: List[Tuple[Any, dict]], on_conflict: str, store: dict) -> None:
    rows = [r for _, r in items]
    try:
        await _upsert(table, rows, on_conflict)
        return
    except Exception as e:
        if _is_permanent(e):
            if len(items) == 1:
                log.error("حذف صف فاسد من %s: %s | %s", table, items[0][1], e)
                return
            mid = len(items) // 2
            await _send_chunk(table, items[:mid], on_conflict, store)
            await _send_chunk(table, items[mid:], on_conflict, store)
            return
        log.warning("فشل مؤقت في كتابة %s (%s): ستُعاد المحاولة", table, e)
        for key, row in items:
            if len(store) < MAX_PENDING:
                store.setdefault(key, row)


async def _flush(table: str, store: dict, on_conflict: str) -> None:
    if not store or not supabase:
        return
    items = list(store.items())
    store.clear()
    for i in range(0, len(items), 500):
        await _send_chunk(table, items[i:i + 500], on_conflict, store)


async def flush_writes() -> None:
    async with _flush_lock:
        await _flush("bot_users", _pending_users, "user_id")
        await _flush("first_attempts", _pending_attempts, "user_id,question_id")


async def flush_loop() -> None:
    while True:
        await asyncio.sleep(config.FLUSH_INTERVAL_SEC)
        try:
            await flush_writes()
        except Exception:
            log.exception("flush_loop")


async def content_refresh_loop() -> None:
    """
    فحص احتياطي بفاصل طويل (CONTENT_REFRESH_SEC). لكن إن لم يكن المحتوى جاهزاً أو فشل آخر تحديث
    يُعاد المحاولة كل 20 ثانية، حتى لا يبقى البوت بلا أسئلة ساعات بسبب تعثّر عابر في Supabase.
    """
    ok = _content_ready
    while True:
        await asyncio.sleep(config.CONTENT_REFRESH_SEC if ok else 20)
        try:
            ok = await refresh_content()
        except Exception:
            log.exception("content_refresh_loop")
            ok = False
