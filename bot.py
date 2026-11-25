"""
VONE — بوت تيليجرام لتحويل النص إلى صوت باستخدام أصوات عربية وإنجليزية مجانية من Microsoft Edge TTS.

واجهة المستخدم ثنائية اللغة (عربي/إنجليزي) — لوحة الأدمن (إحصائيات/إذاعة) عربي فقط.
قاعدة البيانات: Turso (libSQL) عند توفر TURSO_DATABASE_URL و TURSO_AUTH_TOKEN، وإلا SQLite محلي.

نسخة الأداء العالي (للآلاف من المستخدمين):
- قاعدة البيانات ما تقفل الحلقة الرئيسية أبداً (خيوط مخصصة: قراءة / كتابة).
- بيانات المستخدم (لغة/صوت/سرعة/مفضلة) تُقرأ من الذاكرة، والتعديلات تُكتب بالخلفية.
- الكتابات المتكررة (آخر نشاط، عدّادات الطلبات، حالة الاشتراك) تتجمع وتُكتب دفعة وحدة.
- حالة الاشتراك محفوظة بالذاكرة (10 دقايق) بدل سؤال تيليجرام مع كل ضغطة.
- كاش للأصوات المتكررة (file_id) + طابور توليد بحد أقصى + مهلة زمنية.
- الإذاعة مهمة خلفية: تدعم الصور/الملفات/المنشورات المحوّلة (تُحوَّل كما هي) مع زر إلغاء وإيقاف.
"""

import os
import io
import re
import json
import html
import gzip
import time
import asyncio
import hashlib
import logging
import sqlite3
import threading
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor

import edge_tts
from telegram import (
    Update,
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatMemberStatus, ChatAction, ParseMode
from telegram.error import Forbidden, BadRequest, RetryAfter, TimedOut, NetworkError, Conflict
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)

from server import run_server

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# httpx يطبع سطر لكل طلب لتيليجرام — مع آلاف المستخدمين هذا لوغات ضخمة تبطّئ السيرفر
for _noisy in ("httpx", "httpcore", "hpack"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
logger = logging.getLogger("vone_bot")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


class config:
    BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
    ADMIN_ID = int(os.environ.get("ADMIN_ID", "9253474579"))
    CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "@@Zenox_Tols").strip()
    CHANNEL_LINK = os.environ.get("CHANNEL_LINK", "https://t.me/ZenoX_Tools").strip()
    PORT = int(os.environ.get("PORT", "10000"))
    DB_PATH = os.environ.get("DB_PATH", "bot_database.db")

    TURSO_URL = os.environ.get("TURSO_DATABASE_URL", "").strip()
    TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "").strip()

    # ---- التوليد الصوتي ----
    MAX_CONCURRENT_TTS = _env_int("MAX_CONCURRENT_TTS", 8)   # توليدات متزامنة
    MAX_TTS_QUEUE = _env_int("MAX_TTS_QUEUE", 80)            # أقصى عدد ينتظر دوره
    TTS_TIMEOUT = _env_int("TTS_TIMEOUT", 45)                # مهلة التوليد (ثواني)
    PER_USER_COOLDOWN_SECONDS = 3
    MAX_TEXT_LENGTH = 2000
    TTS_CACHE_MAX_TEXT = 400      # النصوص الأقصر من هالطول فقط تُخزَّن بالكاش
    TTS_CACHE_SIZE = 20000

    # ---- الإذاعة ----
    BROADCAST_DELAY_SECONDS = 0.05

    VOICES_PER_PAGE = 10
    ACTIVE_NOW_WINDOW_MINUTES = 5

    # ---- الأداء ----
    TOUCH_THROTTLE_SECONDS = 300      # تحديث "آخر نشاط" بقاعدة البيانات بحد أقصى مرة كل 5 دقايق
    FLUSH_INTERVAL_SECONDS = 5        # كل كم ثانية نكتب الدفعة المتجمعة
    DB_READ_WORKERS = _env_int("DB_READ_WORKERS", 4)
    DB_READ_TIMEOUT = 8               # أقصى انتظار لقراءة من قاعدة البيانات
    MAX_USERS_CACHE = 30000
    SUB_TTL_OK = 600                  # صلاحية "مشترك" بالذاكرة (ثواني)
    SUB_TTL_NO = 15                   # صلاحية "غير مشترك"
    STATS_CACHE_SECONDS = 45


SPEED_RATE = {"slow": "-20%", "normal": "+0%", "fast": "+20%"}


# ====================================================================
# الترجمة
# ====================================================================

STRINGS = {
    "ar": {
        "welcome": (
            "مرحبا بك مجدداً يا {name} في بوت VONE\n\n"
            "طريقة أستخدام البوت: ⚙️\n\n"
            "- أضغط على قائمة الأصوات 🔊\n"
            "- اختر الصوت المُناسب لك 🌐\n"
            "- أرسل النص المُراد تحويلة 🖊"
        ),
        "force_sub": (
            "مرحباً بك يا {name} في بوت VONE\n\n"
            "🚧 يجب عليك إكمال الخطوات التالية!:\n\n"
            "- أنضم الى قناة البوت اولا 📺\n"
            "- أضغط على زر التحقق 🔍\n"
            "- أرسل أمر /start للبدء ⚙️"
        ),
        "btn_voices": "قائمة الأصوات 🔊",
        "btn_favorites": "الأصوات المُفضلة 💙",
        "btn_my_stats": "📊 إحصائياتي",
        "btn_subscribe": "أشترك في القناة 📺",
        "btn_check": "تحقق 🔍",
        "btn_back": "رجوع للقائمة الرئيسية 🖲",
        "btn_prev": "« السابق",
        "btn_next": "التالي »",
        "voices_header": "🔊 قائمة الأصوات المتوفّرة — اضغط على الصوت لتفعيله، أو على القلب لإضافته للمفضلة:",
        "favorites_header": "💙 قائمة أصواتك المفضّلة:",
        "favorites_empty": "لا توجد أصوات مفضّلة بعد 💙",
        "not_subscribed_alert": "لم تشترك في القناة بعد ❌",
        "verified_alert": "تم التحقق بنجاح ✅",
        "must_subscribe_alert": "يجب الاشتراك في القناة أولاً ❌",
        "voice_unavailable_alert": "هذا الصوت لم يعد متوفراً",
        "voice_selected_alert": "تم اختيار الصوت: {name} {emoji} ✅",
        "added_favorite_alert": "أُضيف للمفضلة 💙",
        "removed_favorite_alert": "أُزيل من المفضلة",
        "voice_selected_header": (
            "✅ الصوت الحالي: {emoji} {name}\n"
            "أرسل الآن النص الذي تريد تحويله إلى صوت ✍️\n\n"
            "أو اختر صوتاً آخر من القائمة:"
        ),
        "no_voice_selected": "الرجاء اختيار صوت أولاً من قائمة الأصوات 🔊",
        "cooldown": "⏳ الرجاء الانتظار قليلاً قبل إرسال طلب جديد.",
        "text_too_long": "⚠️ النص طويل جداً، الحد الأقصى المسموح به {max} حرف.",
        "tts_error": "❌ حدث خطأ أثناء تحويل النص إلى صوت، حاول مرة أخرى.",
        "lang_changed": "✅ تم تغيير اللغة إلى العربي",
        "speed_slow": "🐢 بطيء",
        "speed_normal": "⚖️ عادي",
        "speed_fast": "🐇 سريع",
        "speed_changed": "تم ضبط السرعة: {speed}",
        "stats_title": "📊 إحصائياتك الشخصية",
        "stats_joined": "📅 عضو منذ: {date}",
        "stats_voice": "🔊 الصوت الحالي: {voice}",
        "stats_no_voice": "لم تختر صوتاً بعد",
        "stats_success": "✅ عدد التحويلات الناجحة: {count}",
        "queued": "⏳ الطلبات كثيرة حالياً — ترتيبك رقم {pos}، لحظات وتوصلك النتيجة.",
        "busy": "⚠️ الضغط عالي حالياً، حاول بعد لحظات.",
        "maintenance": "🛠 البوت تحت الصيانة حالياً، نعود قريباً 🙏",
        "language_prompt": "🌐 اختر اللغة / Choose your language:",
    },
    "en": {
        "welcome": (
            "Welcome back, {name}, to VONE Bot\n\n"
            "How to use the bot: ⚙️\n\n"
            "- Tap the voice list 🔊\n"
            "- Choose the voice you like 🌐\n"
            "- Send the text you want to convert 🖊"
        ),
        "force_sub": (
            "Welcome, {name}, to VONE Bot\n\n"
            "🚧 Please complete the steps below:\n\n"
            "- Join the bot's channel first 📺\n"
            "- Tap the verify button 🔍\n"
            "- Send /start to begin ⚙️"
        ),
        "btn_voices": "Voice List 🔊",
        "btn_favorites": "Favorite Voices 💙",
        "btn_my_stats": "📊 My Stats",
        "btn_subscribe": "Join Channel 📺",
        "btn_check": "Verify 🔍",
        "btn_back": "Back to Main Menu 🖲",
        "btn_prev": "« Previous",
        "btn_next": "Next »",
        "voices_header": "🔊 Available voices — tap a voice to activate it, or the heart to add it to favorites:",
        "favorites_header": "💙 Your favorite voices:",
        "favorites_empty": "No favorite voices yet 💙",
        "not_subscribed_alert": "You haven't joined the channel yet ❌",
        "verified_alert": "Verified successfully ✅",
        "must_subscribe_alert": "You must join the channel first ❌",
        "voice_unavailable_alert": "This voice is no longer available",
        "voice_selected_alert": "Voice selected: {name} {emoji} ✅",
        "added_favorite_alert": "Added to favorites 💙",
        "removed_favorite_alert": "Removed from favorites",
        "voice_selected_header": (
            "✅ Current voice: {emoji} {name}\n"
            "Now send the text you want to convert to speech ✍️\n\n"
            "Or pick another voice from the list:"
        ),
        "no_voice_selected": "Please choose a voice first from the voice list 🔊",
        "cooldown": "⏳ Please wait a moment before sending a new request.",
        "text_too_long": "⚠️ Text is too long, the maximum allowed is {max} characters.",
        "tts_error": "❌ An error occurred while converting text to speech, please try again.",
        "lang_changed": "✅ Language changed to English",
        "speed_slow": "🐢 Slow",
        "speed_normal": "⚖️ Normal",
        "speed_fast": "🐇 Fast",
        "speed_changed": "Speed set to: {speed}",
        "stats_title": "📊 Your Personal Stats",
        "stats_joined": "📅 Member since: {date}",
        "stats_voice": "🔊 Current voice: {voice}",
        "stats_no_voice": "No voice selected yet",
        "stats_success": "✅ Successful conversions: {count}",
        "queued": "⏳ High demand right now — you're #{pos} in line, your audio is coming shortly.",
        "busy": "⚠️ The bot is very busy right now, please try again in a moment.",
        "maintenance": "🛠 The bot is under maintenance, we'll be back soon 🙏",
        "language_prompt": "🌐 اختر اللغة / Choose your language:",
    },
}


def t(key: str, lang: str, **kwargs) -> str:
    lang = lang if lang in STRINGS else "ar"
    template = STRINGS[lang].get(key, STRINGS["ar"].get(key, key))
    return template.format(**kwargs) if kwargs else template


# ====================================================================
# الأصوات
# ====================================================================

VOICES_AR = [
    {"id": "ar-SA-HamedNeural",   "name": "حامد (السعودية)",   "emoji": "🧔🏻"},
    {"id": "ar-SA-ZariyahNeural", "name": "زارية (السعودية)",  "emoji": "👩🏻‍🦳"},
    {"id": "ar-EG-ShakirNeural",  "name": "شاكر (مصر)",        "emoji": "🧔🏻"},
    {"id": "ar-EG-SalmaNeural",   "name": "سلمى (مصر)",        "emoji": "👩🏻‍🦳"},
    {"id": "ar-AE-HamdanNeural",  "name": "حمدان (الإمارات)",   "emoji": "🧔🏻"},
    {"id": "ar-AE-FatimaNeural",  "name": "فاطمة (الإمارات)",   "emoji": "👩🏻‍🦳"},
    {"id": "ar-BH-AliNeural",     "name": "علي (البحرين)",      "emoji": "🧔🏻"},
    {"id": "ar-BH-LailaNeural",   "name": "ليلى (البحرين)",     "emoji": "👩🏻‍🦳"},
    {"id": "ar-DZ-IsmaelNeural",  "name": "إسماعيل (الجزائر)",  "emoji": "🧔🏻"},
    {"id": "ar-DZ-AminaNeural",   "name": "أمينة (الجزائر)",    "emoji": "👩🏻‍🦳"},
    {"id": "ar-IQ-BasselNeural",  "name": "باسل (العراق)",      "emoji": "🧔🏻"},
    {"id": "ar-IQ-RanaNeural",    "name": "رنا (العراق)",       "emoji": "👩🏻‍🦳"},
    {"id": "ar-JO-TaimNeural",    "name": "طيم (الأردن)",       "emoji": "🧔🏻"},
    {"id": "ar-JO-SanaNeural",    "name": "سناء (الأردن)",      "emoji": "👩🏻‍🦳"},
    {"id": "ar-KW-FahedNeural",   "name": "فهد (الكويت)",       "emoji": "🧔🏻"},
    {"id": "ar-KW-NouraNeural",   "name": "نورة (الكويت)",      "emoji": "👩🏻‍🦳"},
    {"id": "ar-LB-RamiNeural",    "name": "رامي (لبنان)",       "emoji": "🧔🏻"},
    {"id": "ar-LB-LaylaNeural",   "name": "ليلى (لبنان)",       "emoji": "👩🏻‍🦳"},
    {"id": "ar-LY-OmarNeural",    "name": "عمر (ليبيا)",        "emoji": "🧔🏻"},
    {"id": "ar-LY-ImanNeural",    "name": "إيمان (ليبيا)",      "emoji": "👩🏻‍🦳"},
    {"id": "ar-MA-JamalNeural",   "name": "جمال (المغرب)",      "emoji": "🧔🏻"},
    {"id": "ar-MA-MounaNeural",   "name": "منى (المغرب)",       "emoji": "👩🏻‍🦳"},
    {"id": "ar-OM-AbdullahNeural","name": "عبدالله (عُمان)",    "emoji": "🧔🏻"},
    {"id": "ar-OM-AyshaNeural",   "name": "عائشة (عُمان)",      "emoji": "👩🏻‍🦳"},
    {"id": "ar-QA-MoazNeural",    "name": "معاذ (قطر)",         "emoji": "🧔🏻"},
    {"id": "ar-QA-AmalNeural",    "name": "أمل (قطر)",          "emoji": "👩🏻‍🦳"},
    {"id": "ar-SY-LaithNeural",   "name": "ليث (سوريا)",        "emoji": "🧔🏻"},
    {"id": "ar-SY-AmanyNeural",   "name": "أماني (سوريا)",      "emoji": "👩🏻‍🦳"},
    {"id": "ar-TN-HediNeural",    "name": "هادي (تونس)",        "emoji": "🧔🏻"},
    {"id": "ar-TN-ReemNeural",    "name": "ريم (تونس)",         "emoji": "👩🏻‍🦳"},
    {"id": "ar-YE-SalehNeural",   "name": "صالح (اليمن)",       "emoji": "🧔🏻"},
    {"id": "ar-YE-MaryamNeural",  "name": "مريم (اليمن)",       "emoji": "👩🏻‍🦳"},
]

VOICES_EN = [
    {"id": "en-US-GuyNeural",       "name": "Guy (United States)",    "emoji": "🧔🏻"},
    {"id": "en-US-AriaNeural",      "name": "Aria (United States)",   "emoji": "👩🏻‍🦳"},
    {"id": "en-GB-RyanNeural",      "name": "Ryan (United Kingdom)",  "emoji": "🧔🏻"},
    {"id": "en-GB-SoniaNeural",     "name": "Sonia (United Kingdom)", "emoji": "👩🏻‍🦳"},
    {"id": "en-AU-WilliamNeural",   "name": "William (Australia)",    "emoji": "🧔🏻"},
    {"id": "en-AU-NatashaNeural",   "name": "Natasha (Australia)",    "emoji": "👩🏻‍🦳"},
    {"id": "en-CA-LiamNeural",      "name": "Liam (Canada)",          "emoji": "🧔🏻"},
    {"id": "en-CA-ClaraNeural",     "name": "Clara (Canada)",         "emoji": "👩🏻‍🦳"},
    {"id": "en-IE-ConnorNeural",    "name": "Connor (Ireland)",       "emoji": "🧔🏻"},
    {"id": "en-IE-EmilyNeural",     "name": "Emily (Ireland)",        "emoji": "👩🏻‍🦳"},
    {"id": "en-IN-PrabhatNeural",   "name": "Prabhat (India)",        "emoji": "🧔🏻"},
    {"id": "en-IN-NeerjaNeural",    "name": "Neerja (India)",         "emoji": "👩🏻‍🦳"},
    {"id": "en-ZA-LukeNeural",      "name": "Luke (South Africa)",    "emoji": "🧔🏻"},
    {"id": "en-ZA-LeahNeural",      "name": "Leah (South Africa)",    "emoji": "👩🏻‍🦳"},
    {"id": "en-NZ-MitchellNeural",  "name": "Mitchell (New Zealand)", "emoji": "🧔🏻"},
    {"id": "en-NZ-MollyNeural",     "name": "Molly (New Zealand)",    "emoji": "👩🏻‍🦳"},
]

VOICES_BY_ID = {v["id"]: v for v in (VOICES_AR + VOICES_EN)}


def get_voice(voice_id: str):
    return VOICES_BY_ID.get(voice_id)


def get_voice_pool(lang: str):
    return VOICES_EN if lang == "en" else VOICES_AR



# ====================================================================
# أدوات عامة + مقاييس
# ====================================================================

_started_at = time.time()
_metrics = {
    "tts_ok": 0, "tts_fail": 0, "tts_hit": 0, "tts_busy": 0,
    "new_users": 0, "db_errors": 0, "gen_ms": 0.0, "last_tts_error": "",
}
_recent_tts = deque(maxlen=500)   # (وقت، نجح؟) — لمراقبة صحة التوليد وتنبيه الأدمن
_bg_tasks = set()


def spawn(coro):
    """يشغّل مهمة خلفية ويحتفظ بمرجعها (عشان ما تنحذف من الذاكرة) ويسجّل أي خطأ فيها."""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)

    def _done(t):
        _bg_tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.warning("خطأ بمهمة خلفية: %r", t.exception())

    task.add_done_callback(_done)
    return task


def q(v):
    """يحوّل قيمة بايثون لنص SQL آمن (للدفعات اللي نرسلها بطلب واحد)."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            return "NULL"
        return repr(v)
    s = str(v).replace("\x00", "").replace("'", "''")
    return "'" + s + "'"


# ====================================================================
# قاعدة البيانات — خيوط مخصصة، ما تقفل الحلقة الرئيسية أبداً
# ====================================================================
#
# - خيوط للقراءة (عدة) وخيط واحد للكتابة (يحافظ على ترتيب العمليات).
# - كل خيط له اتصاله الخاص (آمن مع مكتبة libsql اللي ما تحب تنتقل بين الخيوط).
# - لو انقطع الاتصال نعيد الاتصال ونحاول مرة ثانية تلقائياً.

_tls = threading.local()
_read_pool = ThreadPoolExecutor(max_workers=config.DB_READ_WORKERS, thread_name_prefix="db-read")
_write_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db-write")
_connect_logged = False
_batch_mode = "auto"   # auto | off — لو executescript ما اشتغل نرجع للتنفيذ المنفرد


def _db_connect():
    global _connect_logged
    if config.TURSO_URL and config.TURSO_AUTH_TOKEN:
        try:
            import libsql
            conn = libsql.connect(database=config.TURSO_URL, auth_token=config.TURSO_AUTH_TOKEN)
            if not _connect_logged:
                _connect_logged = True
                logger.info("تم الاتصال المباشر بقاعدة بيانات Turso بنجاح — كل عملية تُحفظ بشكل دائم ✅")
            return conn
        except Exception as e:
            logger.error("فشل الاتصال بـ Turso، سيعمل البوت بملف محلي مؤقت بدون حفظ دائم: %s", e)

    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        pass
    return conn


def _db_call(fn, *args):
    """ينفّذ fn(conn, *args) داخل خيط قاعدة البيانات مع إعادة اتصال ومحاولة ثانية عند الفشل."""
    last_exc = None
    for attempt in (1, 2):
        conn = getattr(_tls, "conn", None)
        try:
            if conn is None:
                conn = _db_connect()
                _tls.conn = conn
            result = fn(conn, *args)
            conn.commit()
            return result
        except Exception as e:
            last_exc = e
            logger.warning("خطأ قاعدة بيانات (محاولة %s): %s", attempt, e)
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
            _tls.conn = None
    raise last_exc


def run_read(fn, *args):
    return asyncio.get_running_loop().run_in_executor(_read_pool, _db_call, fn, *args)


def run_write(fn, *args):
    return asyncio.get_running_loop().run_in_executor(_write_pool, _db_call, fn, *args)


def _bg_done(fut):
    if fut.cancelled():
        return
    exc = fut.exception()
    if exc is not None:
        _metrics["db_errors"] += 1
        logger.warning("فشلت كتابة بالخلفية: %r", exc)


def bg_write(fn, *args):
    """كتابة بالخلفية — ما ننتظرها ولا تبطّئ المستخدم."""
    fut = run_write(fn, *args)
    fut.add_done_callback(_bg_done)
    return fut


def _exec_many(conn, stmts):
    """ينفّذ مجموعة أوامر SQL بطلب واحد لكل دفعة (أسرع بكثير مع قاعدة بعيدة)."""
    global _batch_mode
    if not stmts:
        return
    for i in range(0, len(stmts), 200):
        chunk = stmts[i:i + 200]
        if _batch_mode != "off":
            try:
                conn.executescript(";\n".join(chunk) + ";")
                continue
            except Exception as e:
                logger.warning("الدفعة الجماعية ما اشتغلت، نرجع للتنفيذ المنفرد: %s", e)
                _batch_mode = "off"
        for s in chunk:
            conn.execute(s)


def _db_init_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            language_code TEXT,
            joined_at REAL,
            last_active REAL,
            selected_voice TEXT,
            is_subscribed INTEGER DEFAULT 0,
            lang TEXT DEFAULT 'ar',
            speed TEXT DEFAULT 'normal',
            success_count INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            user_id INTEGER,
            voice_id TEXT,
            PRIMARY KEY (user_id, voice_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            created_at REAL,
            success INTEGER
        )
    """)
    conn.execute("CREATE TABLE IF NOT EXISTS counters (key TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0)")
    conn.execute("CREATE TABLE IF NOT EXISTS tts_cache (key TEXT PRIMARY KEY, file_id TEXT NOT NULL, created REAL)")
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS banned (user_id INTEGER PRIMARY KEY, reason TEXT, ts REAL)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS broadcast_jobs (
            id INTEGER PRIMARY KEY,
            admin_id INTEGER, src_chat INTEGER, src_msg INTEGER, is_forward INTEGER,
            total INTEGER, cursor INTEGER, sent INTEGER, blocked INTEGER, failed INTEGER,
            status TEXT, rate REAL, started REAL, updated REAL
        )
    """)

    # ترقية آمنة لقاعدة موجودة (فيها مستخدمين حقيقيين) — نضيف الأعمدة الناقصة بس
    cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "lang" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN lang TEXT DEFAULT 'ar'")
    if "speed" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN speed TEXT DEFAULT 'normal'")
    if "success_count" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN success_count INTEGER")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_user_success ON requests(user_id, success)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_last_active ON users(last_active)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tts_created ON tts_cache(created)")

    # عدّادات الطلبات: نحسبها مرة وحدة من الجدول القديم، وبعدها نزيدها فقط
    have = {r[0] for r in conn.execute("SELECT key FROM counters").fetchall()}
    if "total_requests" not in have or "success_requests" not in have:
        row = conn.execute("SELECT COUNT(*), COALESCE(SUM(success), 0) FROM requests").fetchone()
        conn.execute("INSERT OR REPLACE INTO counters(key, value) VALUES ('total_requests', ?)", (int(row[0]),))
        conn.execute("INSERT OR REPLACE INTO counters(key, value) VALUES ('success_requests', ?)", (int(row[1]),))


def _db_load_user(conn, uid):
    row = conn.execute(
        "SELECT lang, speed, selected_voice, joined_at, username, language_code, success_count, "
        "(SELECT group_concat(voice_id, ',') FROM favorites WHERE user_id = ?) "
        "FROM users WHERE user_id = ?",
        (uid, uid),
    ).fetchone()
    if row is None:
        return None
    legacy = None
    if row[6] is None:   # مستخدم قديم: نحسب عدد تحويلاته الناجحة من الجدول القديم مرة وحدة
        legacy = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE user_id = ? AND success = 1", (uid,)
        ).fetchone()[0]
    return tuple(row) + (legacy,)


def _db_insert_user(conn, uid, username, tg_lang, now):
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id, username, language_code, joined_at, last_active, "
        "selected_voice, is_subscribed, lang, speed, success_count) "
        "VALUES (?, ?, ?, ?, ?, NULL, 0, 'ar', 'normal', 0)",
        (uid, username, tg_lang, now, now),
    )


def _db_exec(conn, sql, params=()):
    conn.execute(sql, params)


def _db_flush(conn, snap):
    stmts = []
    for uid, ts in snap["touch"].items():
        stmts.append(f"UPDATE users SET last_active={q(ts)} WHERE user_id={int(uid)}")
    for uid, (un, tl) in snap["profile"].items():
        stmts.append(f"UPDATE users SET username={q(un)}, language_code={q(tl)} WHERE user_id={int(uid)}")
    for uid, v in snap["sub"].items():
        stmts.append(f"UPDATE users SET is_subscribed={1 if v else 0} WHERE user_id={int(uid)}")
    for uid, n in snap["succ"].items():
        stmts.append(
            f"UPDATE users SET success_count=COALESCE(success_count,0)+{int(n)} WHERE user_id={int(uid)}"
        )
    if snap["total"] or snap["ok"]:
        stmts.append("INSERT OR IGNORE INTO counters(key,value) VALUES ('total_requests',0)")
        stmts.append("INSERT OR IGNORE INTO counters(key,value) VALUES ('success_requests',0)")
        stmts.append(f"UPDATE counters SET value=value+{int(snap['total'])} WHERE key='total_requests'")
        stmts.append(f"UPDATE counters SET value=value+{int(snap['ok'])} WHERE key='success_requests'")
    now = time.time()
    for key, fid in snap["tts"].items():
        stmts.append(f"INSERT OR REPLACE INTO tts_cache(key,file_id,created) VALUES ({q(key)},{q(fid)},{q(now)})")
    _exec_many(conn, stmts)


def _db_all_user_ids(conn):
    return [r[0] for r in conn.execute("SELECT user_id FROM users ORDER BY user_id").fetchall()]


def _db_next_user_ids(conn, after_id, limit):
    return [
        r[0]
        for r in conn.execute(
            "SELECT user_id FROM users WHERE user_id > ? ORDER BY user_id LIMIT ?", (after_id, limit)
        ).fetchall()
    ]


def _db_count_users(conn, after_id=0):
    return conn.execute("SELECT COUNT(*) FROM users WHERE user_id > ?", (after_id,)).fetchone()[0]


def _db_delete_users(conn, ids):
    stmts = []
    for uid in ids:
        stmts.append(f"DELETE FROM users WHERE user_id={int(uid)}")
        stmts.append(f"DELETE FROM favorites WHERE user_id={int(uid)}")
    _exec_many(conn, stmts)


def _db_get_stats(conn, now):
    day = 86400
    r = conn.execute(
        "SELECT COUNT(*), "
        "COALESCE(SUM(CASE WHEN last_active >= ? THEN 1 ELSE 0 END), 0), "
        "COALESCE(SUM(CASE WHEN last_active >= ? THEN 1 ELSE 0 END), 0), "
        "COALESCE(SUM(CASE WHEN is_subscribed = 1 THEN 1 ELSE 0 END), 0) FROM users",
        (now - 7 * day, now - 30 * day),
    ).fetchone()
    counters = {k: v for k, v in conn.execute("SELECT key, value FROM counters").fetchall()}
    lang_rows = conn.execute(
        "SELECT language_code, COUNT(*) c FROM users WHERE language_code IS NOT NULL "
        "GROUP BY language_code ORDER BY c DESC LIMIT 5"
    ).fetchall()
    total_requests = int(counters.get("total_requests", 0))
    success_requests = int(counters.get("success_requests", 0))
    return {
        "total_users": int(r[0]),
        "active_7d": int(r[1]),
        "active_30d": int(r[2]),
        "subscribed_count": int(r[3]),
        "total_requests": total_requests,
        "success_rate": (success_requests / total_requests * 100) if total_requests else 100.0,
        "top_languages": [((row[0] or "غير معروف"), row[1]) for row in lang_rows],
    }


def _db_load_tts_cache(conn, limit):
    return [
        (r[0], r[1])
        for r in conn.execute(
            "SELECT key, file_id FROM tts_cache ORDER BY created DESC LIMIT ?", (limit,)
        ).fetchall()
    ]


def _db_load_settings(conn):
    return {r[0]: r[1] for r in conn.execute("SELECT key, value FROM settings").fetchall()}


def _db_load_banned(conn):
    return [r[0] for r in conn.execute("SELECT user_id FROM banned").fetchall()]


def _db_user_info(conn, uid):
    return conn.execute(
        "SELECT user_id, username, language_code, joined_at, last_active, selected_voice, "
        "is_subscribed, lang, speed, success_count FROM users WHERE user_id = ?",
        (uid,),
    ).fetchone()


# ---- مهام الإذاعة (للاستئناف بعد إعادة التشغيل) ----

def _db_job_save(conn, j):
    conn.execute(
        "INSERT OR REPLACE INTO broadcast_jobs (id, admin_id, src_chat, src_msg, is_forward, total, cursor, "
        "sent, blocked, failed, status, rate, started, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (j["id"], j["admin_id"], j["src_chat"], j["src_msg"], 1 if j["is_forward"] else 0, j["total"],
         j["cursor"], j["sent"], j["blocked"], j["failed"], j["status"], j["rate"], j["started"], time.time()),
    )


def _db_job_latest_running(conn):
    row = conn.execute(
        "SELECT id, admin_id, src_chat, src_msg, is_forward, total, cursor, sent, blocked, failed, status, rate, started "
        "FROM broadcast_jobs WHERE status IN ('running','paused') ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return tuple(row) if row else None


def _db_job_get(conn, job_id):
    row = conn.execute(
        "SELECT id, admin_id, src_chat, src_msg, is_forward, total, cursor, sent, blocked, failed, status, rate, started "
        "FROM broadcast_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    return tuple(row) if row else None


# ====================================================================
# النسخ الاحتياطي والاستعادة
# ====================================================================

BACKUP_TABLES = {
    "users": ["user_id", "username", "language_code", "joined_at", "last_active", "selected_voice",
              "is_subscribed", "lang", "speed", "success_count"],
    "favorites": ["user_id", "voice_id"],
    "counters": ["key", "value"],
    "settings": ["key", "value"],
    "banned": ["user_id", "reason", "ts"],
}
BACKUP_KEYWORDS = ("استعادة", "استعاده", "restore")


def _db_export(conn):
    out = {}
    for table, cols in BACKUP_TABLES.items():
        rows = conn.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
        out[table] = {"columns": cols, "rows": [list(r) for r in rows]}
    return out


def _encode_backup(tables: dict) -> bytes:
    payload = {"app": "vone", "version": 1, "created": time.time(), "tables": tables}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return gzip.compress(raw, compresslevel=6)


def parse_backup(data: bytes) -> dict:
    """يفك الملف ويتحقق منه — يرفع ValueError لو مو نسخة VONE صالحة."""
    try:
        raw = gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        raise ValueError("الملف مو نسخة احتياطية صالحة (تعذّر قراءته).")
    if not isinstance(payload, dict) or payload.get("app") != "vone" or "tables" not in payload:
        raise ValueError("هذا الملف مو نسخة احتياطية لبوت VONE.")
    tables = payload["tables"]
    for name, cols in BACKUP_TABLES.items():
        t_ = tables.get(name)
        if not isinstance(t_, dict) or not isinstance(t_.get("rows"), list) or not isinstance(t_.get("columns"), list):
            raise ValueError(f"النسخة ناقصة أو تالفة (الجدول {name}).")
    return payload


def _rows_as_dicts(table_obj, wanted_cols):
    cols = table_obj["columns"]
    idx = {c: i for i, c in enumerate(cols)}
    for row in table_obj["rows"]:
        yield {c: (row[idx[c]] if c in idx and idx[c] < len(row) else None) for c in wanted_cols}


def _db_import(conn, payload):
    """استعادة دمج: ما نمسح شي موجود، ونحدّث المستخدم فقط لو نسخته بالملف أحدث نشاطاً."""
    tables = payload["tables"]
    stmts = []
    ucols = BACKUP_TABLES["users"]
    for r in _rows_as_dicts(tables["users"], ucols):
        if r["user_id"] is None:
            continue
        vals = ", ".join(q(r[c]) for c in ucols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in ucols if c != "user_id")
        stmts.append(
            f"INSERT INTO users ({', '.join(ucols)}) VALUES ({vals}) "
            f"ON CONFLICT(user_id) DO UPDATE SET {updates} "
            f"WHERE COALESCE(excluded.last_active,0) >= COALESCE(users.last_active,0)"
        )
    for r in _rows_as_dicts(tables["favorites"], BACKUP_TABLES["favorites"]):
        if r["user_id"] is not None and r["voice_id"]:
            stmts.append(f"INSERT OR IGNORE INTO favorites(user_id, voice_id) VALUES ({q(r['user_id'])}, {q(r['voice_id'])})")
    for r in _rows_as_dicts(tables["counters"], BACKUP_TABLES["counters"]):
        if r["key"]:
            stmts.append(
                f"INSERT INTO counters(key,value) VALUES ({q(r['key'])}, {q(r['value'] or 0)}) "
                f"ON CONFLICT(key) DO UPDATE SET value=MAX(value, excluded.value)"
            )
    for r in _rows_as_dicts(tables["settings"], BACKUP_TABLES["settings"]):
        if r["key"] and r["key"] != "last_backup":
            stmts.append(f"INSERT OR REPLACE INTO settings(key,value) VALUES ({q(r['key'])}, {q(r['value'])})")
    for r in _rows_as_dicts(tables["banned"], BACKUP_TABLES["banned"]):
        if r["user_id"] is not None:
            stmts.append(f"INSERT OR REPLACE INTO banned(user_id,reason,ts) VALUES ({q(r['user_id'])}, {q(r['reason'])}, {q(r['ts'])})")
    _exec_many(conn, stmts)
    return len(stmts)


# ====================================================================
# إعدادات حيّة (يغيّرها الأدمن من اللوحة بدون إعادة نشر) + قائمة الحظر
# ====================================================================

SETTING_DEFAULTS = {
    "maintenance": "0",
    "force_sub": "1",
    "alerts": "1",
    "cooldown": str(3),
    "max_text": str(2000),
    "max_tts": str(config.MAX_CONCURRENT_TTS),
    "disabled_voices": "",
    "last_backup": "0",
}
_settings = dict(SETTING_DEFAULTS)
_banned = set()


def S(key: str) -> str:
    return _settings.get(key, SETTING_DEFAULTS.get(key, ""))


def S_int(key: str) -> int:
    try:
        return int(float(S(key)))
    except ValueError:
        return int(float(SETTING_DEFAULTS.get(key, "0") or 0))


def set_setting(key: str, value):
    _settings[key] = str(value)
    bg_write(_db_exec, "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, str(value)))


def disabled_voices() -> set:
    return {v for v in S("disabled_voices").split(",") if v}


# ====================================================================
# بيانات المستخدمين بالذاكرة (لغة/صوت/سرعة/مفضلة) + الكتابة المجمّعة
# ====================================================================

class UserState:
    __slots__ = ("uid", "username", "tg_lang", "lang", "speed", "voice", "favs", "joined_at",
                 "success_count", "last_seen", "last_touch", "transient_until")

    def __init__(self, uid, username, tg_lang, now):
        self.uid = uid
        self.username = username
        self.tg_lang = tg_lang
        self.lang = "ar"
        self.speed = "normal"
        self.voice = None
        self.favs = set()
        self.joined_at = now
        self.success_count = 0
        self.last_seen = now
        self.last_touch = now
        self.transient_until = 0.0


_users = OrderedDict()        # user_id -> UserState (LRU)
_user_loading = {}            # user_id -> Future (يمنع تحميل نفس المستخدم مرتين بنفس اللحظة)

_pending_touch = {}           # user_id -> آخر نشاط
_pending_profile = {}         # user_id -> (username, language_code)
_pending_sub = {}             # user_id -> bool
_pending_succ = {}            # user_id -> عدد تحويلات ناجحة جديدة
_pending_counters = {"total": 0, "ok": 0}
_pending_tts = {}             # key -> file_id


def _swap_pending():
    global _pending_touch, _pending_profile, _pending_sub, _pending_succ, _pending_counters, _pending_tts
    snap = {
        "touch": _pending_touch, "profile": _pending_profile, "sub": _pending_sub,
        "succ": _pending_succ, "total": _pending_counters["total"], "ok": _pending_counters["ok"],
        "tts": _pending_tts,
    }
    _pending_touch, _pending_profile, _pending_sub = {}, {}, {}
    _pending_succ, _pending_tts = {}, {}
    _pending_counters = {"total": 0, "ok": 0}
    return snap


def _snap_empty(snap) -> bool:
    return not (snap["touch"] or snap["profile"] or snap["sub"] or snap["succ"]
                or snap["total"] or snap["ok"] or snap["tts"])


def _requeue(snap):
    """لو فشلت الكتابة نرجّع الدفعة عشان ما نخسر بيانات."""
    for k, v in snap["touch"].items():
        _pending_touch.setdefault(k, v)
    for k, v in snap["profile"].items():
        _pending_profile.setdefault(k, v)
    for k, v in snap["sub"].items():
        _pending_sub.setdefault(k, v)
    for k, v in snap["succ"].items():
        _pending_succ[k] = _pending_succ.get(k, 0) + v
    for k, v in snap["tts"].items():
        _pending_tts.setdefault(k, v)
    _pending_counters["total"] += snap["total"]
    _pending_counters["ok"] += snap["ok"]


async def flush_pending():
    snap = _swap_pending()
    if _snap_empty(snap):
        return
    try:
        await run_write(_db_flush, snap)
    except Exception as e:
        _metrics["db_errors"] += 1
        logger.warning("فشل تفريغ الدفعة، راح نعيد المحاولة بالدورة الجاية: %r", e)
        _requeue(snap)


async def _load_user_state(user) -> UserState:
    uid = user.id
    now = time.time()
    uname = user.username or ""
    tl = user.language_code or ""
    try:
        row = await asyncio.wait_for(run_read(_db_load_user, uid), timeout=config.DB_READ_TIMEOUT)
    except Exception as e:
        # قاعدة البيانات بطيئة/واقفة: نخدم المستخدم بإعدادات افتراضية مؤقتة بدل ما نوقفه
        _metrics["db_errors"] += 1
        logger.warning("تعذّر تحميل المستخدم %s من قاعدة البيانات: %r", uid, e)
        st = UserState(uid, uname, tl, now)
        st.transient_until = now + 30
        return st

    st = UserState(uid, uname, tl, now)
    if row is None:
        _metrics["new_users"] += 1
        bg_write(_db_insert_user, uid, uname, tl, now)
    else:
        lang, speed, voice, joined, db_uname, db_tl, sc, favs_csv, legacy = row
        st.lang = lang if lang in STRINGS else "ar"
        st.speed = speed if speed in SPEED_RATE else "normal"
        st.voice = voice
        st.joined_at = joined or now
        st.favs = {v for v in (favs_csv or "").split(",") if v}
        if sc is None:
            st.success_count = int(legacy or 0)
            bg_write(_db_exec, "UPDATE users SET success_count=? WHERE user_id=? AND success_count IS NULL",
                     (st.success_count, uid))
        else:
            st.success_count = int(sc)
        if (db_uname or "") != uname or (db_tl or "") != tl:
            _pending_profile[uid] = (uname, tl)
        _pending_touch[uid] = now
    return st


async def get_user(user) -> UserState:
    """يرجّع حالة المستخدم من الذاكرة (أو يحمّلها مرة وحدة من قاعدة البيانات)."""
    uid = user.id
    now = time.time()
    st = _users.get(uid)
    if st is not None and st.transient_until and now > st.transient_until:
        _users.pop(uid, None)
        st = None
    if st is not None:
        _users.move_to_end(uid)
        st.last_seen = now
        if now - st.last_touch >= config.TOUCH_THROTTLE_SECONDS:
            st.last_touch = now
            _pending_touch[uid] = now
        uname = user.username or ""
        tl = user.language_code or ""
        if uname != st.username or tl != st.tg_lang:
            st.username, st.tg_lang = uname, tl
            _pending_profile[uid] = (uname, tl)
        return st

    fut = _user_loading.get(uid)
    if fut is None:
        async def _load():
            s = await _load_user_state(user)
            _users[uid] = s
            while len(_users) > config.MAX_USERS_CACHE:
                _users.popitem(last=False)
            return s

        fut = asyncio.ensure_future(_load())
        _user_loading[uid] = fut
        fut.add_done_callback(lambda f, u=uid: _user_loading.pop(u, None))
    return await fut


def set_user_lang(st: UserState, lang: str):
    st.lang = lang
    bg_write(_db_exec, "UPDATE users SET lang=? WHERE user_id=?", (lang, st.uid))


def set_user_speed(st: UserState, speed: str):
    st.speed = speed
    bg_write(_db_exec, "UPDATE users SET speed=? WHERE user_id=?", (speed, st.uid))


def set_user_voice(st: UserState, voice_id: str):
    st.voice = voice_id
    bg_write(_db_exec, "UPDATE users SET selected_voice=? WHERE user_id=?", (voice_id, st.uid))


def toggle_user_favorite(st: UserState, voice_id: str) -> bool:
    if voice_id in st.favs:
        st.favs.discard(voice_id)
        bg_write(_db_exec, "DELETE FROM favorites WHERE user_id=? AND voice_id=?", (st.uid, voice_id))
        return False
    st.favs.add(voice_id)
    bg_write(_db_exec, "INSERT OR IGNORE INTO favorites (user_id, voice_id) VALUES (?, ?)", (st.uid, voice_id))
    return True


def record_request(st: UserState, success: bool):
    """يسجّل نتيجة طلب تحويل بالذاكرة (يُكتب بالدفعة الجاية بدل كتابة فورية)."""
    _pending_counters["total"] += 1
    _recent_tts.append((time.time(), bool(success)))
    if success:
        _pending_counters["ok"] += 1
        _pending_succ[st.uid] = _pending_succ.get(st.uid, 0) + 1
        st.success_count += 1


# ====================================================================
# التحقق من الاشتراك بالقناة — مخزّن بالذاكرة
# ====================================================================

_sub_cache = {}   # user_id -> (مشترك؟, وقت انتهاء الصلاحية)
_sub_sem = asyncio.Semaphore(25)
_SUB_OK_STATUSES = None


def _sub_ok_statuses():
    global _SUB_OK_STATUSES
    if _SUB_OK_STATUSES is None:
        _SUB_OK_STATUSES = (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    return _SUB_OK_STATUSES


def _remember_sub(user_id: int, status: bool, ttl: float):
    prev = _sub_cache.get(user_id)
    _sub_cache[user_id] = (status, time.time() + ttl)
    if prev is None or prev[0] != status:
        _pending_sub[user_id] = status


async def is_subscribed(bot, user_id: int, force: bool = False) -> bool:
    now = time.time()
    entry = _sub_cache.get(user_id)
    if entry and not force and entry[1] > now:
        return entry[0]
    try:
        async with _sub_sem:
            member = await bot.get_chat_member(chat_id=config.CHANNEL_USERNAME, user_id=user_id)
        status = member.status in _sub_ok_statuses()
        _remember_sub(user_id, status, config.SUB_TTL_OK if status else config.SUB_TTL_NO)
        return status
    except BadRequest as e:
        logger.warning("تعذّر التحقق من الاشتراك للمستخدم %s: %s", user_id, e)
        _remember_sub(user_id, False, config.SUB_TTL_NO)
        return False
    except Exception as e:
        # مشكلة مؤقتة من تيليجرام (ضغط/شبكة): ما نحجب مستخدم مشترك بسبب عطل عابر
        logger.warning("خطأ مؤقت أثناء التحقق من الاشتراك (%s): %r", user_id, e)
        if entry:
            return entry[0]
        return True


async def passes_sub_gate(bot, user_id: int, force: bool = False) -> bool:
    if S("force_sub") != "1":
        return True
    return await is_subscribed(bot, user_id, force=force)


# ====================================================================
# كاش الأصوات المتكررة (file_id) — نفس النص+الصوت+السرعة يُرسل فوراً بدون توليد
# ====================================================================

_tts_cache = OrderedDict()   # key -> file_id


def tts_key(voice_id: str, rate: str, text: str) -> str:
    norm = " ".join(text.split())
    return hashlib.sha1(f"{voice_id}|{rate}|{norm}".encode("utf-8")).hexdigest()[:32]


def tts_cache_put(key: str, file_id: str):
    _tts_cache[key] = file_id
    _tts_cache.move_to_end(key)
    while len(_tts_cache) > config.TTS_CACHE_SIZE:
        _tts_cache.popitem(last=False)
    _pending_tts[key] = file_id


# ====================================================================
# إحصائيات الأدمن (مخزّنة 45 ثانية)
# ====================================================================

_stats_cache = None   # (وقت، dict)


async def get_stats_cached(force: bool = False) -> dict:
    global _stats_cache
    now = time.time()
    max_age = 5 if force else config.STATS_CACHE_SECONDS
    if _stats_cache is None or now - _stats_cache[0] >= max_age:
        stats = await run_read(_db_get_stats, now)
        _stats_cache = (now, stats)
    stats = dict(_stats_cache[1])
    window = config.ACTIVE_NOW_WINDOW_MINUTES * 60
    stats["active_now"] = sum(1 for s in _users.values() if now - s.last_seen <= window)
    return stats


# ====================================================================
# الأزرار (Inline Keyboards)
# ====================================================================

def get_voice_pool(lang: str):
    dis = disabled_voices()
    pool = VOICES_EN if lang == "en" else VOICES_AR
    return [v for v in pool if v["id"] not in dis] if dis else pool


def main_menu_keyboard(lang: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t("btn_voices", lang), callback_data="menu_voices", style="primary")],
        [InlineKeyboardButton(t("btn_favorites", lang), callback_data="menu_fav", style="primary")],
    ])


def language_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("English 🇺🇸", callback_data="set_lang:en", style="success"),
            InlineKeyboardButton("🇾🇪 عربي", callback_data="set_lang:ar", style="success"),
        ],
    ])


def subscribe_keyboard(lang: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t("btn_subscribe", lang), url=config.CHANNEL_LINK, style="danger")],
        [InlineKeyboardButton(t("btn_check", lang), callback_data="check_sub", style="primary")],
    ])


def back_main_keyboard(lang: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t("btn_back", lang), callback_data="back_main")],
    ])


def _paginate(items, page):
    start = page * config.VOICES_PER_PAGE
    end = start + config.VOICES_PER_PAGE
    return items[start:end], len(items)


def voices_list_keyboard(page: int, favorites, lang: str):
    pool = get_voice_pool(lang)
    page_items, total = _paginate(pool, page)
    rows = []
    for v in page_items:
        heart = "💙" if v["id"] in favorites else "🤍"
        rows.append([
            InlineKeyboardButton(f"{v['emoji']} {v['name']}", callback_data=f"vsel:list:{page}:{v['id']}"),
            InlineKeyboardButton(heart, callback_data=f"vtog:list:{page}:{v['id']}"),
        ])
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(t("btn_prev", lang), callback_data=f"vlist:{page - 1}"))
    if (page + 1) * config.VOICES_PER_PAGE < total:
        nav_row.append(InlineKeyboardButton(t("btn_next", lang), callback_data=f"vlist:{page + 1}"))
    if nav_row:
        rows.append(nav_row)
    rows.append([InlineKeyboardButton(t("btn_back", lang), callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


def favorites_list_keyboard(page: int, favorite_voice_ids, lang: str):
    pool = get_voice_pool(lang)
    fav_voices = [v for v in pool if v["id"] in favorite_voice_ids]
    page_items, total = _paginate(fav_voices, page)
    rows = []
    for v in page_items:
        rows.append([
            InlineKeyboardButton(f"{v['emoji']} {v['name']} 💙", callback_data=f"vsel:fav:{page}:{v['id']}"),
            InlineKeyboardButton("✖️", callback_data=f"vtog:fav:{page}:{v['id']}"),
        ])
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(t("btn_prev", lang), callback_data=f"vfav:{page - 1}"))
    if (page + 1) * config.VOICES_PER_PAGE < total:
        nav_row.append(InlineKeyboardButton(t("btn_next", lang), callback_data=f"vfav:{page + 1}"))
    if nav_row:
        rows.append(nav_row)
    rows.append([InlineKeyboardButton(t("btn_back", lang), callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


def with_speed_row(keyboard: InlineKeyboardMarkup, lang: str, current_speed: str, origin: str, page: int):
    """يضيف صف تحكم بالسرعة تحت قائمة الأصوات (يظهر بعد اختيار صوت فقط)."""
    rows = [list(row) for row in keyboard.inline_keyboard]
    speed_row = []
    for value in ("slow", "normal", "fast"):
        label = t(f"speed_{value}", lang)
        if value == current_speed:
            label = "✅ " + label
        speed_row.append(
            InlineKeyboardButton(label, callback_data=f"speed:{value}:{origin}:{page}", style="success")
        )
    rows.insert(len(rows) - 1, speed_row)  # قبل صف "رجوع للقائمة الرئيسية" اللي دايماً أخير صف
    return InlineKeyboardMarkup(rows)


def stats_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("تحديث 🔄", callback_data="admin_stats_refresh", style="primary")],
        [InlineKeyboardButton("🎛 لوحة التحكم", callback_data="adm:home")],
    ])


# ====================================================================
# منطق البوت
# ====================================================================

tts_semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_TTS)
_tts_queued = 0
_last_request_time = {}
_awaiting_broadcast = set()     # الأدمن اللي فعّل وضع الإذاعة وينتظر المنشور
_bc_pending = {}                # المنشور المُستلم بانتظار تأكيد البدء
_pending_restore = {}           # ملف استعادة بانتظار التأكيد
SEP = "➖➖➖➖➖➖➖➖➖➖➖➖➖➖➖"


def build_personal_stats_text(lang: str, st: UserState) -> str:
    date_str = time.strftime("%Y-%m-%d", time.gmtime(st.joined_at)) if st.joined_at else "—"
    voice = get_voice(st.voice) if st.voice else None
    voice_str = f"{voice['emoji']} {voice['name']}" if voice else t("stats_no_voice", lang)
    return "\n".join([
        t("stats_title", lang),
        SEP,
        "",
        t("stats_joined", lang, date=date_str),
        t("stats_voice", lang, voice=voice_str),
        t("stats_success", lang, count=st.success_count),
    ])


def _fmt_duration(sec: float) -> str:
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}س {m}د"
    if m:
        return f"{m}د {s}ث"
    return f"{s}ث"


def build_perf_text() -> str:
    ok, fail = _metrics["tts_ok"], _metrics["tts_fail"]
    avg = _metrics["gen_ms"] / 1000.0
    return "\n".join([
        "⚙️ <b>الأداء</b>",
        f"⏱ مدة التشغيل: {_fmt_duration(time.time() - _started_at)}",
        f"🧠 مستخدمون بالذاكرة: {len(_users):,}",
        f"🎙 تحويلات: ✅ {ok:,} / ❌ {fail:,}",
        f"⚡ من الكاش (بدون توليد): {_metrics['tts_hit']:,}",
        f"⏳ بالانتظار الآن: {_tts_queued} | متوسط التوليد: {avg:.1f}ث",
        f"🗄 أخطاء القاعدة: {_metrics['db_errors']}",
    ])


def build_stats_text(stats: dict) -> str:
    lines = [
        "📊 <b>لوحة إحصائيات بوت VONE</b>",
        SEP,
        "",
        "👥 <b>المستخدمون</b>",
        f"🟢 نشطون الآن: <b>{stats['active_now']}</b>",
        f"👤 إجمالي المستخدمين: <b>{stats['total_users']}</b>",
        f"📈 نشطون آخر 7 أيام: <b>{stats['active_7d']}</b>",
        f"📈 نشطون آخر 30 يوم: <b>{stats['active_30d']}</b>",
        f"📺 مشتركون بالقناة (آخر تحقق): <b>{stats['subscribed_count']}</b>",
        f"🚫 محظورون: <b>{len(_banned)}</b>",
        "",
        SEP,
        "",
        "🌍 <b>أبرز اللغات لدى المستخدمين</b>",
    ]
    if stats["top_languages"]:
        total = stats["total_users"] or 1
        for lang_code, count in stats["top_languages"]:
            lines.append(f"▫️ {lang_code}: <b>{count}</b> ({count / total * 100:.1f}%)")
    else:
        lines.append("▫️ لا توجد بيانات كافية بعد")
    lines += [
        "",
        SEP,
        "",
        "📨 <b>الطلبات</b>",
        f"📥 إجمالي الطلبات: <b>{stats['total_requests']:,}</b>",
        f"✅ نسبة نجاح البوت: <b>{stats['success_rate']:.1f}%</b>",
        "",
        SEP,
        "",
        build_perf_text(),
        "",
        f"🕒 آخر تحديث: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
    ]
    return "\n".join(lines)


async def send_stats(bot, chat_id: int, message_id: int = None, force: bool = False):
    stats = await get_stats_cached(force=force)
    text = build_stats_text(stats)
    if message_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text,
                reply_markup=stats_keyboard(), parse_mode=ParseMode.HTML,
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                logger.warning("فشل تحديث رسالة الإحصائيات: %s", e)
        return
    await bot.send_message(chat_id=chat_id, text=text, reply_markup=stats_keyboard(), parse_mode=ParseMode.HTML)


async def gate_blocked(update, user, lang: str) -> bool:
    """صيانة / حظر — يرجّع True لو لازم نوقف المعالجة (الأدمن مستثنى)."""
    if user.id == config.ADMIN_ID:
        return False
    query = update.callback_query
    if user.id in _banned:
        if query:
            await query.answer()
        return True
    if S("maintenance") == "1":
        text = t("maintenance", lang)
        if query:
            await query.answer(text, show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(text)
        return True
    return False


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or update.effective_message is None:
        return
    st = await get_user(user)
    lang = st.lang
    if await gate_blocked(update, user, lang):
        return
    name = user.first_name or "صديقنا"
    if not await passes_sub_gate(context.bot, user.id):
        await update.effective_message.reply_text(
            t("force_sub", lang, name=name), reply_markup=subscribe_keyboard(lang)
        )
        return
    await update.effective_message.reply_text(
        t("welcome", lang, name=name), reply_markup=main_menu_keyboard(lang)
    )


async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or update.effective_message is None:
        return
    st = await get_user(user)
    if await gate_blocked(update, user, st.lang):
        return
    await update.effective_message.reply_text(t("language_prompt", st.lang), reply_markup=language_keyboard())


async def mystats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or update.effective_message is None:
        return
    st = await get_user(user)
    lang = st.lang
    if await gate_blocked(update, user, lang):
        return
    name = user.first_name or "صديقنا"
    if not await passes_sub_gate(context.bot, user.id):
        await update.effective_message.reply_text(
            t("force_sub", lang, name=name), reply_markup=subscribe_keyboard(lang)
        )
        return
    await update.effective_message.reply_text(
        build_personal_stats_text(lang, st), reply_markup=back_main_keyboard(lang)
    )


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return
    data = query.data or ""
    name = user.first_name or "صديقنا"
    st = await get_user(user)
    lang = st.lang
    is_admin = user.id == config.ADMIN_ID

    # ---- أزرار الأدمن (لوحة التحكم + الإذاعة) ----
    if data.startswith(("adm:", "bc_")) or data == "admin_stats_refresh":
        await admin_callback(update, context, data)
        return

    if await gate_blocked(update, user, lang):
        return

    # ---- تغيير اللغة (قبل بوابة الاشتراك: تفضيل بسيط ما يضر) ----
    if data.startswith("set_lang:"):
        new_lang = data.split(":", 1)[1]
        if new_lang not in ("ar", "en"):
            await query.answer()
            return
        set_user_lang(st, new_lang)
        await query.answer(t("lang_changed", new_lang))
        try:
            if await passes_sub_gate(context.bot, user.id):
                await query.edit_message_text(
                    t("welcome", new_lang, name=name), reply_markup=main_menu_keyboard(new_lang)
                )
            else:
                await query.edit_message_text(
                    t("force_sub", new_lang, name=name), reply_markup=subscribe_keyboard(new_lang)
                )
        except BadRequest:
            pass
        return

    if data == "check_sub":
        subscribed = await passes_sub_gate(context.bot, user.id, force=True)
        if not subscribed:
            await query.answer(t("not_subscribed_alert", lang), show_alert=True)
            return
        await query.answer(t("verified_alert", lang), show_alert=True)
        try:
            await query.edit_message_text(t("welcome", lang, name=name), reply_markup=main_menu_keyboard(lang))
        except BadRequest:
            await context.bot.send_message(
                chat_id=user.id, text=t("welcome", lang, name=name), reply_markup=main_menu_keyboard(lang)
            )
        return

    if not is_admin and not await passes_sub_gate(context.bot, user.id):
        await query.answer(t("must_subscribe_alert", lang), show_alert=True)
        try:
            await query.edit_message_text(t("force_sub", lang, name=name), reply_markup=subscribe_keyboard(lang))
        except BadRequest:
            pass
        return

    # مهم: لازم قبل await query.answer() العام تحت — عشان ما يصير answer() مرتين لنفس الضغطة
    if data.startswith("speed:"):
        _, value, origin, page = data.split(":", 3)
        if value not in SPEED_RATE:
            await query.answer()
            return
        set_user_speed(st, value)
        await query.answer(t("speed_changed", lang, speed=t(f"speed_{value}", lang)))
        voice = get_voice(st.voice) if st.voice else None
        if not voice:
            return
        header = t("voice_selected_header", lang, emoji=voice["emoji"], name=voice["name"])
        page = int(page)
        kb = favorites_list_keyboard(page, st.favs, lang) if origin == "fav" else voices_list_keyboard(page, st.favs, lang)
        kb = with_speed_row(kb, lang, value, origin, page)
        try:
            await query.edit_message_text(header, reply_markup=kb)
        except BadRequest:
            pass
        return

    if not data.startswith(("vsel:", "vtog:")):   # هذي الفروع ترد بنفسها (رد واحد فقط لكل ضغطة)
        await query.answer()

    if data == "back_main":
        await query.edit_message_text(t("welcome", lang, name=name), reply_markup=main_menu_keyboard(lang))
        return

    if data == "menu_my_stats":   # توافق مع رسائل قديمة فيها الزر
        await query.edit_message_text(build_personal_stats_text(lang, st), reply_markup=back_main_keyboard(lang))
        return

    if data == "menu_voices":
        await query.edit_message_text(t("voices_header", lang), reply_markup=voices_list_keyboard(0, st.favs, lang))
        return

    if data == "menu_fav":
        if not st.favs:
            await query.edit_message_text(t("favorites_empty", lang), reply_markup=back_main_keyboard(lang))
            return
        await query.edit_message_text(t("favorites_header", lang), reply_markup=favorites_list_keyboard(0, st.favs, lang))
        return

    if data.startswith("vlist:"):
        page = int(data.split(":")[1])
        await query.edit_message_text(t("voices_header", lang), reply_markup=voices_list_keyboard(page, st.favs, lang))
        return

    if data.startswith("vfav:"):
        page = int(data.split(":")[1])
        await query.edit_message_text(t("favorites_header", lang), reply_markup=favorites_list_keyboard(page, st.favs, lang))
        return

    if data.startswith("vsel:"):
        _, origin, page, voice_id = data.split(":", 3)
        voice = get_voice(voice_id)
        if not voice or voice_id in disabled_voices():
            await query.answer(t("voice_unavailable_alert", lang), show_alert=True)
            return
        set_user_voice(st, voice_id)
        await query.answer(t("voice_selected_alert", lang, name=voice["name"], emoji=voice["emoji"]))
        header = t("voice_selected_header", lang, emoji=voice["emoji"], name=voice["name"])
        page = int(page)
        kb = favorites_list_keyboard(page, st.favs, lang) if origin == "fav" else voices_list_keyboard(page, st.favs, lang)
        kb = with_speed_row(kb, lang, st.speed, origin, page)
        try:
            await query.edit_message_text(header, reply_markup=kb)
        except BadRequest:
            pass
        return

    if data.startswith("vtog:"):
        _, origin, page, voice_id = data.split(":", 3)
        now_fav = toggle_user_favorite(st, voice_id)
        await query.answer(t("added_favorite_alert", lang) if now_fav else t("removed_favorite_alert", lang))
        page = int(page)
        try:
            if origin == "fav":
                if page > 0 and page * config.VOICES_PER_PAGE >= len(st.favs):
                    page -= 1
                if not st.favs:
                    await query.edit_message_text(t("favorites_empty", lang), reply_markup=back_main_keyboard(lang))
                    return
                await query.edit_message_reply_markup(reply_markup=favorites_list_keyboard(page, st.favs, lang))
            else:
                await query.edit_message_reply_markup(reply_markup=voices_list_keyboard(page, st.favs, lang))
        except BadRequest:
            pass
        return


# ====================================================================
# التوليد الصوتي
# ====================================================================

async def generate_speech(text: str, voice_id: str, rate: str = "+0%") -> bytes:
    """يحاول التوليد مرتين قبل الاستسلام — خدمة مايكروسوفت غير الرسمية (edge-tts)
    أحياناً ترفض طلب لحظياً تحت الضغط، وإعادة المحاولة فوراً تحل أغلب هالحالات."""
    last_error = None
    for attempt in range(2):
        try:
            communicate = edge_tts.Communicate(text, voice_id, rate=rate)
            chunks = bytearray()
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio":
                    chunks.extend(chunk["data"])
            if chunks:
                return bytes(chunks)
            raise RuntimeError("empty audio stream")
        except Exception as e:
            last_error = e
            logger.warning("محاولة توليد الصوت رقم %s فشلت: %s", attempt + 1, e)
            if attempt == 0:
                await asyncio.sleep(1)
    raise last_error


async def tts_generate_queued(text: str, voice_id: str, rate: str) -> bytes:
    """طابور بحد أقصى + مهلة زمنية: ما نخلي طلب معلّق يمسك مكان للأبد."""
    global _tts_queued
    _tts_queued += 1
    counted = True
    try:
        async with tts_semaphore:
            _tts_queued -= 1
            counted = False
            t0 = time.monotonic()
            data = await asyncio.wait_for(generate_speech(text, voice_id, rate), timeout=config.TTS_TIMEOUT)
            ms = (time.monotonic() - t0) * 1000.0
            _metrics["gen_ms"] = ms if _metrics["gen_ms"] == 0 else (_metrics["gen_ms"] * 0.9 + ms * 0.1)
            return data
    finally:
        if counted:
            _tts_queued -= 1


async def _typing(bot, chat_id):
    try:
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.RECORD_VOICE)
    except Exception:
        pass


async def process_tts(update, context, st: UserState, voice: dict, text: str):
    user = update.effective_user
    msg = update.effective_message
    lang = st.lang
    rate = SPEED_RATE.get(st.speed, "+0%")
    key = tts_key(voice["id"], rate, text) if len(text) <= config.TTS_CACHE_MAX_TEXT else None

    success = False
    notice = None
    try:
        # 1) كاش: نفس النص+الصوت+السرعة انولد قبل → نرسله فوراً بدون توليد
        if key:
            fid = _tts_cache.get(key)
            if fid:
                try:
                    await context.bot.send_audio(chat_id=user.id, audio=fid)
                    _tts_cache.move_to_end(key)
                    _metrics["tts_hit"] += 1
                    success = True
                    return
                except BadRequest:
                    _tts_cache.pop(key, None)

        # 2) طابور محدود
        if _tts_queued >= config.MAX_TTS_QUEUE:
            _metrics["tts_busy"] += 1
            await msg.reply_text(t("busy", lang))
            return
        spawn(_typing(context.bot, user.id))
        if tts_semaphore.locked():
            try:
                notice = await msg.reply_text(t("queued", lang, pos=_tts_queued + 1))
            except Exception:
                notice = None

        audio_bytes = await tts_generate_queued(text, voice["id"], rate)
        audio_file = io.BytesIO(audio_bytes)
        audio_file.name = "vone_tts.mp3"
        sent = await context.bot.send_audio(
            chat_id=user.id, audio=audio_file, title=voice["name"], performer="VONE"
        )
        audio = getattr(sent, "audio", None)
        if key and audio is not None and getattr(audio, "file_id", None):
            tts_cache_put(key, audio.file_id)
        success = True
    except asyncio.TimeoutError:
        _metrics["last_tts_error"] = "timeout"
        logger.warning("انتهت مهلة توليد الصوت للمستخدم %s", user.id)
        await msg.reply_text(t("tts_error", lang))
    except Exception as e:
        _metrics["last_tts_error"] = str(e)[:200]
        logger.warning("فشل توليد الصوت: %s", e)
        try:
            await msg.reply_text(t("tts_error", lang))
        except Exception:
            pass
    finally:
        if notice is not None:
            try:
                await notice.delete()
            except Exception:
                pass
        if success:
            _metrics["tts_ok"] += 1
        else:
            _metrics["tts_fail"] += 1
        record_request(st, success)


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if user is None or msg is None:
        return
    text = (msg.text or "").strip()
    st = await get_user(user)
    lang = st.lang
    is_admin = user.id == config.ADMIN_ID

    if await gate_blocked(update, user, lang):
        return

    if is_admin:
        if text in ("إحصائيات", "احصائيات"):
            await send_stats(context.bot, user.id, force=True)
            return
        if text in ("إذاعة", "اذاعة"):
            await begin_broadcast_mode(context.bot, user.id)
            return
        if text in ("لوحة", "لوحة التحكم", "ادمن", "أدمن"):
            await show_admin_home(context.bot, user.id)
            return

    name = user.first_name or "صديقنا"
    if not await passes_sub_gate(context.bot, user.id):
        await msg.reply_text(t("force_sub", lang, name=name), reply_markup=subscribe_keyboard(lang))
        return

    voice = get_voice(st.voice) if st.voice else None
    if not voice or voice["id"] in disabled_voices():
        await msg.reply_text(t("no_voice_selected", lang), reply_markup=main_menu_keyboard(lang))
        return

    if not text:
        return
    now = time.time()
    last = _last_request_time.get(user.id, 0)
    if now - last < S_int("cooldown"):
        await msg.reply_text(t("cooldown", lang))
        return
    _last_request_time[user.id] = now

    max_len = S_int("max_text")
    if len(text) > max_len:
        await msg.reply_text(t("text_too_long", lang, max=max_len))
        return

    await process_tts(update, context, st, voice, text)


# ====================================================================
# الإذاعة (نسخة مطوّرة): شريط تقدم + تسريع/تبطيء + إيقاف مؤقت + إلغاء + استئناف
# ====================================================================
#
# سبب توقف الإذاعة القديمة عند عدد قليل (لقيته بالكود):
#  1) حدّ تيليجرام (RetryAfter) كان يُحسب "فشل" بدون انتظار → باقي القائمة تفشل بسرعة.
#  2) خطأ داخل معالج Forbidden (حذف المستخدم من القاعدة) كان يقتل الحلقة بصمت.
# النسخة الجديدة: تنتظر وتخفّض السرعة تلقائياً، تعزل الأخطاء، تعيد المحاولة، وتحفظ تقدمها.

BC_MIN_RATE, BC_MAX_RATE, BC_STEP, BC_START_RATE = 2.0, 28.0, 4.0, 20.0
BC_CONCURRENCY, BC_CHUNK = 12, 200
_job = None


def _retry_seconds(e) -> float:
    ra = getattr(e, "retry_after", 1)
    try:
        return float(ra.total_seconds()) if hasattr(ra, "total_seconds") else float(ra)
    except Exception:
        return 5.0


class BroadcastJob:
    def __init__(self, admin_id, src_chat, src_msg, is_forward, rate=BC_START_RATE, job_id=None):
        self.id = job_id or int(time.time() * 1000)
        self.admin_id, self.src_chat, self.src_msg, self.is_forward = admin_id, src_chat, src_msg, is_forward
        self.state = "running"          # running | paused | cancelled | done | error
        self.total = 0
        self.cursor = 0
        self.sent = self.blocked = self.failed = 0
        self.retry_ids, self.blocked_ids = [], []
        self.rate = float(rate)
        self.next_slot = 0.0
        self.pause_until = 0.0
        self.throttled = 0
        self.last_error = ""
        self.consec_fail = 0
        self.started = time.time()
        self.progress_msg_id = None
        self.last_render = 0.0
        self.last_text = ""
        self.window = deque(maxlen=40)  # (وقت، عدد المعالَج) لقياس السرعة الفعلية
        self.phase = ""

    @property
    def done_count(self) -> int:
        return self.sent + self.blocked + self.failed

    def as_row(self) -> dict:
        return {"id": self.id, "admin_id": self.admin_id, "src_chat": self.src_chat, "src_msg": self.src_msg,
                "is_forward": self.is_forward, "total": self.total, "cursor": self.cursor, "sent": self.sent,
                "blocked": self.blocked, "failed": self.failed, "status": self.state, "rate": self.rate,
                "started": self.started}


def _bc_keyboard(job):
    if job.state not in ("running", "paused"):
        return None
    pause_btn = (
        InlineKeyboardButton("▶️ استئناف", callback_data="bc_pause", style="success")
        if job.state == "paused"
        else InlineKeyboardButton("⏸ إيقاف مؤقت", callback_data="bc_pause", style="primary")
    )
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏪ تبطيء", callback_data="bc_slow", style="primary"),
         InlineKeyboardButton("⏩ تسريع", callback_data="bc_fast", style="primary")],
        [pause_btn, InlineKeyboardButton("❌ إلغاء", callback_data="bc_stop", style="danger")],
    ])


def _bc_text(job) -> str:
    total = max(job.total, 1)
    done = job.done_count
    pct = min(100, int(done * 100 / total))
    bar = "▓" * (pct // 10) + "░" * (10 - pct // 10)
    titles = {"running": "📡 الإذاعة جارية", "paused": "⏸ الإذاعة متوقفة مؤقتاً", "cancelled": "⛔ تم إلغاء الإذاعة",
              "done": "✅ اكتملت الإذاعة", "error": "❌ توقفت الإذاعة بسبب خطأ"}
    lines = [titles.get(job.state, "📡 الإذاعة"), f"{bar} {pct}%", ""]
    lines.append(f"👥 المستهدفون: {job.total:,}")
    lines.append(f"✅ وصلت: {job.sent:,}")
    lines.append(f"🚫 حظروا البوت: {job.blocked:,}")
    lines.append(f"⚠️ فشلت: {job.failed:,}" + (f" (+{len(job.retry_ids)} بانتظار إعادة المحاولة)" if job.retry_ids else ""))
    if job.state in ("running", "paused"):
        speed = job.rate
        if len(job.window) >= 2:
            t0, d0 = job.window[0]
            t1, d1 = job.window[-1]
            if t1 > t0:
                speed = max(0.0, (d1 - d0) / (t1 - t0))
        remaining = max(0, job.total - done)
        eta = remaining / speed if speed > 0.05 else 0
        lines.append("")
        lines.append(f"⚡ السرعة المضبوطة: {job.rate:.0f} رسالة/ث (الفعلية ~{speed:.0f})")
        if job.state == "running" and eta:
            lines.append(f"⏱ المتبقي تقريباً: {_fmt_duration(eta)}")
        if job.phase:
            lines.append(f"🔁 {job.phase}")
    else:
        lines.append(f"⏱ المدة: {_fmt_duration(time.time() - job.started)}")
    if job.throttled:
        lines.append(f"🛡 حماية تيليجرام تدخلت {job.throttled} مرة (خفّضت السرعة تلقائياً)")
    if job.last_error and job.state in ("error", "running", "paused"):
        lines.append(f"ℹ️ آخر ملاحظة: {job.last_error}")
    return "\n".join(lines)


async def _bc_render(bot, job, force=False):
    now = time.monotonic()
    if not force and now - job.last_render < 3:
        return
    job.last_render = now
    job.window.append((now, job.done_count))
    if job.progress_msg_id is None:
        return
    text = _bc_text(job)
    if text == job.last_text and not force:
        return
    job.last_text = text
    try:
        await bot.edit_message_text(chat_id=job.admin_id, message_id=job.progress_msg_id, text=text,
                                    reply_markup=_bc_keyboard(job))
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.warning("تعذّر تحديث شريط الإذاعة: %s", e)
    except Exception as e:
        logger.warning("تعذّر تحديث شريط الإذاعة: %r", e)


def _bc_persist(job):
    bg_write(_db_job_save, job.as_row())


async def _bc_wait_slot(job):
    now = time.monotonic()
    if job.pause_until > now:
        await asyncio.sleep(job.pause_until - now)
    now = time.monotonic()
    slot = max(job.next_slot, now)
    job.next_slot = slot + 1.0 / max(job.rate, 0.5)
    if slot > now:
        await asyncio.sleep(slot - now)


async def _bc_send_one(bot, job, uid, final_pass=False):
    if uid == job.admin_id:
        job.total = max(0, job.total - 1)
        return
    attempts = 0
    while True:
        while job.state == "paused":
            await asyncio.sleep(0.5)
        if job.state not in ("running", "paused"):
            return
        await _bc_wait_slot(job)
        try:
            if job.is_forward:
                coro = bot.forward_message(chat_id=uid, from_chat_id=job.src_chat, message_id=job.src_msg)
            else:
                coro = bot.copy_message(chat_id=uid, from_chat_id=job.src_chat, message_id=job.src_msg)
            await asyncio.wait_for(coro, timeout=25)
            job.sent += 1
            job.consec_fail = 0
            return
        except RetryAfter as e:
            secs = _retry_seconds(e) + 1
            job.pause_until = max(job.pause_until, time.monotonic() + secs)
            job.rate = max(BC_MIN_RATE, job.rate * 0.75)
            job.throttled += 1
            job.last_error = f"حدّ تيليجرام: انتظار {int(secs)}ث وخفّضت السرعة تلقائياً"
            continue
        except Forbidden:
            job.blocked += 1
            job.blocked_ids.append(uid)
            return
        except BadRequest as e:
            m = str(e).lower()
            if any(k in m for k in ("chat not found", "user is deactivated", "peer_id_invalid", "bot was blocked", "user not found")):
                job.blocked += 1
                job.blocked_ids.append(uid)
                return
            job.failed += 1
            job.consec_fail += 1
            job.last_error = str(e)[:150]
            if job.sent == 0 and job.consec_fail >= 10:
                job.state = "error"
            return
        except (NetworkError, asyncio.TimeoutError, ConnectionError) as e:
            attempts += 1
            if attempts >= 3:
                if final_pass:
                    job.failed += 1
                    job.last_error = f"شبكة: {str(e)[:100]}"
                else:
                    job.retry_ids.append(uid)
                return
            await asyncio.sleep(attempts * 1.5)
        except Exception as e:
            job.failed += 1
            job.last_error = repr(e)[:150]
            return


async def _bc_send_chunk(bot, job, ids, final_pass=False):
    sem = asyncio.Semaphore(BC_CONCURRENCY)

    async def one(uid):
        async with sem:
            if job.state in ("cancelled", "error"):
                return
            await _bc_send_one(bot, job, uid, final_pass)

    await asyncio.gather(*(one(u) for u in ids))


async def run_broadcast(bot, job, resume=False):
    global _job
    try:
        if not resume:
            job.total = await run_read(_db_count_users, 0)
        else:
            job.total = max(job.total, job.done_count)
        msg = await bot.send_message(job.admin_id, _bc_text(job), reply_markup=_bc_keyboard(job))
        job.progress_msg_id = msg.message_id
        _bc_persist(job)
        while job.state in ("running", "paused"):
            ids = await run_read(_db_next_user_ids, job.cursor, BC_CHUNK)
            if not ids:
                break
            await _bc_send_chunk(bot, job, ids)
            if job.state not in ("running", "paused"):
                break
            job.cursor = ids[-1]
            await _bc_render(bot, job)
            _bc_persist(job)
        if job.state in ("running", "paused") and job.retry_ids:
            ids, job.retry_ids = list(job.retry_ids), []
            job.phase = f"إعادة محاولة {len(ids)} مستخدم تعذّر الوصول لهم"
            await asyncio.sleep(2)
            await _bc_send_chunk(bot, job, ids, final_pass=True)
            job.phase = ""
        if job.state in ("running", "paused"):
            job.state = "done"
    except Exception as e:
        logger.exception("خطأ بالإذاعة")
        job.state = "error"
        job.last_error = repr(e)[:150]
    finally:
        try:
            if job.blocked_ids:
                for uid in job.blocked_ids:
                    _users.pop(uid, None)
                bg_write(_db_delete_users, list(job.blocked_ids))
            _bc_persist(job)
            await _bc_render(bot, job, force=True)
            summary = _bc_text(job)
            await bot.send_message(job.admin_id, summary)
        except Exception as e:
            logger.warning("تعذّر إرسال ملخص الإذاعة: %r", e)
        _job = None


async def begin_broadcast_mode(bot, admin_id):
    if _job is not None:
        await bot.send_message(admin_id, "⚠️ فيه إذاعة شغالة حالياً — تحكّم فيها من رسالة التقدم أو انتظر تخلص.")
        return
    _awaiting_broadcast.add(admin_id)
    await bot.send_message(
        admin_id,
        "📡 تم تفعيل وضع الإذاعة.\n"
        "أرسل الآن المنشور: نص، صورة، فيديو، ملف، أو منشور محوّل من قناة.\n"
        "• المنشور المحوّل يوصل لهم كما هو بالضبط (مع علامة التحويل).\n"
        "• غير كذا يوصلهم منسوخ بدون اسمك.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء الإذاعة", callback_data="bc_cancel", style="danger")]]),
    )


class _BroadcastPendingFilter(filters.MessageFilter):
    def filter(self, message):
        u = message.from_user
        if u is None or u.id != config.ADMIN_ID or u.id not in _awaiting_broadcast:
            return False
        if message.chat is None or message.chat.type != "private":
            return False
        if (message.text or "").startswith("/"):
            return False
        cap = (message.caption or "").lower()
        if any(k in cap for k in BACKUP_KEYWORDS):
            return False
        return True


broadcast_pending_filter = _BroadcastPendingFilter()


async def broadcast_capture(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    admin_id = update.effective_user.id
    _awaiting_broadcast.discard(admin_id)
    is_fwd = getattr(msg, "forward_origin", None) is not None
    _bc_pending[admin_id] = {"chat": msg.chat_id, "msg": msg.message_id, "fwd": is_fwd}
    try:
        n = await run_read(_db_count_users, 0)
    except Exception:
        n = 0
    kind = "منشور محوّل — سيوصل كما هو بالضبط مع علامة التحويل" if is_fwd else "منشور عادي — سيوصل منسوخاً بدون اسمك"
    await msg.reply_text(
        f"📢 جاهز للإرسال\n👥 المستهدفون: ~{n:,} مستخدم\n📝 النوع: {kind}\n\n"
        "⬆️ هذا هو المنشور اللي بيوصلهم. تبدأ؟",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🚀 ابدأ الإذاعة الآن", callback_data="bc_go", style="success"),
            InlineKeyboardButton("❌ إلغاء", callback_data="bc_cancel", style="danger"),
        ]]),
    )


# ====================================================================
# النسخ الاحتياطي / الاستعادة
# ====================================================================

_backup_running = False
_backup_retry_at = 0.0


async def build_backup():
    tables = await run_read(_db_export)
    data = await asyncio.to_thread(_encode_backup, tables)
    counts = {k: len(v["rows"]) for k, v in tables.items()}
    return data, counts


async def send_backup(bot, reason="تلقائية") -> bool:
    global _backup_running, _backup_retry_at
    if _backup_running:
        return False
    _backup_running = True
    try:
        data, counts = await build_backup()
        fname = time.strftime("vone_backup_%Y-%m-%d_%H%M.json.gz", time.gmtime())
        caption = (
            f"💾 نسخة احتياطية ({reason})\n"
            f"👥 مستخدمين: {counts['users']:,} | 💙 مفضلة: {counts['favorites']:,}\n"
            f"📦 الحجم: {len(data) / 1024:.0f} KB\n\n"
            "للاستعادة: أرسل هذا الملف للبوت مع كلمة «استعادة» بالتعليق."
        )
        await bot.send_document(chat_id=config.ADMIN_ID, document=io.BytesIO(data), filename=fname, caption=caption)
        set_setting("last_backup", int(time.time()))
        logger.info("تم إرسال نسخة احتياطية (%s KB)", len(data) // 1024)
        return True
    except Exception as e:
        _backup_retry_at = time.time() + 3600
        logger.warning("فشل إنشاء/إرسال النسخة الاحتياطية: %r", e)
        return False
    finally:
        _backup_running = False


async def document_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = update.effective_user, update.effective_message
    if user is None or msg is None or user.id != config.ADMIN_ID or msg.document is None:
        return
    cap = (msg.caption or "").lower()
    if not any(k in cap for k in BACKUP_KEYWORDS):
        return
    doc = msg.document
    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await msg.reply_text("❌ الملف أكبر من 20 ميجا (حدّ تيليجرام لتحميل الملفات للبوتات).")
        return
    await msg.reply_text("⏳ جارٍ قراءة النسخة والتحقق منها...")
    try:
        f = await context.bot.get_file(doc.file_id)
        data = bytes(await f.download_as_bytearray())
        payload = await asyncio.to_thread(parse_backup, data)
    except ValueError as e:
        await msg.reply_text(f"❌ {e}")
        return
    except Exception as e:
        await msg.reply_text(f"❌ تعذّر تحميل الملف: {e!r}")
        return
    _pending_restore[user.id] = payload
    t_ = payload["tables"]
    created = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(payload.get("created") or 0))
    await msg.reply_text(
        "📥 النسخة سليمة ✅\n"
        f"🕒 تاريخها: {created}\n"
        f"👥 مستخدمين: {len(t_['users']['rows']):,}\n"
        f"💙 مفضلة: {len(t_['favorites']['rows']):,}\n\n"
        "الاستعادة بطريقة الدمج: ما يُمسح شي موجود، والمستخدم يتحدّث بس لو بياناته بالنسخة أحدث نشاطاً.\n"
        "تأكد الاستعادة؟",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ استعادة الآن", callback_data="adm:restore_ok", style="success"),
            InlineKeyboardButton("❌ إلغاء", callback_data="adm:restore_no", style="danger"),
        ]]),
    )


async def _reload_after_restore():
    global _stats_cache
    _users.clear()
    _sub_cache.clear()
    _stats_cache = None
    settings = await run_read(_db_load_settings)
    _settings.update({k: v for k, v in settings.items() if v is not None})
    _banned.clear()
    _banned.update(await run_read(_db_load_banned))
    _apply_max_tts()


# ====================================================================
# لوحة تحكم الأدمن
# ====================================================================

def _apply_max_tts():
    global tts_semaphore
    n = max(1, S_int("max_tts"))
    tts_semaphore = asyncio.Semaphore(n)


def _onoff(v: bool) -> str:
    return "✅" if v else "❌"


def admin_home_text() -> str:
    last = float(S("last_backup") or 0)
    last_txt = f"قبل {_fmt_duration(time.time() - last)}" if last > 0 else "لا يوجد بعد"
    job_txt = "لا توجد"
    if _job is not None:
        job_txt = f"{_job.done_count * 100 // max(_job.total, 1)}% ({_job.state})"
    return "\n".join([
        "🎛 <b>لوحة تحكم VONE</b>",
        SEP,
        f"🛠 وضع الصيانة: {_onoff(S('maintenance') == '1')}",
        f"🔒 الاشتراك الإجباري: {_onoff(S('force_sub') == '1')}",
        f"🚨 تنبيهات الأعطال: {_onoff(S('alerts') == '1')}",
        f"⚙️ التهدئة: {S_int('cooldown')}ث | أقصى نص: {S_int('max_text')} | توليد متزامن: {S_int('max_tts')}",
        f"🎙 أصوات معطّلة: {len(disabled_voices())} | 🚫 محظورون: {len(_banned)}",
        f"💾 آخر نسخة احتياطية: {last_txt}",
        f"📡 الإذاعة: {job_txt}",
    ])


def admin_home_keyboard():
    mt = S("maintenance") == "1"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 الإحصائيات", callback_data="adm:stats", style="primary"),
         InlineKeyboardButton("📢 الإذاعة", callback_data="adm:bc", style="primary")],
        [InlineKeyboardButton(f"🛠 الصيانة: {'إيقاف' if mt else 'تفعيل'}", callback_data="adm:tg:maintenance",
                              style="danger" if not mt else "success"),
         InlineKeyboardButton(f"🔒 الاشتراك: {_onoff(S('force_sub') == '1')}", callback_data="adm:tg:force_sub")],
        [InlineKeyboardButton("⚙️ الحدود", callback_data="adm:set"),
         InlineKeyboardButton("🎙 الأصوات", callback_data="adm:voices:0")],
        [InlineKeyboardButton("👤 المستخدمين", callback_data="adm:users"),
         InlineKeyboardButton("💾 النسخ الاحتياطي", callback_data="adm:backup")],
        [InlineKeyboardButton(f"🚨 تنبيهات الأعطال: {_onoff(S('alerts') == '1')}", callback_data="adm:tg:alerts")],
        [InlineKeyboardButton("🔄 تحديث", callback_data="adm:home")],
    ])


_BACK_ROW = [InlineKeyboardButton("🔙 لوحة التحكم", callback_data="adm:home")]


async def show_admin_home(bot, chat_id, message_id=None):
    text, kb = admin_home_text(), admin_home_keyboard()
    if message_id:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=kb,
                                        parse_mode=ParseMode.HTML)
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
    await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def _edit(query, text, kb=None, html=False):
    try:
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML if html else None)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


_SETTING_STEPS = {
    "cooldown": ("⏱ التهدئة بين الطلبات (ثانية)", 1, 0, 30),
    "max_text": ("📝 أقصى طول للنص (حرف)", 500, 500, 5000),
    "max_tts": ("🎙 توليدات متزامنة", 2, 2, 30),
}


def _settings_screen():
    lines = ["⚙️ <b>الحدود (تتغير فوراً بدون إعادة نشر)</b>", SEP]
    rows = []
    for key, (label, step, lo, hi) in _SETTING_STEPS.items():
        lines.append(f"{label}: <b>{S_int(key)}</b>")
        rows.append([
            InlineKeyboardButton("➖", callback_data=f"adm:set:{key}:-{step}"),
            InlineKeyboardButton(f"{label.split(' ', 1)[0]} {S_int(key)}", callback_data="adm:noop"),
            InlineKeyboardButton("➕", callback_data=f"adm:set:{key}:{step}"),
        ])
    rows.append(_BACK_ROW)
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _voices_admin_screen(page: int):
    allv = VOICES_AR + VOICES_EN
    items, total = _paginate(allv, page)
    dis = disabled_voices()
    rows = []
    for v in items:
        mark = "🚫" if v["id"] in dis else "✅"
        rows.append([InlineKeyboardButton(f"{mark} {v['name']}", callback_data=f"adm:vt:{v['id']}:{page}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("« السابق", callback_data=f"adm:voices:{page - 1}"))
    if (page + 1) * config.VOICES_PER_PAGE < total:
        nav.append(InlineKeyboardButton("التالي »", callback_data=f"adm:voices:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append(_BACK_ROW)
    return "🎙 <b>الأصوات</b>\nاضغط على صوت لتعطيله/تفعيله (المعطّل يختفي من قوائم المستخدمين فوراً).", InlineKeyboardMarkup(rows)


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    global _job
    query, user, bot = update.callback_query, update.effective_user, context.bot
    if user.id != config.ADMIN_ID:
        await query.answer("هذا الأمر للأدمن فقط ❌", show_alert=True)
        return
    chat_id, mid = query.message.chat_id, query.message.message_id

    if data in ("admin_stats_refresh", "adm:stats"):
        await query.answer("تم التحديث 🔄")
        await send_stats(bot, chat_id, mid, force=True)
        return
    if data == "adm:noop":
        await query.answer()
        return
    if data == "adm:home":
        await query.answer()
        await show_admin_home(bot, chat_id, mid)
        return
    if data.startswith("adm:tg:"):
        key = data.split(":", 2)[2]
        if key in ("maintenance", "force_sub", "alerts"):
            set_setting(key, "0" if S(key) == "1" else "1")
            await query.answer("تم ✅")
        await show_admin_home(bot, chat_id, mid)
        return
    if data == "adm:set":
        await query.answer()
        text, kb = _settings_screen()
        await _edit(query, text, kb, html=True)
        return
    if data.startswith("adm:set:"):
        _, _, key, delta = data.split(":", 3)
        if key in _SETTING_STEPS:
            _, _, lo, hi = _SETTING_STEPS[key]
            set_setting(key, max(lo, min(hi, S_int(key) + int(delta))))
            if key == "max_tts":
                _apply_max_tts()
        await query.answer("تم ✅")
        text, kb = _settings_screen()
        await _edit(query, text, kb, html=True)
        return
    if data.startswith("adm:voices:"):
        await query.answer()
        text, kb = _voices_admin_screen(int(data.split(":")[2]))
        await _edit(query, text, kb, html=True)
        return
    if data.startswith("adm:vt:"):
        _, _, vid, page = data.split(":", 3)
        if vid in VOICES_BY_ID:
            dis = disabled_voices()
            dis.symmetric_difference_update({vid})
            set_setting("disabled_voices", ",".join(sorted(dis)))
        await query.answer("تم ✅")
        text, kb = _voices_admin_screen(int(page))
        await _edit(query, text, kb, html=True)
        return
    if data == "adm:users":
        await query.answer()
        await _edit(
            query,
            "👤 <b>إدارة المستخدمين</b>\n" + SEP + f"\n🚫 محظورون حالياً: {len(_banned)}\n\n"
            "الأوامر:\n<code>/user 123456</code> — معلومات مستخدم\n<code>/ban 123456 السبب</code> — حظر\n"
            "<code>/unban 123456</code> — فك الحظر",
            InlineKeyboardMarkup([_BACK_ROW]), html=True,
        )
        return
    if data == "adm:backup":
        await query.answer()
        last = float(S("last_backup") or 0)
        last_txt = f"قبل {_fmt_duration(time.time() - last)}" if last > 0 else "لا يوجد بعد"
        await _edit(
            query,
            "💾 <b>النسخ الاحتياطي</b>\n" + SEP + f"\nآخر نسخة: {last_txt}\n"
            "تُرسل لك تلقائياً كل 24 ساعة.\n\n"
            "<b>للاستعادة:</b> أرسل ملف النسخة للبوت مع كلمة <code>استعادة</code> بالتعليق، وبعدها أكّد.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("💾 أرسل نسخة الآن", callback_data="adm:backup_now", style="primary")],
                _BACK_ROW,
            ]), html=True,
        )
        return
    if data == "adm:backup_now":
        await query.answer("⏳ جارٍ إنشاء النسخة...")
        ok = await send_backup(bot, reason="يدوية")
        if not ok:
            await bot.send_message(chat_id, "❌ تعذّر إنشاء النسخة، شوف اللوق.")
        return
    if data == "adm:restore_ok":
        payload = _pending_restore.pop(user.id, None)
        if payload is None:
            await query.answer("انتهت صلاحية الطلب، أعد إرسال الملف.", show_alert=True)
            return
        await query.answer("⏳ جارٍ الاستعادة...")
        await _edit(query, "⏳ جارٍ الاستعادة... لا تطفّي البوت.")
        try:
            n = await run_write(_db_import, payload)
            await _reload_after_restore()
            await _edit(query, f"✅ تمت الاستعادة بنجاح ({n:,} عملية). تم تحديث الذاكرة.")
        except Exception as e:
            logger.exception("فشلت الاستعادة")
            await _edit(query, f"❌ فشلت الاستعادة: {e!r}")
        return
    if data == "adm:restore_no":
        _pending_restore.pop(user.id, None)
        await query.answer("أُلغيت")
        await _edit(query, "❌ تم إلغاء الاستعادة.")
        return
    if data == "adm:bc":
        await query.answer()
        await begin_broadcast_mode(bot, chat_id)
        return

    # ---- أزرار الإذاعة ----
    if data == "bc_cancel":
        _awaiting_broadcast.discard(user.id)
        _bc_pending.pop(user.id, None)
        await query.answer("تم الإلغاء")
        await _edit(query, "❌ تم إلغاء الإذاعة.")
        return
    if data == "bc_go":
        info = _bc_pending.pop(user.id, None)
        if info is None:
            await query.answer("انتهت صلاحية الطلب، أعد تفعيل الإذاعة.", show_alert=True)
            return
        if _job is not None:
            await query.answer("فيه إذاعة شغالة حالياً.", show_alert=True)
            return
        await query.answer("🚀 بدأت الإذاعة")
        await _edit(query, "🚀 بدأت الإذاعة — تابع شريط التقدم بالرسالة الجاية.")
        _job = BroadcastJob(user.id, info["chat"], info["msg"], info["fwd"])
        spawn(run_broadcast(bot, _job))
        return
    if data.startswith("bc_resume:") or data.startswith("bc_ignore:"):
        job_id = int(data.split(":")[1])
        if data.startswith("bc_ignore:"):
            bg_write(_db_exec, "UPDATE broadcast_jobs SET status='abandoned' WHERE id=?", (job_id,))
            await query.answer("تم التجاهل")
            await _edit(query, "🗑 تم تجاهل الإذاعة المتوقفة.")
            return
        if _job is not None:
            await query.answer("فيه إذاعة شغالة حالياً.", show_alert=True)
            return
        row = await run_read(_db_job_get, job_id)
        if row is None:
            await query.answer("ما لقيت الإذاعة.", show_alert=True)
            return
        (jid, admin_id, src_chat, src_msg, is_fwd, total, cursor, sent, blocked, failed, _st, rate, started) = row
        await query.answer("▶️ استئناف")
        await _edit(query, "▶️ تم استئناف الإذاعة من حيث توقفت.")
        _job = BroadcastJob(admin_id, src_chat, src_msg, bool(is_fwd), rate or BC_START_RATE, job_id=jid)
        _job.total, _job.cursor, _job.sent, _job.blocked, _job.failed = total or 0, cursor or 0, sent or 0, blocked or 0, failed or 0
        _job.started = started or time.time()
        spawn(run_broadcast(bot, _job, resume=True))
        return

    job = _job
    if data in ("bc_slow", "bc_fast", "bc_pause", "bc_stop"):
        if job is None:
            await query.answer("ما فيه إذاعة شغالة.", show_alert=True)
            return
        if data == "bc_slow":
            job.rate = max(BC_MIN_RATE, job.rate - BC_STEP)
            await query.answer(f"⏪ السرعة الآن {job.rate:.0f}/ث")
        elif data == "bc_fast":
            job.rate = min(BC_MAX_RATE, job.rate + BC_STEP)
            msg = f"⏩ السرعة الآن {job.rate:.0f}/ث"
            if job.rate >= 24:
                msg += " — قريبة من حدّ تيليجرام، وإذا تدخّل يخفّض تلقائياً"
            await query.answer(msg, show_alert=job.rate >= 24)
        elif data == "bc_pause":
            job.state = "paused" if job.state == "running" else "running"
            await query.answer("⏸ متوقفة مؤقتاً" if job.state == "paused" else "▶️ استُؤنفت")
        else:
            job.state = "cancelled"
            await query.answer("⛔ جارٍ الإلغاء...")
        await _bc_render(bot, job, force=True)
        _bc_persist(job)
        return
    await query.answer()


# ====================================================================
# أوامر الأدمن النصية
# ====================================================================

def _is_admin(update) -> bool:
    u = update.effective_user
    return u is not None and u.id == config.ADMIN_ID


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_admin(update):
        await show_admin_home(context.bot, update.effective_chat.id)


async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    await update.effective_message.reply_text("⏳ جارٍ إنشاء النسخة...")
    if not await send_backup(context.bot, reason="يدوية"):
        await update.effective_message.reply_text("❌ تعذّر إنشاء النسخة، شوف اللوق.")


def _parse_uid(context):
    try:
        return int(context.args[0])
    except Exception:
        return None


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    uid = _parse_uid(context)
    if uid is None or uid == config.ADMIN_ID:
        await update.effective_message.reply_text("الاستخدام: /ban 123456 السبب")
        return
    reason = " ".join(context.args[1:])[:100]
    _banned.add(uid)
    bg_write(_db_exec, "INSERT OR REPLACE INTO banned(user_id, reason, ts) VALUES (?, ?, ?)", (uid, reason, time.time()))
    await update.effective_message.reply_text(f"🚫 تم حظر {uid}.")


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    uid = _parse_uid(context)
    if uid is None:
        await update.effective_message.reply_text("الاستخدام: /unban 123456")
        return
    _banned.discard(uid)
    bg_write(_db_exec, "DELETE FROM banned WHERE user_id=?", (uid,))
    await update.effective_message.reply_text(f"✅ تم فك الحظر عن {uid}.")


async def user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    uid = _parse_uid(context)
    if uid is None:
        await update.effective_message.reply_text("الاستخدام: /user 123456")
        return
    row = await run_read(_db_user_info, uid)
    if row is None:
        await update.effective_message.reply_text("ما لقيت هالمستخدم.")
        return
    _, un, tl, joined, active, voice, sub, lang, speed, sc = row
    fmt = lambda ts: time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts)) if ts else "—"
    await update.effective_message.reply_text(
        f"👤 {uid} @{un or '—'}\n🌐 لغة تيليجرام: {tl or '—'} | لغة البوت: {lang}\n"
        f"📅 انضم: {fmt(joined)}\n🕒 آخر نشاط: {fmt(active)}\n🔊 الصوت: {voice or '—'} | السرعة: {speed}\n"
        f"✅ تحويلات ناجحة: {sc or 0}\n📺 مشترك: {'نعم' if sub else 'لا/غير معروف'}\n"
        f"🚫 محظور: {'نعم' if uid in _banned else 'لا'}"
    )


async def channel_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmu = update.chat_member
    if cmu is None:
        return
    if (cmu.chat.username or "").lower() != config.CHANNEL_USERNAME.lstrip("@").lower():
        return
    status = cmu.new_chat_member.status in _sub_ok_statuses()
    _remember_sub(cmu.new_chat_member.user.id, status, config.SUB_TTL_OK if status else config.SUB_TTL_NO)


async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, Conflict):
        logger.warning("تعارض: نسخة ثانية من البوت تسحب التحديثات (طبيعي وقت النشر): %s", err)
    elif isinstance(err, (NetworkError, TimedOut)):
        logger.warning("خطأ شبكة مؤقت: %s", err)
    else:
        logger.error("خطأ غير معالج", exc_info=err)


# ====================================================================
# مهام الخلفية: التفريغ الدوري + النسخ الاحتياطي + تنبيه الأعطال
# ====================================================================

_alert_active = False
_last_alert = 0.0


async def _health_check(bot):
    global _alert_active, _last_alert
    if S("alerts") != "1":
        return
    now = time.time()
    samples = [ok for ts, ok in _recent_tts if now - ts <= 300]
    if len(samples) < 20:
        return
    rate = sum(1 for s in samples if s) * 100.0 / len(samples)
    if rate < 70 and not _alert_active and now - _last_alert > 1800:
        _alert_active, _last_alert = True, now
        await bot.send_message(
            config.ADMIN_ID,
            f"🚨 تنبيه: نسبة نجاح التوليد آخر 5 دقايق نزلت إلى {rate:.0f}% ({len(samples)} طلب)\n"
            f"آخر خطأ: {_metrics['last_tts_error'] or 'غير معروف'}\n"
            "غالباً مشكلة بخدمة الأصوات (مثل تغيير من مايكروسوفت) — جرّب تحديث edge-tts.",
        )
    elif _alert_active and rate >= 85:
        _alert_active = False
        await bot.send_message(config.ADMIN_ID, f"✅ رجعت نسبة نجاح التوليد طبيعية ({rate:.0f}%).")


async def _minute_tasks(bot):
    now = time.time()
    for uid in [u for u, ts in _last_request_time.items() if now - ts > 120]:
        _last_request_time.pop(uid, None)
    for uid in [u for u, (_, exp) in _sub_cache.items() if exp < now - 3600]:
        _sub_cache.pop(uid, None)
    logger.info(
        "مقاييس: مستخدمون بالذاكرة=%s تحويلات ok/fail/cache=%s/%s/%s طابور=%s متوسط توليد=%.1fث أخطاء قاعدة=%s",
        len(_users), _metrics["tts_ok"], _metrics["tts_fail"], _metrics["tts_hit"], _tts_queued,
        _metrics["gen_ms"] / 1000.0, _metrics["db_errors"],
    )
    await _health_check(bot)
    last = float(S("last_backup") or 0)
    if now - last >= 86400 and not _backup_running and now >= _backup_retry_at and now - _started_at > 120:
        spawn(send_backup(bot, reason="تلقائية كل 24 ساعة"))


async def housekeeping_loop(bot):
    tick = 0
    while True:
        await asyncio.sleep(config.FLUSH_INTERVAL_SECONDS)
        try:
            await flush_pending()
            tick += 1
            if tick % 12 == 0:
                await _minute_tasks(bot)
        except Exception as e:
            logger.warning("خطأ بمهمة الصيانة الدورية: %r", e)


async def _warm_tts_cache():
    try:
        rows = await run_read(_db_load_tts_cache, config.TTS_CACHE_SIZE)
        for key, fid in reversed(rows):
            _tts_cache[key] = fid
        logger.info("تم تحميل %s صوت متكرر بالكاش", len(rows))
    except Exception as e:
        logger.warning("تعذّر تحميل كاش الأصوات: %r", e)


async def _notify_interrupted_broadcast(bot):
    try:
        row = await run_read(_db_job_latest_running)
        if not row:
            return
        job_id, admin_id, _sc, _sm, _fw, total, cursor, sent, blocked, failed, _st, _rate, _started = row
        bg_write(_db_exec, "UPDATE broadcast_jobs SET status='interrupted' WHERE id=?", (job_id,))
        done = (sent or 0) + (blocked or 0) + (failed or 0)
        pct = done * 100 // max(total or 1, 1)
        await bot.send_message(
            admin_id,
            f"⚠️ إذاعة سابقة توقفت بسبب إعادة تشغيل البوت عند {pct}% ({done:,}/{total:,}).\nتبي تكملها من حيث وقفت؟",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("▶️ استئناف", callback_data=f"bc_resume:{job_id}", style="success"),
                InlineKeyboardButton("🗑 تجاهل", callback_data=f"bc_ignore:{job_id}", style="danger"),
            ]]),
        )
    except Exception as e:
        logger.warning("تعذّر فحص الإذاعات المتوقفة: %r", e)


async def post_init(application: Application):
    bot = application.bot
    cmds_en = [
        BotCommand("start", "Start the bot"),
        BotCommand("language", "Change language"),
        BotCommand("mystats", "My stats"),
    ]
    cmds_ar = [
        BotCommand("start", "بدء استخدام البوت"),
        BotCommand("language", "تغيير اللغة 🌐"),
        BotCommand("mystats", "إحصائياتي 📊"),
    ]
    admin_cmds = cmds_ar + [
        BotCommand("admin", "🎛 لوحة التحكم"),
        BotCommand("backup", "💾 نسخة احتياطية الآن"),
        BotCommand("user", "👤 معلومات مستخدم"),
        BotCommand("ban", "🚫 حظر مستخدم"),
        BotCommand("unban", "✅ فك حظر"),
    ]
    try:
        await bot.set_my_commands(cmds_en)
        await bot.set_my_commands(cmds_ar, language_code="ar")
        await bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=config.ADMIN_ID))
    except Exception as e:
        logger.warning("تعذّر ضبط قائمة الأوامر: %r", e)
    spawn(housekeeping_loop(bot))
    spawn(_warm_tts_cache())
    spawn(_notify_interrupted_broadcast(bot))


async def post_shutdown(application: Application):
    try:
        await flush_pending()
    except Exception as e:
        logger.warning("تعذّر التفريغ الأخير: %r", e)
    _read_pool.shutdown(wait=False)
    _write_pool.shutdown(wait=False)


def main():
    if not config.BOT_TOKEN:
        raise RuntimeError("متغيّر البيئة BOT_TOKEN غير موجود. أضفه من إعدادات Render.")

    # تجهيز قاعدة البيانات + تحميل الإعدادات وقائمة الحظر (كلها داخل خيط القاعدة)
    _write_pool.submit(_db_call, _db_init_schema).result(timeout=300)
    _settings.update({k: v for k, v in _write_pool.submit(_db_call, _db_load_settings).result(timeout=60).items() if v is not None})
    _banned.update(_write_pool.submit(_db_call, _db_load_banned).result(timeout=60))
    _apply_max_tts()

    threading.Thread(target=run_server, daemon=True).start()

    application = (
        ApplicationBuilder()
        .token(config.BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .concurrent_updates(True)
        .connection_pool_size(256)
        .pool_timeout(10.0)
        .connect_timeout(10.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("language", language_command))
    application.add_handler(CommandHandler("mystats", mystats_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("backup", backup_command))
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("unban", unban_command))
    application.add_handler(CommandHandler("user", user_command))
    application.add_handler(ChatMemberHandler(channel_member_update, ChatMemberHandler.CHAT_MEMBER))
    application.add_handler(CallbackQueryHandler(callback_router))
    # ترتيب مهم: التقاط الإذاعة قبل معالج الملفات والنصوص
    application.add_handler(MessageHandler(broadcast_pending_filter & filters.UpdateType.MESSAGE, broadcast_capture))
    application.add_handler(MessageHandler(
        filters.Document.ALL & filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE, document_router))
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE, text_router))
    application.add_error_handler(error_handler)

    logger.info("VONE bot is starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True, bootstrap_retries=5)


if __name__ == "__main__":
    main()
