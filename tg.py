"""طبقة تيليجرام: حد معدل عام + إعادة المحاولة عند 429 + إرسال/تعديل ذكي للرسائل."""
import asyncio
import logging
import time
import aiohttp
from typing import Optional, Tuple

from telebot.async_telebot import AsyncTeleBot
from telebot.types import InputMediaPhoto

try:
    from telebot.asyncio_helper import ApiTelegramException
except ImportError:  # pragma: no cover
    from telebot.apihelper import ApiTelegramException

import config

log = logging.getLogger("quizbot.tg")


class _ErrorHandler:
    """يمنع انهيار المعالجة، ويسجّل الخطأ مع تفاصيله."""

    def handle(self, exception):
        log.error("telebot handler error: %r", exception, exc_info=exception)
        return True


bot = AsyncTeleBot(config.BOT_TOKEN or "0:missing", exception_handler=_ErrorHandler())


# ---------------------------------------------------------------------------
# حد المعدل: يوزّع الطلبات على الزمن بدل أن تنفجر دفعة واحدة فتُحظر من تيليجرام
# ---------------------------------------------------------------------------
class RateLimiter:
    def __init__(self, rate_per_sec: float):
        self._interval = 1.0 / max(rate_per_sec, 0.1)
        self._next = 0.0
        self._blocked_until = 0.0

    async def wait(self) -> None:
        while True:
            now = time.monotonic()
            if self._blocked_until > now:
                await asyncio.sleep(self._blocked_until - now)
                continue
            start = max(now, self._next)
            self._next = start + self._interval
            delay = start - now
            if delay > 0:
                await asyncio.sleep(delay)
            if time.monotonic() < self._blocked_until:
                continue
            break

    def block_for(self, seconds: float) -> None:
        now = time.monotonic()
        self._blocked_until = max(self._blocked_until, now + seconds)
        self._next = max(self._next, now + seconds)


_limiter = RateLimiter(config.TG_RATE_PER_SEC)


def _retry_after(e: Exception) -> float:
    try:
        return min(float(e.result_json["parameters"]["retry_after"]), 30.0)
    except Exception:
        return 1.0


def error_text(e: Exception) -> str:
    return (str(getattr(e, "description", "") or "") + " " + str(e)).lower()


def is_not_modified(e: Exception) -> bool:
    return "message is not modified" in error_text(e)


async def tg_call(method, *args, limited: bool = True, retries: int = 4, **kwargs):
    """ينفّذ طلباً لتيليجرام مع حد المعدل وإعادة المحاولة التلقائية عند 429 أو أخطاء الشبكة المؤقتة."""
    attempt = 0
    while True:
        if limited:
            await _limiter.wait()
        try:
            return await method(*args, **kwargs)
        except ApiTelegramException as e:
            if getattr(e, "error_code", None) == 429 and attempt < retries:
                wait = _retry_after(e) + 0.25
                _limiter.block_for(wait)
                attempt += 1
                log.warning("429 من تيليجرام: انتظار %.1f ثانية (محاولة %d)", wait, attempt)
                await asyncio.sleep(wait)
                continue
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as net_err:
            if attempt < retries:
                attempt += 1
                wait = 0.5 * attempt
                log.warning("خطأ شبكة مؤقت (%s): إعادة المحاولة بعد %.1f ثانية (محاولة %d)", net_err, wait, attempt)
                await asyncio.sleep(wait)
                continue
            raise


async def answer_cb(call_id: str, text: Optional[str] = None, alert: bool = False) -> None:
    """الرد على الضغطة (يُوقف دوران الزر). لا يخضع لحد الإرسال ولا يُسقط المعالجة عند فشله."""
    try:
        await tg_call(bot.answer_callback_query, call_id, text=text, show_alert=alert,
                      limited=False, retries=1)
    except Exception as e:  # query is too old / already answered
        log.debug("answer_callback_query: %s", e)


async def safe_delete(chat_id: int, message_id: Optional[int]) -> None:
    if not message_id:
        return
    try:
        await tg_call(bot.delete_message, chat_id, message_id)
    except Exception as e:
        log.debug("delete_message: %s", e)


async def render_message(
    chat_id: int,
    cur_msg_id: Optional[int],
    cur_is_photo: bool,
    text: str,
    markup,
    image_id: Optional[str] = None,
    same_media: bool = False,
) -> Tuple[int, bool]:
    """
    يعرض (text, markup[, image]) في الرسالة الحالية إن أمكن تعديلها، وإلا يحذفها ويرسل جديدة.
      - صورة → صورة : تعديل الوسائط (أو الكابشن فقط إن كانت الصورة نفسها).
      - نص   → نص   : تعديل النص.
      - اختلاف النوع: حذف + إرسال (تيليجرام لا يسمح بتحويل نص إلى صورة بالتعديل).
    يُرجع (message_id, is_photo) للرسالة النهائية.
    """
    want_photo = bool(image_id)

    if cur_msg_id:
        try:
            if want_photo and cur_is_photo:
                if same_media:
                    await tg_call(bot.edit_message_caption, text, chat_id, cur_msg_id,
                                  parse_mode="HTML", reply_markup=markup)
                else:
                    await tg_call(bot.edit_message_media,
                                  InputMediaPhoto(image_id, caption=text, parse_mode="HTML"),
                                  chat_id, cur_msg_id, reply_markup=markup)
                return cur_msg_id, True
            if not want_photo and not cur_is_photo:
                await tg_call(bot.edit_message_text, text, chat_id, cur_msg_id,
                              parse_mode="HTML", reply_markup=markup)
                return cur_msg_id, False
        except ApiTelegramException as e:
            if is_not_modified(e):
                return cur_msg_id, cur_is_photo
            if getattr(e, "error_code", None) == 429:
                raise
            log.info("تعذّر التعديل (%s) → سيُستبدل بإرسال جديد", e)
        await safe_delete(chat_id, cur_msg_id)

    if want_photo:
        sent = await tg_call(bot.send_photo, chat_id, image_id, caption=text,
                             parse_mode="HTML", reply_markup=markup)
        return sent.message_id, True
    sent = await tg_call(bot.send_message, chat_id, text, parse_mode="HTML", reply_markup=markup)
    return sent.message_id, False
