"""
VONE — بوت تيليجرام لتحويل النص إلى صوت باستخدام أصوات عربية وإنجليزية مجانية من Microsoft Edge TTS.

واجهة المستخدم ثنائية اللغة (عربي/إنجليزي) — لوحة الأدمن (إحصائيات/إذاعة) تبقى عربي فقط.
قاعدة البيانات: Turso (libSQL) عند توفر متغيرات البيئة TURSO_DATABASE_URL و TURSO_AUTH_TOKEN،
وإلا يرجع تلقائياً لملف SQLite محلي كما كان.
"""

import os
import io
import time
import asyncio
import logging
import sqlite3
import threading
from contextlib import contextmanager

import edge_tts
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus, ChatAction, ParseMode
from telegram.error import Forbidden, BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from server import run_server

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger("vone_bot")


class config:
    BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
    ADMIN_ID = int(os.environ.get("ADMIN_ID", "6043858925"))
    CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "@ZenoX_Tools").strip()
    CHANNEL_LINK = os.environ.get("CHANNEL_LINK", "https://t.me/ZenoX_Tools").strip()
    PORT = int(os.environ.get("PORT", "10000"))
    DB_PATH = os.environ.get("DB_PATH", "bot_database.db")

    TURSO_URL = os.environ.get("TURSO_DATABASE_URL", "").strip()
    TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "").strip()

    MAX_CONCURRENT_TTS = 5
    PER_USER_COOLDOWN_SECONDS = 3
    MAX_TEXT_LENGTH = 2000
    BROADCAST_DELAY_SECONDS = 0.05
    VOICES_PER_PAGE = 10
    ACTIVE_NOW_WINDOW_MINUTES = 5


# ====================================================================
# الترجمة — كل النصوص اللي يشوفها المستخدم (عدا لوحة الأدمن) بمكان واحد
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
    },
}


def t(key: str, lang: str, **kwargs) -> str:
    lang = lang if lang in STRINGS else "ar"
    template = STRINGS[lang].get(key, STRINGS["ar"].get(key, key))
    return template.format(**kwargs) if kwargs else template


# ====================================================================
# الأصوات — مجموعتين منفصلتين (عربي / إنجليزي) من Microsoft Edge TTS
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
# قاعدة البيانات
# ====================================================================

_db_lock = threading.Lock()
_conn = None


def _db_connect():
    if config.TURSO_URL and config.TURSO_AUTH_TOKEN:
        try:
            import libsql
            conn = libsql.connect(database=config.TURSO_URL, auth_token=config.TURSO_AUTH_TOKEN)
            logger.info("تم الاتصال المباشر بقاعدة بيانات Turso بنجاح — كل عملية تُحفظ فوراً وبشكل دائم ✅")
            return conn
        except Exception as e:
            logger.error(
                "فشل الاتصال بـ Turso، سيعمل البوت بملف محلي مؤقت بدون حفظ دائم: %s", e
            )

    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


@contextmanager
def _get_conn():
    global _conn
    with _db_lock:
        if _conn is None:
            _conn = _db_connect()
        yield _conn
        _conn.commit()


class db:
    @staticmethod
    def init_db():
        with _get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    language_code TEXT,
                    joined_at REAL,
                    last_active REAL,
                    selected_voice TEXT,
                    is_subscribed INTEGER DEFAULT 0,
                    lang TEXT DEFAULT 'ar'
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
            # ترقية آمنة لقاعدة بيانات موجودة سابقاً (مثل قاعدة Turso الحالية
            # اللي فيها مستخدمين حقيقيين) — نضيف عمود lang لو ناقص بدون أي
            # خطر على البيانات الموجودة.
            cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
            if "lang" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN lang TEXT DEFAULT 'ar'")

    @staticmethod
    def upsert_user(user_id: int, username: str, language_code: str):
        now = time.time()
        with _get_conn() as conn:
            cur = conn.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
            exists = cur.fetchone() is not None
            if exists:
                conn.execute(
                    "UPDATE users SET username=?, language_code=?, last_active=? WHERE user_id=?",
                    (username, language_code, now, user_id),
                )
            else:
                conn.execute(
                    "INSERT INTO users (user_id, username, language_code, joined_at, last_active, selected_voice, lang) "
                    "VALUES (?, ?, ?, ?, ?, NULL, 'ar')",
                    (user_id, username, language_code, now, now),
                )
        return not exists

    @staticmethod
    def touch_user(user_id: int):
        with _get_conn() as conn:
            conn.execute("UPDATE users SET last_active=? WHERE user_id=?", (time.time(), user_id))

    @staticmethod
    def set_selected_voice(user_id: int, voice_id: str):
        with _get_conn() as conn:
            conn.execute("UPDATE users SET selected_voice=? WHERE user_id=?", (voice_id, user_id))

    @staticmethod
    def get_selected_voice(user_id: int):
        with _get_conn() as conn:
            cur = conn.execute("SELECT selected_voice FROM users WHERE user_id=?", (user_id,))
            row = cur.fetchone()
            return row[0] if row else None

    @staticmethod
    def set_subscribed(user_id: int, subscribed: bool):
        with _get_conn() as conn:
            conn.execute(
                "UPDATE users SET is_subscribed=? WHERE user_id=?",
                (1 if subscribed else 0, user_id),
            )

    @staticmethod
    def set_lang(user_id: int, lang: str):
        with _get_conn() as conn:
            conn.execute("UPDATE users SET lang=? WHERE user_id=?", (lang, user_id))

    @staticmethod
    def get_lang(user_id: int) -> str:
        with _get_conn() as conn:
            cur = conn.execute("SELECT lang FROM users WHERE user_id=?", (user_id,))
            row = cur.fetchone()
            return row[0] if row and row[0] else "ar"

    @staticmethod
    def get_all_user_ids():
        with _get_conn() as conn:
            cur = conn.execute("SELECT user_id FROM users")
            return [r[0] for r in cur.fetchall()]

    @staticmethod
    def remove_user(user_id: int):
        with _get_conn() as conn:
            conn.execute("DELETE FROM users WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM favorites WHERE user_id=?", (user_id,))

    @staticmethod
    def toggle_favorite(user_id: int, voice_id: str) -> bool:
        with _get_conn() as conn:
            cur = conn.execute(
                "SELECT 1 FROM favorites WHERE user_id=? AND voice_id=?", (user_id, voice_id)
            )
            if cur.fetchone():
                conn.execute(
                    "DELETE FROM favorites WHERE user_id=? AND voice_id=?", (user_id, voice_id)
                )
                return False
            else:
                conn.execute(
                    "INSERT INTO favorites (user_id, voice_id) VALUES (?, ?)", (user_id, voice_id)
                )
                return True

    @staticmethod
    def get_favorites(user_id: int):
        with _get_conn() as conn:
            cur = conn.execute("SELECT voice_id FROM favorites WHERE user_id=?", (user_id,))
            return [r[0] for r in cur.fetchall()]

    @staticmethod
    def is_favorite(user_id: int, voice_id: str) -> bool:
        with _get_conn() as conn:
            cur = conn.execute(
                "SELECT 1 FROM favorites WHERE user_id=? AND voice_id=?", (user_id, voice_id)
            )
            return cur.fetchone() is not None

    @staticmethod
    def log_request(user_id: int, success: bool):
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO requests (user_id, created_at, success) VALUES (?, ?, ?)",
                (user_id, time.time(), 1 if success else 0),
            )

    @staticmethod
    def get_stats():
        now = time.time()
        day = 86400
        with _get_conn() as conn:
            total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

            active_now = conn.execute(
                "SELECT COUNT(*) FROM users WHERE last_active >= ?",
                (now - config.ACTIVE_NOW_WINDOW_MINUTES * 60,),
            ).fetchone()[0]

            active_7d = conn.execute(
                "SELECT COUNT(*) FROM users WHERE last_active >= ?", (now - 7 * day,)
            ).fetchone()[0]

            active_30d = conn.execute(
                "SELECT COUNT(*) FROM users WHERE last_active >= ?", (now - 30 * day,)
            ).fetchone()[0]

            subscribed_count = conn.execute(
                "SELECT COUNT(*) FROM users WHERE is_subscribed=1"
            ).fetchone()[0]

            total_requests = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
            success_requests = conn.execute(
                "SELECT COUNT(*) FROM requests WHERE success=1"
            ).fetchone()[0]
            success_rate = (success_requests / total_requests * 100) if total_requests else 100.0

            lang_rows = conn.execute(
                "SELECT language_code, COUNT(*) c FROM users "
                "WHERE language_code IS NOT NULL GROUP BY language_code ORDER BY c DESC LIMIT 5"
            ).fetchall()
            top_languages = [(row[0] or "غير معروف", row[1]) for row in lang_rows]

        return {
            "total_users": total_users,
            "active_now": active_now,
            "active_7d": active_7d,
            "active_30d": active_30d,
            "subscribed_count": subscribed_count,
            "total_requests": total_requests,
            "success_rate": success_rate,
            "top_languages": top_languages,
        }


# ====================================================================
# الأزرار (Inline Keyboards) — كلها تأخذ lang وترجع النصوص الصحيحة
# ====================================================================

def main_menu_keyboard(lang: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t("btn_voices", lang), callback_data="menu_voices", style="primary")],
        [InlineKeyboardButton(t("btn_favorites", lang), callback_data="menu_fav", style="primary")],
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


def _paginate(items, page):
    start = page * config.VOICES_PER_PAGE
    end = start + config.VOICES_PER_PAGE
    return items[start:end], len(items)


def voices_list_keyboard(page: int, favorites: set, lang: str):
    pool = get_voice_pool(lang)
    page_items, total = _paginate(pool, page)
    rows = []
    for v in page_items:
        is_fav = v["id"] in favorites
        heart = "💙" if is_fav else "🤍"
        rows.append([
            InlineKeyboardButton(
                f"{v['emoji']} {v['name']}", callback_data=f"vsel:list:{page}:{v['id']}"
            ),
            InlineKeyboardButton(
                heart, callback_data=f"vtog:list:{page}:{v['id']}"
            ),
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


def favorites_list_keyboard(page: int, favorite_voice_ids: list, lang: str):
    pool = get_voice_pool(lang)
    fav_voices = [v for v in pool if v["id"] in favorite_voice_ids]
    page_items, total = _paginate(fav_voices, page)
    rows = []
    for v in page_items:
        rows.append([
            InlineKeyboardButton(
                f"{v['emoji']} {v['name']} 💙", callback_data=f"vsel:fav:{page}:{v['id']}"
            ),
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


def stats_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("تحديث 🔄", callback_data="admin_stats_refresh", style="primary")],
    ])


# ====================================================================
# منطق البوت
# ====================================================================

tts_semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_TTS)
_last_request_time = {}


async def is_subscribed(bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=config.CHANNEL_USERNAME, user_id=user_id)
        subscribed = member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )
    except BadRequest as e:
        logger.warning("تعذّر التحقق من الاشتراك للمستخدم %s: %s", user_id, e)
        subscribed = False
    except Exception as e:
        logger.warning("خطأ غير متوقع أثناء التحقق من الاشتراك: %s", e)
        subscribed = False

    db.set_subscribed(user_id, subscribed)
    return subscribed


SEP = "➖➖➖➖➖➖➖➖➖➖➖➖➖➖➖"


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
        "",
        SEP,
        "",
        "🌍 <b>أبرز اللغات لدى المستخدمين</b>",
    ]
    if stats["top_languages"]:
        total = stats["total_users"] or 1
        for lang, count in stats["top_languages"]:
            pct = count / total * 100
            lines.append(f"▫️ {lang}: <b>{count}</b> ({pct:.1f}%)")
    else:
        lines.append("▫️ لا توجد بيانات كافية بعد")

    lines += [
        "",
        SEP,
        "",
        "📨 <b>الطلبات</b>",
        f"📥 إجمالي الطلبات: <b>{stats['total_requests']}</b>",
        f"✅ نسبة نجاح البوت: <b>{stats['success_rate']:.1f}%</b>",
        "",
        SEP,
        "",
        "ℹ️ لغة المستخدم تُستخدم كأقرب تقريب متاح بدل الدولة الفعلية"
        " (تيليجرام لا يوفّر بيانات دولة حقيقية عبر الـ API).",
        "",
        f"🕒 آخر تحديث: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
    ]
    return "\n".join(lines)


async def send_stats(bot, chat_id: int, message_id: int = None):
    stats = db.get_stats()
    text = build_stats_text(stats)
    if message_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=stats_keyboard(),
                parse_mode=ParseMode.HTML,
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                logger.warning("فشل تحديث رسالة الإحصائيات: %s", e)
        return
    await bot.send_message(
        chat_id=chat_id, text=text, reply_markup=stats_keyboard(), parse_mode=ParseMode.HTML
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.upsert_user(user.id, user.username or "", user.language_code or "")

    name = user.first_name or "صديقنا"
    lang = db.get_lang(user.id)
    subscribed = await is_subscribed(context.bot, user.id)

    if not subscribed:
        await update.message.reply_text(
            t("force_sub", lang, name=name), reply_markup=subscribe_keyboard(lang)
        )
        return

    await update.message.reply_text(
        t("welcome", lang, name=name), reply_markup=main_menu_keyboard(lang)
    )


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    data = query.data or ""
    name = user.first_name or "صديقنا"

    db.touch_user(user.id)
    lang = db.get_lang(user.id)

    if data == "check_sub":
        subscribed = await is_subscribed(context.bot, user.id)
        if not subscribed:
            await query.answer(t("not_subscribed_alert", lang), show_alert=True)
            return
        await query.answer(t("verified_alert", lang), show_alert=True)
        try:
            await query.edit_message_text(
                t("welcome", lang, name=name), reply_markup=main_menu_keyboard(lang)
            )
        except BadRequest:
            await context.bot.send_message(
                chat_id=user.id,
                text=t("welcome", lang, name=name),
                reply_markup=main_menu_keyboard(lang),
            )
        return

    if user.id != config.ADMIN_ID and not await is_subscribed(context.bot, user.id):
        await query.answer(t("must_subscribe_alert", lang), show_alert=True)
        try:
            await query.edit_message_text(
                t("force_sub", lang, name=name), reply_markup=subscribe_keyboard(lang)
            )
        except BadRequest:
            pass
        return

    if data == "admin_stats_refresh":
        if user.id != config.ADMIN_ID:
            await query.answer("هذا الأمر للأدمن فقط ❌", show_alert=True)
            return
        await query.answer("تم التحديث 🔄")
        await send_stats(context.bot, query.message.chat_id, query.message.message_id)
        return

    if data.startswith("set_lang:"):
        new_lang = data.split(":", 1)[1]
        if new_lang not in ("ar", "en"):
            return
        db.set_lang(user.id, new_lang)
        await query.answer(t("lang_changed", new_lang))
        await query.edit_message_text(
            t("welcome", new_lang, name=name), reply_markup=main_menu_keyboard(new_lang)
        )
        return

    await query.answer()

    if data == "back_main":
        await query.edit_message_text(
            t("welcome", lang, name=name), reply_markup=main_menu_keyboard(lang)
        )
        return

    if data == "menu_voices":
        favs = set(db.get_favorites(user.id))
        await query.edit_message_text(
            t("voices_header", lang), reply_markup=voices_list_keyboard(0, favs, lang)
        )
        return

    if data == "menu_fav":
        fav_ids = db.get_favorites(user.id)
        if not fav_ids:
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton(t("btn_back", lang), callback_data="back_main")]]
            )
            await query.edit_message_text(t("favorites_empty", lang), reply_markup=kb)
            return
        await query.edit_message_text(
            t("favorites_header", lang), reply_markup=favorites_list_keyboard(0, fav_ids, lang)
        )
        return

    if data.startswith("vlist:"):
        page = int(data.split(":")[1])
        favs = set(db.get_favorites(user.id))
        await query.edit_message_text(
            t("voices_header", lang), reply_markup=voices_list_keyboard(page, favs, lang)
        )
        return

    if data.startswith("vfav:"):
        page = int(data.split(":")[1])
        fav_ids = db.get_favorites(user.id)
        await query.edit_message_text(
            t("favorites_header", lang), reply_markup=favorites_list_keyboard(page, fav_ids, lang)
        )
        return

    if data.startswith("vsel:"):
        _, origin, page, voice_id = data.split(":", 3)
        voice = get_voice(voice_id)
        if not voice:
            await query.answer(t("voice_unavailable_alert", lang), show_alert=True)
            return
        db.set_selected_voice(user.id, voice_id)
        await query.answer(t("voice_selected_alert", lang, name=voice["name"], emoji=voice["emoji"]))
        favs = set(db.get_favorites(user.id))
        header = t("voice_selected_header", lang, emoji=voice["emoji"], name=voice["name"])
        if origin == "fav":
            fav_ids = db.get_favorites(user.id)
            await query.edit_message_text(
                header, reply_markup=favorites_list_keyboard(int(page), fav_ids, lang)
            )
        else:
            await query.edit_message_text(
                header, reply_markup=voices_list_keyboard(int(page), favs, lang)
            )
        return

    if data.startswith("vtog:"):
        _, origin, page, voice_id = data.split(":", 3)
        now_fav = db.toggle_favorite(user.id, voice_id)
        await query.answer(t("added_favorite_alert", lang) if now_fav else t("removed_favorite_alert", lang))

        page = int(page)
        if origin == "fav":
            fav_ids = db.get_favorites(user.id)
            if page > 0 and page * config.VOICES_PER_PAGE >= len(fav_ids):
                page -= 1
            if not fav_ids:
                kb = InlineKeyboardMarkup(
                    [[InlineKeyboardButton(t("btn_back", lang), callback_data="back_main")]]
                )
                await query.edit_message_text(t("favorites_empty", lang), reply_markup=kb)
                return
            await query.edit_message_reply_markup(reply_markup=favorites_list_keyboard(page, fav_ids, lang))
        else:
            favs = set(db.get_favorites(user.id))
            await query.edit_message_reply_markup(reply_markup=voices_list_keyboard(page, favs, lang))
        return


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (update.message.text or "").strip()
    db.upsert_user(user.id, user.username or "", user.language_code or "")
    db.touch_user(user.id)

    if user.id == config.ADMIN_ID:
        if context.user_data.get("awaiting_broadcast"):
            context.user_data["awaiting_broadcast"] = False
            await do_broadcast(context.bot, user.id, update.message)
            return

        if text in ("إحصائيات", "احصائيات"):
            await send_stats(context.bot, user.id)
            return

        if text in ("إذاعة", "اذاعة"):
            context.user_data["awaiting_broadcast"] = True
            await update.message.reply_text(
                "📡 تم تفعيل وضع الإذاعة.\nأرسل الآن المنشور (نص أو صورة أو أي رسالة) الذي تريد إرساله لجميع المستخدمين."
            )
            return

    name = user.first_name or "صديقنا"
    lang = db.get_lang(user.id)

    if not await is_subscribed(context.bot, user.id):
        await update.message.reply_text(
            t("force_sub", lang, name=name), reply_markup=subscribe_keyboard(lang)
        )
        return

    voice_id = db.get_selected_voice(user.id)
    voice = get_voice(voice_id) if voice_id else None
    if not voice:
        await update.message.reply_text(
            t("no_voice_selected", lang), reply_markup=main_menu_keyboard(lang)
        )
        return

    now = time.time()
    last = _last_request_time.get(user.id, 0)
    if now - last < config.PER_USER_COOLDOWN_SECONDS:
        await update.message.reply_text(t("cooldown", lang))
        return
    _last_request_time[user.id] = now

    if len(text) == 0:
        return
    if len(text) > config.MAX_TEXT_LENGTH:
        await update.message.reply_text(t("text_too_long", lang, max=config.MAX_TEXT_LENGTH))
        return

    await context.bot.send_chat_action(chat_id=user.id, action=ChatAction.RECORD_VOICE)

    success = False
    try:
        async with tts_semaphore:
            audio_bytes = await generate_speech(text, voice["id"])
        if not audio_bytes:
            raise RuntimeError("empty audio")

        audio_file = io.BytesIO(audio_bytes)
        audio_file.name = "vone_tts.mp3"
        await context.bot.send_audio(
            chat_id=user.id,
            audio=audio_file,
            title=voice["name"],
            performer="VONE",
        )
        success = True
    except Exception as e:
        logger.exception("فشل توليد الصوت: %s", e)
        await update.message.reply_text(t("tts_error", lang))
    finally:
        db.log_request(user.id, success)


async def generate_speech(text: str, voice_id: str) -> bytes:
    communicate = edge_tts.Communicate(text, voice_id)
    chunks = bytearray()
    async for chunk in communicate.stream():
        if chunk.get("type") == "audio":
            chunks.extend(chunk["data"])
    return bytes(chunks)


async def do_broadcast(bot, admin_id: int, source_message):
    user_ids = db.get_all_user_ids()
    await bot.send_message(admin_id, f"📡 جارٍ إرسال الإذاعة إلى {len(user_ids)} مستخدم بهدوء...")

    sent, failed = 0, 0
    for uid in user_ids:
        if uid == admin_id:
            continue
        try:
            await bot.copy_message(
                chat_id=uid,
                from_chat_id=source_message.chat_id,
                message_id=source_message.message_id,
            )
            sent += 1
        except Forbidden:
            db.remove_user(uid)
            failed += 1
        except Exception as e:
            logger.warning("فشل إرسال الإذاعة للمستخدم %s: %s", uid, e)
            failed += 1

        await asyncio.sleep(config.BROADCAST_DELAY_SECONDS)

    await bot.send_message(
        admin_id, f"✅ انتهت الإذاعة\n\nنجح الإرسال: {sent}\nفشل الإرسال: {failed}"
    )


async def post_init(application: Application):
    await application.bot.set_my_commands([BotCommand("start", "بدء استخدام البوت / Start the bot")])


def main():
    if not config.BOT_TOKEN:
        raise RuntimeError("متغيّر البيئة BOT_TOKEN غير موجود. أضفه من إعدادات Render.")

    db.init_db()

    threading.Thread(target=run_server, daemon=True).start()

    application = (
        ApplicationBuilder()
        .token(config.BOT_TOKEN)
        .post_init(post_init)
        .concurrent_updates(True)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(callback_router))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    logger.info("VONE bot is starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()



