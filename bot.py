"""
VONE — بوت تيليجرام لتحويل النص إلى صوت باستخدام أصوات عربية مجانية من Microsoft Edge TTS.

المميزات:
- اشتراك إجباري بالقناة قبل الاستخدام.
- قائمة أصوات مقسّمة على صفحات (10 لكل صفحة) مع أزرار "التالي/السابق".
- قائمة أصوات مفضّلة لكل مستخدم.
- تحويل النص المُرسل إلى ملف صوتي بالصوت المختار.
- أوامر أدمن: إحصائيات حيّة + إذاعة جماعية بطيئة وآمنة.
- حماية: حد تزامن لعمليات التحويل + تهدئة لكل مستخدم + حد لطول النص.
"""

import asyncio
import io
import logging
import threading
import time

import edge_tts
from telegram import Update, BotCommand
from telegram.constants import ChatMemberStatus, ChatAction
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

import config
import database as db
from voices import VOICES, get_voice
from keyboards import (
    main_menu_keyboard,
    subscribe_keyboard,
    voices_list_keyboard,
    favorites_list_keyboard,
    stats_keyboard,
)
from server import run_server

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger("vone_bot")

# يحدّ من عدد عمليات التحويل التي تُنفَّذ في نفس اللحظة على مستوى البوت كله
tts_semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_TTS)

# آخر وقت طلب لكل مستخدم (تهدئة ضد السبام)، مخزّن في الذاكرة فقط
_last_request_time = {}


# ------------------------------------------------------------------
# أدوات مساعدة
# ------------------------------------------------------------------

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
    from telegram.constants import ParseMode

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
            # "Message is not modified" يعني الأرقام لم تتغيّر منذ آخر تحديث — هذا طبيعي، لا نرسل رسالة جديدة
            if "not modified" not in str(e).lower():
                logger.warning("فشل تحديث رسالة الإحصائيات: %s", e)
        return
    await bot.send_message(
        chat_id=chat_id, text=text, reply_markup=stats_keyboard(), parse_mode=ParseMode.HTML
    )


# ------------------------------------------------------------------
# /start
# ------------------------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.upsert_user(user.id, user.username or "", user.language_code or "")

    name = user.first_name or "صديقنا"
    subscribed = await is_subscribed(context.bot, user.id)

    if not subscribed:
        await update.message.reply_text(force_sub_text(name), reply_markup=subscribe_keyboard())
        return

    await update.message.reply_text(welcome_text(name), reply_markup=main_menu_keyboard())


# ------------------------------------------------------------------
# استقبال ضغطات الأزرار
# ------------------------------------------------------------------

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    data = query.data or ""
    name = user.first_name or "صديقنا"

    db.touch_user(user.id)

    # ---- التحقق من الاشتراك ----
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

    # أي زر آخر غير "تحقق" يتطلب أن يكون المستخدم مشتركاً فعلاً (الأدمن مستثنى)
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
            from telegram import InlineKeyboardMarkup, InlineKeyboardButton

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
        # نبقي نفس القائمة ظاهرة، فقط نحدّث نص الرأس ليدل على الصوت المفعّل
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
            # لو صارت الصفحة فاضية بعد الحذف نرجع صفحة للخلف
            if page > 0 and page * config.VOICES_PER_PAGE >= len(fav_ids):
                page -= 1
            if not fav_ids:
                from telegram import InlineKeyboardMarkup, InlineKeyboardButton

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


# ------------------------------------------------------------------
# استقبال الرسائل النصية (نص التحويل + أوامر الأدمن النصية)
# ------------------------------------------------------------------

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (update.message.text or "").strip()
    db.upsert_user(user.id, user.username or "", user.language_code or "")
    db.touch_user(user.id)

    # ---- أوامر الأدمن النصية ----
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

    # ---- التحقق من الاشتراك للمستخدم العادي ----
    name = user.first_name or "صديقنا"
    if not await is_subscribed(context.bot, user.id):
        await update.message.reply_text(force_sub_text(name), reply_markup=subscribe_keyboard())
        return

    # ---- التحقق من اختيار صوت مسبقاً ----
    voice_id = db.get_selected_voice(user.id)
    voice = get_voice(voice_id) if voice_id else None
    if not voice:
        await update.message.reply_text(
            "الرجاء اختيار صوت أولاً من قائمة الأصوات 🔊", reply_markup=main_menu_keyboard()
        )
        return

    # ---- تهدئة ضد السبام ----
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


# ------------------------------------------------------------------
# الإذاعة الجماعية
# ------------------------------------------------------------------

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


# ------------------------------------------------------------------
# الإعداد والتشغيل
# ------------------------------------------------------------------

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
