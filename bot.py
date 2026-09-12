"""
VONE — بوت تيليجرام لتحويل النص إلى صوت باستخدام أصوات عربية مجانية من Microsoft Edge TTS.
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

    MAX_CONCURRENT_TTS = 5
    PER_USER_COOLDOWN_SECONDS = 3
    MAX_TEXT_LENGTH = 2000
    BROADCAST_DELAY_SECONDS = 0.05
    VOICES_PER_PAGE = 10
    ACTIVE_NOW_WINDOW_MINUTES = 5


VOICES = [
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

VOICES_BY_ID = {v["id"]: v for v in VOICES}


def get_voice(voice_id: str):
    return VOICES_BY_ID.get(voice_id)


_db_lock = threading.Lock()


def _db_connect():
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


@contextmanager
def _get_conn():
    with _db_lock:
        conn = _db_connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


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
                    is_subscribed INTEGER DEFAULT 0
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
                    "INSERT INTO users (user_id, username, language_code, joined_at, last_active, selected_voice) "
                    "VALUES (?, ?, ?, ?, ?, NULL)",
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


def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("قائمة الأصوات 🔊", callback_data="menu_voices", style="primary")],
        [InlineKeyboardButton("الأصوات المُفضلة 💙", callback_data="menu_fav", style="primary")],
    ])


def subscribe_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("أشترك في القناة 📺", url=config.CHANNEL_LINK, style="danger")],
        [InlineKeyboardButton("تحقق 🔍", callback_data="check_sub", style="primary")],
    ])


def _paginate(items, page):
    start = page * config.VOICES_PER_PAGE
    end = start + config.VOICES_PER_PAGE
    return items[start:end], len(items)


def voices_list_keyboard(page: int, favorites: set):
    page_items, total = _paginate(VOICES, page)
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
        nav_row.append(InlineKeyboardButton("« السابق", callback_data=f"vlist:{page - 1}"))
    if (page + 1) * config.VOICES_PER_PAGE < total:
        nav_row.append(InlineKeyboardButton("التالي »", callback_data=f"vlist:{page + 1}"))
    if nav_row:
        rows.append(nav_row)

    rows.append([InlineKeyboardButton("رجوع للقائمة الرئيسية 🖲", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


def favorites_list_keyboard(page: int, favorite_voice_ids: list):
    fav_voices = [v for v in VOICES if v["id"] in favorite_voice_ids]
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
        nav_row.append(InlineKeyboardButton("« السابق", callback_data=f"vfav:{page - 1}"))
    if (page + 1) * config.VOICES_PER_PAGE < total:
        nav_row.append(InlineKeyboardButton("التالي »", callback_data=f"vfav:{page + 1}"))
    if nav_row:
        rows.append(nav_row)

    rows.append([InlineKeyboardButton("رجوع للقائمة الرئيسية 🖲", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


def stats_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("تحديث 🔄", callback_data="admin_stats_refresh", style="primary")],
    ])


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


def welcome_text(name: str) -> str:
    return (
        f"مرحبا بك مجدداً يا {name} في بوت VONE\n\n"
        "طريقة أستخدام البوت: ⚙️\n\n"
        "- أضغط على قائمة الأصوات 🔊\n"
        "- اختر الصوت المُناسب لك 🌐\n"
        "- أرسل النص المُراد تحويلة 🖊"
    )


def force_sub_text(name: str) -> str:
    return (
        f"مرحباً بك يا {name} في بوت VONE\n\n"
        "🚧 يجب عليك إكمال الخطوات التالية!:\n\n"
        "- أنضم الى قناة البوت اولا 📺\n"
        "- أضغط على زر التحقق 🔍\n"
        "- أرسل أمر /start للبدء ⚙️"
    )


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
    subscribed = await is_subscribed(context.bot, user.id)

    if not subscribed:
        await update.message.reply_text(force_sub_text(name), reply_markup=subscribe_keyboard())
        return

    await update.message.reply_text(welcome_text(name), reply_markup=main_menu_keyboard())


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    data = query.data or ""
    name = user.first_name or "صديقنا"

    db.touch_user(user.id)

    if data == "check_sub":
        subscribed = await is_subscribed(context.bot, user.id)
        if not subscribed:
            await query.answer("لم تشترك في القناة بعد ❌", show_alert=True)
            return
        await query.answer("تم التحقق بنجاح ✅", show_alert=True)
        try:
            await query.edit_message_text(welcome_text(name), reply_markup=main_menu_keyboard())
        except BadRequest:
            await context.bot.send_message(
                chat_id=user.id, text=welcome_text(name), reply_markup=main_menu_keyboard()
            )
        return

    if user.id != config.ADMIN_ID and not await is_subscribed(context.bot, user.id):
        await query.answer("يجب الاشتراك في القناة أولاً ❌", show_alert=True)
        try:
            await query.edit_message_text(force_sub_text(name), reply_markup=subscribe_keyboard())
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

    await query.answer()

    if data == "back_main":
        await query.edit_message_text(welcome_text(name), reply_markup=main_menu_keyboard())
        return

    if data == "menu_voices":
        favs = set(db.get_favorites(user.id))
        await query.edit_message_text(
            "🔊 قائمة الأصوات المتوفّرة — اضغط على الصوت لتفعيله، أو على القلب لإضافته للمفضلة:",
            reply_markup=voices_list_keyboard(0, favs),
        )
        return

    if data == "menu_fav":
        fav_ids = db.get_favorites(user.id)
        if not fav_ids:
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("رجوع للقائمة الرئيسية 🖲", callback_data="back_main")]]
            )
            await query.edit_message_text("لا توجد أصوات مفضّلة بعد 💙", reply_markup=kb)
            return
        await query.edit_message_text(
            "💙 قائمة أصواتك المفضّلة:", reply_markup=favorites_list_keyboard(0, fav_ids)
        )
        return

    if data.startswith("vlist:"):
        page = int(data.split(":")[1])
        favs = set(db.get_favorites(user.id))
        await query.edit_message_text(
            "🔊 قائمة الأصوات المتوفّرة — اضغط على الصوت لتفعيله، أو على القلب لإضافته للمفضلة:",
            reply_markup=voices_list_keyboard(page, favs),
        )
        return

    if data.startswith("vfav:"):
        page = int(data.split(":")[1])
        fav_ids = db.get_favorites(user.id)
        await query.edit_message_text(
            "💙 قائمة أصواتك المفضّلة:", reply_markup=favorites_list_keyboard(page, fav_ids)
        )
        return

    if data.startswith("vsel:"):
        _, origin, page, voice_id = data.split(":", 3)
        voice = get_voice(voice_id)
        if not voice:
            await query.answer("هذا الصوت لم يعد متوفراً", show_alert=True)
            return
        db.set_selected_voice(user.id, voice_id)
        await query.answer(f"تم اختيار الصوت: {voice['name']} {voice['emoji']} ✅")
        favs = set(db.get_favorites(user.id))
        header = (
            f"✅ الصوت الحالي: {voice['emoji']} {voice['name']}\n"
            "أرسل الآن النص الذي تريد تحويله إلى صوت ✍️\n\n"
            "أو اختر صوتاً آخر من القائمة:"
        )
        if origin == "fav":
            fav_ids = db.get_favorites(user.id)
            await query.edit_message_text(header, reply_markup=favorites_list_keyboard(int(page), fav_ids))
        else:
            await query.edit_message_text(header, reply_markup=voices_list_keyboard(int(page), favs))
        return

    if data.startswith("vtog:"):
        _, origin, page, voice_id = data.split(":", 3)
        now_fav = db.toggle_favorite(user.id, voice_id)
        await query.answer("أُضيف للمفضلة 💙" if now_fav else "أُزيل من المفضلة")

        page = int(page)
        if origin == "fav":
            fav_ids = db.get_favorites(user.id)
            if page > 0 and page * config.VOICES_PER_PAGE >= len(fav_ids):
                page -= 1
            if not fav_ids:
                kb = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("رجوع للقائمة الرئيسية 🖲", callback_data="back_main")]]
                )
                await query.edit_message_text("لا توجد أصوات مفضّلة بعد 💙", reply_markup=kb)
                return
            await query.edit_message_reply_markup(reply_markup=favorites_list_keyboard(page, fav_ids))
        else:
            favs = set(db.get_favorites(user.id))
            await query.edit_message_reply_markup(reply_markup=voices_list_keyboard(page, favs))
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
    if not await is_subscribed(context.bot, user.id):
        await update.message.reply_text(force_sub_text(name), reply_markup=subscribe_keyboard())
        return

    voice_id = db.get_selected_voice(user.id)
    voice = get_voice(voice_id) if voice_id else None
    if not voice:
        await update.message.reply_text(
            "الرجاء اختيار صوت أولاً من قائمة الأصوات 🔊", reply_markup=main_menu_keyboard()
        )
        return

    now = time.time()
    last = _last_request_time.get(user.id, 0)
    if now - last < config.PER_USER_COOLDOWN_SECONDS:
        await update.message.reply_text("⏳ الرجاء الانتظار قليلاً قبل إرسال طلب جديد.")
        return
    _last_request_time[user.id] = now

    if len(text) == 0:
        return
    if len(text) > config.MAX_TEXT_LENGTH:
        await update.message.reply_text(
            f"⚠️ النص طويل جداً، الحد الأقصى المسموح به {config.MAX_TEXT_LENGTH} حرف."
        )
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
        await update.message.reply_text("❌ حدث خطأ أثناء تحويل النص إلى صوت، حاول مرة أخرى.")
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
    await application.bot.set_my_commands([BotCommand("start", "بدء استخدام البوت")])


def main():
    if not config.BOT_TOKEN:
        raise RuntimeError("متغيّر البيئة BOT_TOKEN غير موجود. أضفه من إعدادات Render.")

    db.init_db()

    threading.Thread(target=run_server, daemon=True).start()

    application = (
        ApplicationBuilder().token(config.BOT_TOKEN).post_init(post_init).build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(callback_router))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    logger.info("VONE bot is starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()



