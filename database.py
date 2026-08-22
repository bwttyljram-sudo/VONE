"""
طبقة قاعدة البيانات — SQLite بسيطة وخفيفة، مناسبة لاستضافة مجانية بمعالج ضعيف.
تخزّن: المستخدمين، الأصوات المفضلة، سجلّ الطلبات (لإحصائيات النجاح/النشاط).
"""

import sqlite3
import time
import threading
from contextlib import contextmanager

from config import DB_PATH, ACTIVE_NOW_WINDOW_MINUTES

_lock = threading.Lock()


def _connect():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


@contextmanager
def get_conn():
    with _lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init_db():
    with get_conn() as conn:
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


# ------------------ المستخدمون ------------------

def upsert_user(user_id: int, username: str, language_code: str):
    now = time.time()
    with get_conn() as conn:
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
    return not exists  # True لو هذا مستخدم جديد


def touch_user(user_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE users SET last_active=? WHERE user_id=?", (time.time(), user_id))


def set_selected_voice(user_id: int, voice_id: str):
    with get_conn() as conn:
        conn.execute("UPDATE users SET selected_voice=? WHERE user_id=?", (voice_id, user_id))


def get_selected_voice(user_id: int):
    with get_conn() as conn:
        cur = conn.execute("SELECT selected_voice FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        return row[0] if row else None


def set_subscribed(user_id: int, subscribed: bool):
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET is_subscribed=? WHERE user_id=?", (1 if subscribed else 0, user_id)
        )


def get_all_user_ids():
    with get_conn() as conn:
        cur = conn.execute("SELECT user_id FROM users")
        return [r[0] for r in cur.fetchall()]


def remove_user(user_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM users WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM favorites WHERE user_id=?", (user_id,))


# ------------------ المفضلة ------------------

def toggle_favorite(user_id: int, voice_id: str) -> bool:
    """يضيف الصوت للمفضلة إن لم يكن موجوداً، أو يحذفه إن كان موجوداً. يرجع True لو أصبح مفضّلاً."""
    with get_conn() as conn:
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


def get_favorites(user_id: int):
    with get_conn() as conn:
        cur = conn.execute("SELECT voice_id FROM favorites WHERE user_id=?", (user_id,))
        return [r[0] for r in cur.fetchall()]


def is_favorite(user_id: int, voice_id: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT 1 FROM favorites WHERE user_id=? AND voice_id=?", (user_id, voice_id)
        )
        return cur.fetchone() is not None


# ------------------ سجل الطلبات والإحصائيات ------------------

def log_request(user_id: int, success: bool):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO requests (user_id, created_at, success) VALUES (?, ?, ?)",
            (user_id, time.time(), 1 if success else 0),
        )


def get_stats():
    now = time.time()
    day = 86400
    with get_conn() as conn:
        total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

        active_now = conn.execute(
            "SELECT COUNT(*) FROM users WHERE last_active >= ?",
            (now - ACTIVE_NOW_WINDOW_MINUTES * 60,),
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

        # أقرب تقريب متاح لـ"الدولة" هو لغة تطبيق تيليجرام لدى المستخدم (Telegram لا يوفر دولة حقيقية)
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
