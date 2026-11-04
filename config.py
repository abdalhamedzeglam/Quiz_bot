"""الإعدادات: كل شيء يُقرأ من متغيرات البيئة (Render → Environment)."""
import hashlib
import os
import re

from dotenv import load_dotenv

load_dotenv()

# ---------- أسرار وروابط (إلزامية) ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()  # مفتاح service_role

_raw_admins = os.getenv("ADMIN_IDS") or os.getenv("ADMIN_ID", "")
ADMIN_IDS = {int(x) for x in re.split(r"[,\s]+", _raw_admins) if x.strip().isdigit()}

# Render يضبط هذا المتغير تلقائياً. إن كان فارغاً يعمل البوت بوضع polling محلياً.
RENDER_EXTERNAL_URL = (os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
PORT = int(os.getenv("PORT", "8080"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET") or hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:48]


# ---------- ضبط الأداء (القيم الافتراضية مناسبة لـ Render المجاني) ----------
def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return int(default)


# حد إرسال/تعديل الرسائل إلى تيليجرام في الثانية (حدّه الرسمي نحو 30)
TG_RATE_PER_SEC = _float("TG_RATE_PER_SEC", 26)
# أقل فاصل بين ضغطتين من نفس الطالب (ثانية)
USER_THROTTLE_SEC = _float("USER_THROTTLE_SEC", 0.5)
# كل كم ثانية يُعاد تحميل الأبواب والأسئلة من Supabase (فحص احتياطي فقط)
CONTENT_REFRESH_SEC = _int("CONTENT_REFRESH_SEC", 21600)   # 6 ساعات؛ تعديلات الأدمن من البوت تُحدَّث فوراً، وزر «تحديث البيانات» يدوياً
# كل كم ثانية تُكتب المحاولات المتراكمة في Supabase دفعة واحدة
FLUSH_INTERVAL_SEC = _float("FLUSH_INTERVAL_SEC", 2)
# أقصى عدد خيوط لاتصالات Supabase
DB_THREADS = _int("DB_THREADS", 8)
# مدة بقاء الجلسة الخاملة قبل حذفها من الذاكرة (ثانية)
SESSION_TTL_SEC = _int("SESSION_TTL_SEC", 2 * 3600)
# إن زاد عدد التحديثات قيد المعالجة عن هذا الرقم يرد الخادم 503 وتعيد تيليجرام المحاولة لاحقاً
MAX_PENDING_UPDATES = _int("MAX_PENDING_UPDATES", 3000)
# أقل عدد محاولات ليدخل السؤال في تقرير "الأسئلة الأصعب"
REPORT_MIN_ATTEMPTS = _int("REPORT_MIN_ATTEMPTS", 10)
