"""نقطة التشغيل: خادم الـ webhook، المهام الخلفية، والإيقاف الرشيق."""
import asyncio
import collections
import hmac
import logging
import signal

import aiohttp
import telebot
from aiohttp import web
from telebot.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

import config
import database as db
from state_manager import sessions
from tg import bot, tg_call

# استيراد المعالجات يسجّلها على البوت
import handlers_student  # noqa: F401
import handlers_admin  # noqa: F401

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("quizbot")

_stopping = asyncio.Event()
_inflight: set = set()
_background: set = set()

# إسقاط التحديثات المكررة (تيليجرام قد يعيد إرسال التحديث عند بطء الرد)
_seen_ids = collections.deque(maxlen=5000)
_seen_set: set = set()


def _remember(update_id: int) -> bool:
    """True إن كان التحديث جديداً."""
    if update_id in _seen_set:
        return False
    if len(_seen_ids) == _seen_ids.maxlen:
        _seen_set.discard(_seen_ids[0])
    _seen_ids.append(update_id)
    _seen_set.add(update_id)
    return True


def spawn_background(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


# ===========================================================================
# Webhook
# ===========================================================================
async def _process(update) -> None:
    try:
        await bot.process_new_updates([update])
    except Exception:
        log.exception("update processing failed")


async def handle_webhook(request: web.Request) -> web.Response:
    if _stopping.is_set():
        return web.Response(status=503)
    token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(token, config.WEBHOOK_SECRET):
        return web.Response(status=403)
    if len(_inflight) >= config.MAX_PENDING_UPDATES:     # حماية من الانهيار: تيليجرام ستعيد المحاولة لاحقاً
        return web.Response(status=503)
    try:
        update = telebot.types.Update.de_json(await request.text())
    except Exception:
        log.exception("bad webhook body")
        return web.Response(text="ok")
    if not _remember(update.update_id):
        return web.Response(text="ok")

    task = asyncio.create_task(_process(update))
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)
    return web.Response(text="ok")


async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


# ===========================================================================
# مهام خلفية
# ===========================================================================
async def cleanup_loop() -> None:
    while True:
        await asyncio.sleep(300)
        sessions.cleanup()


async def setup_commands() -> None:
    try:
        await tg_call(bot.set_my_commands, [BotCommand("start", "📖 القائمة الرئيسية")],
                      scope=BotCommandScopeDefault())
        for admin_id in config.ADMIN_IDS:
            await tg_call(bot.set_my_commands,
                          [BotCommand("start", "📖 القائمة الرئيسية"), BotCommand("admin", "👑 لوحة التحكم")],
                          scope=BotCommandScopeChat(chat_id=admin_id))
    except Exception as e:
        log.warning("set_my_commands: %s", e)


# ===========================================================================
# التشغيل والإيقاف
# ===========================================================================
def _check_env() -> None:
    missing = [n for n, v in (("BOT_TOKEN", config.BOT_TOKEN), ("SUPABASE_URL", config.SUPABASE_URL),
                              ("SUPABASE_KEY", config.SUPABASE_KEY)) if not v]
    if missing:
        raise SystemExit("متغيرات بيئة ناقصة: " + ", ".join(missing))
    if not config.ADMIN_IDS:
        log.warning("ADMIN_IDS فارغ: لن يستطيع أحد فتح لوحة التحكم")


async def main() -> None:
    _check_env()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _stopping.set)
        except NotImplementedError:      # ويندوز
            pass

    for attempt in range(1, 6):          # تحميل الأسئلة قبل استقبال الطلاب
        if await db.refresh_content():
            log.info("تم تحميل المحتوى: %d باب", len(db.get_chapters()))
            break
        log.warning("تعذّر تحميل المحتوى (محاولة %d)", attempt)
        await asyncio.sleep(min(2 * attempt, 10))

    spawn_background(db.content_refresh_loop())
    spawn_background(db.flush_loop())
    spawn_background(cleanup_loop())
    await setup_commands()

    runner = None
    if config.RENDER_EXTERNAL_URL:
        app = web.Application()
        app.router.add_post("/webhook", handle_webhook)
        app.router.add_get("/", health)
        app.router.add_get("/health", health)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", config.PORT).start()
        await bot.set_webhook(
            url=f"{config.RENDER_EXTERNAL_URL}/webhook",
            secret_token=config.WEBHOOK_SECRET,
            allowed_updates=["message", "callback_query"],
            max_connections=40,
        )
        log.info("🚀 البوت يعمل (webhook) على المنفذ %s", config.PORT)
        await _stopping.wait()
    else:
        log.info("🚀 البوت يعمل (polling محلي)")
        await bot.remove_webhook()
        poll = asyncio.create_task(bot.polling(non_stop=True))
        await _stopping.wait()
        poll.cancel()

    # ---------------- إيقاف رشيق: أكمل ما بدأ، ثم احفظ المحاولات المتراكمة ----------------
    log.info("إيقاف: انتظار التحديثات الجارية...")
    if _inflight:
        await asyncio.wait(set(_inflight), timeout=8)
    await db.flush_writes()
    for task in list(_background):
        task.cancel()
    if runner:
        await runner.cleanup()
    try:
        await bot.close_session()
    except Exception:
        pass
    log.info("تم الإيقاف")


if __name__ == "__main__":
    asyncio.run(main())
