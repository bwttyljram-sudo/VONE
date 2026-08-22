"""
قائمة الأصوات العربية المتوفرة مجاناً ضمن مكتبة Microsoft Edge TTS (عبر مكتبة edge-tts).
كل صوت له: المعرّف الرسمي (id) المستخدم فعلياً عند توليد الصوت، اسم عرض بالعربي، وايموجي حسب الجنس.

ملاحظة: هذه هي كل الأصوات العربية الرسمية المتوفرة حالياً في خدمة Edge TTS (32 صوت / 16 دولة).
🧔🏻 = صوت رجالي   |   👩🏻‍🦳 = صوت نسائي
"""

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
