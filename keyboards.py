"""
بناء لوحات المفاتيح (Inline Keyboards) لكل قوائم البوت.
تنسيق بيانات الأزرار (callback_data) موحّد وقصير حتى لا يتجاوز حد تيليجرام (64 بايت):

    vlist:{page}                      -> تصفح قائمة كل الأصوات
    vfav:{page}                       -> تصفح قائمة الأصوات المفضلة
    vsel:{origin}:{page}:{voice_id}   -> اختيار صوت معيّن كصوت نشط
    vtog:{origin}:{page}:{voice_id}   -> إضافة/حذف من المفضلة
    menu_voices / menu_fav / back_main
    check_sub
    admin_stats_refresh
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from voices import VOICES
from config import VOICES_PER_PAGE, CHANNEL_LINK


def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("قائمة الأصوات 🔊", callback_data="menu_voices")],
        [InlineKeyboardButton("الأصوات المُفضلة 💙", callback_data="menu_fav")],
    ])


def subscribe_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("أشترك في القناة 📺", url=CHANNEL_LINK)],
        [InlineKeyboardButton("تحقق 🔍", callback_data="check_sub")],
    ])


def _paginate(items, page):
    start = page * VOICES_PER_PAGE
    end = start + VOICES_PER_PAGE
    return items[start:end], len(items)


def voices_list_keyboard(page: int, favorites: set):
    """قائمة كل الأصوات مع زر تفعيل + زر تبديل المفضلة لكل صوت."""
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
    if (page + 1) * VOICES_PER_PAGE < total:
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
    if (page + 1) * VOICES_PER_PAGE < total:
        nav_row.append(InlineKeyboardButton("التالي »", callback_data=f"vfav:{page + 1}"))
    if nav_row:
        rows.append(nav_row)

    rows.append([InlineKeyboardButton("رجوع للقائمة الرئيسية 🖲", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


def stats_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("تحديث 🔄", callback_data="admin_stats_refresh")],
    ])
