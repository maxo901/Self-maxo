# -*- coding: utf-8 -*-
"""
MAXO SELF - سلف اکانت یکپارچه تلگرام (Telethon)
همه چیز از طریق پیام‌های ذخیره‌شده (Saved Messages) و داخل PV کنترل می‌شود.
برای تست روی Termux آماده است.
"""

import os
import re
import time
import html as pyhtml
import random
import pickle
import base64
import asyncio
import sqlite3
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from telethon import TelegramClient, events, utils as tl_utils
from telethon.extensions import html as tl_html
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
from telethon.tl.functions.contacts import BlockRequest, UnblockRequest
from telethon.tl.functions.account import UpdateProfileRequest
from telethon.tl.types import (
    MessageEntityBold, MessageEntityBlockquote, MessageEntityCode,
    MessageEntityUrl, MessageEntityMention, MessageEntityHashtag,
    MessageEntityCashtag, MessageEntityBotCommand, MessageEntityEmail,
    MessageEntityPhone,
)


# ============================================================
# CONFIG
# ============================================================
# روی ترموکس: همین مقادیر پیش‌فرض کار می‌کنن، لازم نیست چیزی ست کنی.
# روی Railway: این سه‌تا رو به عنوان Environment Variable بذار:
#   API_ID, API_HASH, SESSION_STRING
# (SESSION_STRING رو با اسکریپت generate_session.py می‌سازی — پایین توضیح داده شده)

API_ID = int(os.environ.get("API_ID", "66324"))
API_HASH = os.environ.get("API_HASH", "a52446ae0a1a02f005d")
SESSION_STRING = os.environ.get("SESSION_STRING", "").strip()

APP_DIR = os.path.dirname(os.path.abspath(__file__))

# روی Railway این رو به مسیر ولوم (مثلاً /data) ست کن تا دیتابیس و کش
# رسانه‌ها بین دیپلوی‌ها و ری‌استارت‌ها از بین نره. روی ترموکس نیازی نیست
# و خودش کنار همین فایل ذخیره می‌کنه.
DATA_DIR = os.environ.get("DATA_DIR", APP_DIR)
Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

SESSION = os.path.join(DATA_DIR, "MAXO")   # فقط وقتی SESSION_STRING خالی باشه استفاده می‌شه
DB_FILE = os.path.join(DATA_DIR, "MAXO.db")
MEDIA_DIR = Path(DATA_DIR) / "maxo_media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

TEHRAN = ZoneInfo("Asia/Tehran")

MAX_NAME_LEN = 64            # حد امن اسم
MAX_BIO_LEN = 70             # حد امن بیو (اکانت عادی تلگرام)
CLOCK_LABEL = "MAXO"         # برچسب پیش‌فرض جلوی ساعت در اسم

MAX_MEDIA_MB = 50            # حداکثر حجم رسانه‌ای که کش می‌شود
ARCHIVE_TTL_HOURS = 24       # بعد این مدت، کش پیام‌های حذف‌نشده پاک می‌شود

AUTO_REPLY_COOLDOWN = 600    # پاسخ خودکار حداکثر هر ۱۰ دقیقه یکبار برای هر نفر
AUTO_REPLY_MAX_DELAY = 20    # حداکثر تاخیر قبل از ارسال پاسخ خودکار (ثانیه)


# ============================================================
# DATABASE
# ============================================================

db = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA busy_timeout=30000")

db.execute("""
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS blocked (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    name TEXT,
    created_at TEXT
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS chats (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    name TEXT,
    received INTEGER DEFAULT 0,
    replies INTEGER DEFAULT 0,
    last_message TEXT,
    last_seen TEXT,
    last_auto_reply_at INTEGER DEFAULT 0
)
""")

# مهاجرت امن برای دیتابیس‌های قدیمی‌تر که این ستون را ندارند
try:
    db.execute("ALTER TABLE chats ADD COLUMN last_auto_reply_at INTEGER DEFAULT 0")
    db.commit()
except sqlite3.OperationalError:
    pass

db.execute("""
CREATE TABLE IF NOT EXISTS muted (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    name TEXT,
    created_at TEXT
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS deleted_archive (
    msg_id INTEGER PRIMARY KEY,
    chat_id INTEGER,
    sender_id INTEGER,
    name TEXT,
    username TEXT,
    text TEXT,
    media_type TEXT,
    media_path TEXT,
    created_at INTEGER
)
""")
db.commit()

# اگه از نسخه‌ی قبلی یه جدول deleted_archive با ستون‌های قدیمی مونده،
# خودش می‌سازدش دوباره (این جدول فقط کش موقتِ ۲۴ ساعته‌ست، جای نگرانی نیست).
_required_archive_cols = {
    "msg_id", "chat_id", "sender_id", "name", "username",
    "text", "media_type", "media_path", "created_at"
}
_existing_archive_cols = {
    row[1] for row in db.execute("PRAGMA table_info(deleted_archive)").fetchall()
}
if not _required_archive_cols.issubset(_existing_archive_cols):
    db.execute("DROP TABLE IF EXISTS deleted_archive")
    db.execute("""
    CREATE TABLE deleted_archive (
        msg_id INTEGER PRIMARY KEY,
        chat_id INTEGER,
        sender_id INTEGER,
        name TEXT,
        username TEXT,
        text TEXT,
        media_type TEXT,
        media_path TEXT,
        created_at INTEGER
    )
    """)
    db.commit()


def get_setting(key, default=""):
    cur = db.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    return row[0] if row else default


def set_setting(key, value):
    db.execute(
        "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
        (key, str(value))
    )
    db.commit()


DEFAULT_SETTINGS = {
    "auto_reply": "0",
    "auto_text": "سلام، فعلاً آفلاین هستم. بعداً پیام شما را می‌بینم.",
    "auto_style": "as_typed",
    "waiting_for_text": "0",
    "name_clock": "0",
    "name_font": "1",
    "name_label": CLOCK_LABEL,
    "name_original": "",
    "bio_clock": "0",
    "bio_font": "1",
    "bio_original": "",
    "archive_enabled": "1",
    "chat_font": "0",
}

for _key, _value in DEFAULT_SETTINGS.items():
    if get_setting(_key, "") == "":
        set_setting(_key, _value)


def get_last_auto_reply(user_id):
    row = db.execute(
        "SELECT last_auto_reply_at FROM chats WHERE user_id=?",
        (int(user_id),)
    ).fetchone()
    return row[0] if row and row[0] else 0


def set_last_auto_reply(user_id, ts):
    db.execute(
        "UPDATE chats SET last_auto_reply_at=? WHERE user_id=?",
        (int(ts), int(user_id))
    )
    db.commit()


def set_auto_entities(entities):
    try:
        blob = pickle.dumps(entities or [])
        encoded = base64.b64encode(blob).decode("ascii")
    except Exception as e:
        print("ENTITIES SAVE ERROR:", e)
        encoded = ""
    set_setting("auto_text_entities", encoded)


def load_auto_entities():
    raw = get_setting("auto_text_entities", "")
    if not raw:
        return []
    try:
        return pickle.loads(base64.b64decode(raw))
    except Exception as e:
        print("ENTITIES LOAD ERROR:", e)
        return []


# ============================================================
# TELEGRAM CLIENT
# ============================================================

# اگه SESSION_STRING تنظیم شده باشه (حالت Railway) از همون استفاده می‌کنه
# وگرنه از فایل سشن محلی (حالت Termux) استفاده می‌کنه.
if SESSION_STRING:
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
else:
    client = TelegramClient(SESSION, API_ID, API_HASH)

ME_ID = None
suppress_delete_ids = set()


# ============================================================
# فونت‌های یونیکد برای اسم / بیو (۴ فونت، هم حروف هم عدد)
# ============================================================
# فونت ۱ = Sans-Serif Bold   ->  𝗠𝗔𝗫𝗢 | 𝟯:𝟮𝟬
# فونت ۲ = Bold (سریف)       ->  𝐌𝐀𝐗𝐎 | 𝟑:𝟐𝟎
# فونت ۳ = Monospace         ->  𝙼𝙰𝚇𝙾 | 𝟹:𝟸𝟶
# فونت ۴ = Sans-Serif ساده   ->  𝖬𝖠𝖷𝖮 | 𝟦:𝟤𝟢

def _build_font_map(upper_start, lower_start, digit_start):
    mapping = {}
    for i in range(26):
        mapping[chr(ord("A") + i)] = chr(upper_start + i)
        mapping[chr(ord("a") + i)] = chr(lower_start + i)
    for i in range(10):
        mapping[chr(ord("0") + i)] = chr(digit_start + i)
    return mapping


FONT_MAPS = {
    1: _build_font_map(0x1D5D4, 0x1D5EE, 0x1D7EC),   # Sans-Serif Bold
    2: _build_font_map(0x1D400, 0x1D41A, 0x1D7CE),   # Bold
    3: _build_font_map(0x1D670, 0x1D68A, 0x1D7F6),   # Monospace
    4: _build_font_map(0x1D5A0, 0x1D5BA, 0x1D7E2),   # Sans-Serif
}


def apply_font(text, font):
    mapping = FONT_MAPS.get(int(font), FONT_MAPS[1])
    return "".join(mapping.get(ch, ch) for ch in text)


def clock_12h(now_dt):
    hour = now_dt.hour % 12
    if hour == 0:
        hour = 12
    return f"{hour}:{now_dt.minute:02d}"


def build_name_text(font):
    now_dt = datetime.now(TEHRAN)
    label = get_setting("name_label", CLOCK_LABEL) or CLOCK_LABEL
    raw = f"{label} | {clock_12h(now_dt)}"
    styled = apply_font(raw, font)
    return styled[:MAX_NAME_LEN]


def build_bio_text(font):
    now_dt = datetime.now(TEHRAN)
    original = get_setting("bio_original", "")
    raw_line = f"TIME : {clock_12h(now_dt)}"
    styled_line = apply_font(raw_line, font)

    if original:
        full = f"{original}\n{styled_line}"
    else:
        full = styled_line

    if len(full) > MAX_BIO_LEN:
        keep = MAX_BIO_LEN - len(styled_line) - 1
        if keep > 0:
            full = f"{original[:keep]}\n{styled_line}"
        else:
            full = styled_line[:MAX_BIO_LEN]

    return full


# ============================================================
# پیام پاسخ خودکار: دقیقاً با همون فرمتی که تایپ شده، یا استایل انتخابی
# ============================================================

def build_bold_entity(text):
    length = len(tl_utils.add_surrogate(text))
    if length <= 0:
        return []
    return [MessageEntityBold(offset=0, length=length)]


def build_quote_entity(text):
    length = len(tl_utils.add_surrogate(text))
    if length <= 0:
        return []
    try:
        return [MessageEntityBlockquote(offset=0, length=length, collapsed=False)]
    except TypeError:
        return [MessageEntityBlockquote(offset=0, length=length)]


def get_auto_reply_payload():
    raw_text = get_setting("auto_text", "")
    style = get_setting("auto_style", "as_typed")

    if not raw_text:
        return "", []

    if style == "bold":
        return raw_text, build_bold_entity(raw_text)
    elif style == "quote":
        return raw_text, build_quote_entity(raw_text)
    else:
        return raw_text, load_auto_entities()


# ============================================================
# HELP MENU
# ============================================================

HELP = """
<b>⚡ MAXO SELF</b>
<blockquote>مدیریت کامل از طریق پیام‌های ذخیره‌شده</blockquote>

━━━━━━━━━━━━━━━━━━

<b>🎛 پاسخ خودکار</b>
<code>روشن</code> / <code>خاموش</code>
<code>متن</code> — نمایش متن و استایل فعلی
<code>تغییر متن</code> — پیام بعدی رو با هر فرمتی بفرستی (بولد/ایتالیک/نقل‌قول/...) دقیقاً همونجوری ذخیره می‌شه
<code>لغو</code> — لغو حالت «تغییر متن»
<code>استایل ساده</code> — دقیقاً مثل نوشتنت
<code>استایل بولد</code> — کل متن بولد فرستاده شود
<code>استایل نقل قول</code> — کل متن به شکل نقل‌قول فرستاده شود
<i>حداکثر هر ۱۰ دقیقه یکبار برای هر نفر، با تاخیر تصادفی تا ۲۰ ثانیه</i>

━━━━━━━━━━━━━━━━━━

<b>🕒 ساعت در اسم</b> (بر اساس ساعت ایران، هر دقیقه دقیق آپدیت می‌شود)
<code>اسم 1</code> تا <code>اسم 4</code> — فعال با فونت شماره ۱ تا ۴
<code>اسم MAXO</code> — تغییر برچسب اسم (هر متنی بخوای)
<code>حذف اسم</code> — خاموش و بازگشت به اسم قبلی

<b>🕒 ساعت در بیو</b>
<code>بیو 1</code> تا <code>بیو 4</code> — فعال با فونت انتخابی
(بیو قبلی حفظ می‌شود و زیرش زمان اضافه می‌شود)
<code>حذف بیو</code> — خاموش و بازگشت به بیوی قبلی

━━━━━━━━━━━━━━━━━━

<b>🔇 سکوت</b>
<blockquote>این دو دستور را داخل PV همون شخص بزن</blockquote>
<code>سکوت</code> — پیام‌های بعدی این شخص خودکار برای هردو پاک می‌شود
<code>حذف سکوت</code> — لغو سکوت
<code>سکوت‌ها</code> — لیست افراد ساکت‌شده

━━━━━━━━━━━━━━━━━━

<b>🚫 بلاک</b>
<code>بلاک @username</code> یا <code>بلاک 123456</code>
<code>انبلاک @username</code>
<code>بلاک‌ها</code> — لیست بلاک‌شده‌ها
<i>داخل PV هم فقط با تایپ «بلاک» همون شخص بلاک می‌شود</i>

━━━━━━━━━━━━━━━━━━

<b>🗑 حذف پیام</b>
<code>حذف 30</code> — داخل همون PV بزن، ۳۰ پیام آخر پاک می‌شود
<code>حذف</code> — روی هر پیامی (چه مال من چه مال طرف) ریپلای کن، همون یکی سریع پاک می‌شود

━━━━━━━━━━━━━━━━━━

<b>📦 آرشیو پیام‌های حذف‌شده</b>
هر پیام PV آدم‌ها (متن، عکس، فیلم، ویس، حتی عکس/فیلم زمان‌دار) کش می‌شود (پیام ربات‌ها نه)؛
اگر طرف مقابل پاکش کند، خودش با فرستنده و زمان برات میاد اینجا.
<code>آرشیو روشن</code> / <code>آرشیو خاموش</code>

━━━━━━━━━━━━━━━━━━

<b>🔤 فونت چت</b>
<blockquote>هر پیامی که بفرستی (PV، گروه، کانال) همون لحظه با فونت انتخابی ویرایش می‌شه</blockquote>
<code>فونت چت</code> — منو و حالت فعلی
<code>فونت چت 1</code> — بولد
<code>فونت چت 2</code> — کشیده (سـ‍ـلام)
<code>فونت چت 3</code> — تکی (مونواسپیس)
<code>فونت چت 4</code> — نقل قول
<code>فونت چت 5</code> — نقل قول بولد
<code>فونت چت 6</code> — کشیده فونت‌دار (کشیده + بولد + حروف انگلیسی فانتزی)
<code>فونت چت خاموش</code> — غیرفعال
<i>پیامی که خودت دستی فرمت زده باشی (بولد، لینک متنی...) و دستورها دست‌نخورده می‌مونن. فونت کشیده روی پیام‌های دارای لینک/منشن اعمال نمی‌شه.</i>

━━━━━━━━━━━━━━━━━━

<b>💥 اسپم (گروه / کانال)</b>
<code>اسپم سلام 10</code> — ۱۰ بار «سلام» می‌فرسته (حداکثر ۱۵۰)
روی یه پیام ریپلای کنی، اسپم‌ها هم ریپلای می‌شن.
<code>توقف اسپم</code> — وسط کار متوقف می‌کنه
<i>اگه فونت چت روشن باشه، اسپم‌ها هم با همون فونت فرستاده می‌شن</i>

━━━━━━━━━━━━━━━━━━

<b>🗂 چت فایل</b>
<blockquote>داخل PV هر شخص بزن</blockquote>
<code>چت فایل</code> (یا <code>-چت فایل-</code>) — کل چت رو با تاریخ شمسی/میلادی، ساعت دقیق، آیدی، اسم، ریپلای، فوروارد، ویرایش و ... به‌صورت فایل HTML دارک شبیه تلگرام می‌سازه و تو Saved Messages می‌فرسته.
داخل فایل: انتخاب فونت (کلفت/نستعلیق/کوفی...)، سوییچ دارک/روشن، جستجو، و با تپ روی هر پیام جزئیاتش (ساعت با ثانیه، آیدی پیام و فرستنده).

━━━━━━━━━━━━━━━━━━

<b>👤 اطلاعات</b>
<code>وضعیت</code> — وضعیت کلی ربات
<code>اکانت</code> — اطلاعات اکانت
<code>دستورات</code> — همین راهنما

━━━━━━━━━━━━━━━━━━
"""


async def reply_help(event):
    """راهنما از حد ۴۰۹۶ کاراکتر تلگرام بیشتره؛ تکه‌تکه می‌فرستیم."""
    sep = "━━━━━━━━━━━━━━━━━━"
    sections = [s.strip() for s in HELP.split(sep) if s.strip()]
    chunks, cur = [], ""
    for sec in sections:
        piece = sec + "\n\n" + sep + "\n\n"
        if cur and len(cur) + len(piece) > 3000:
            chunks.append(cur)
            cur = ""
        cur += piece
    if cur:
        chunks.append(cur)
    for i, ch in enumerate(chunks):
        if i == 0:
            await event.reply(ch, parse_mode="html")
        else:
            await client.send_message("me", ch, parse_mode="html")


# ============================================================
# UTILS
# ============================================================

def now():
    return datetime.now(TEHRAN).strftime("%Y-%m-%d %H:%M:%S")


def normalize_number(text):
    return text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))


def user_name(user):
    first = getattr(user, "first_name", "") or ""
    last = getattr(user, "last_name", "") or ""
    name = f"{first} {last}".strip()
    return name or "بدون نام"


def user_username(user):
    username = getattr(user, "username", None)
    return f"@{username}" if username else "ندارد"


async def resolve_user(value):
    value = value.strip()
    if value.startswith("@"):
        value = value[1:]
    try:
        return await client.get_entity(value)
    except Exception as e:
        print("RESOLVE ERROR:", e)
        return None


def save_chat(user, message_text=""):
    try:
        db.execute("""
        INSERT OR IGNORE INTO chats
        (user_id, username, name, received, replies, last_message, last_seen, last_auto_reply_at)
        VALUES (?, ?, ?, 0, 0, '', ?, 0)
        """, (user.id, getattr(user, "username", None), user_name(user), now()))

        db.execute("""
        UPDATE chats
        SET username=?, name=?, last_message=?, last_seen=?, received=received+1
        WHERE user_id=?
        """, (
            getattr(user, "username", None),
            user_name(user),
            message_text[:500],
            now(),
            user.id
        ))
        db.commit()
    except Exception as e:
        print("DB CHAT ERROR:", e)


# ============================================================
# BLOCK / UNBLOCK
# ============================================================

async def block_user(user):
    if not user:
        return "❌ <b>کاربر پیدا نشد.</b>"
    try:
        input_user = await client.get_input_entity(user)
        await client(BlockRequest(id=input_user))

        db.execute("""
        INSERT OR REPLACE INTO blocked (user_id, username, name, created_at)
        VALUES (?, ?, ?, ?)
        """, (user.id, getattr(user, "username", None), user_name(user), now()))
        db.commit()

        return (
            "🚫 <b>کاربر بلاک شد.</b>\n\n"
            f"<b>نام:</b> {user_name(user)}\n"
            f"<b>ID:</b> <code>{user.id}</code>\n"
            f"<b>Username:</b> <code>{user_username(user)}</code>"
        )
    except Exception as e:
        return f"❌ <b>بلاک نشد.</b>\n\n<code>{str(e)[:700]}</code>"


async def unblock_user(user):
    if not user:
        return "❌ <b>کاربر پیدا نشد.</b>"
    try:
        input_user = await client.get_input_entity(user)
        await client(UnblockRequest(id=input_user))

        db.execute("DELETE FROM blocked WHERE user_id=?", (user.id,))
        db.commit()

        return f"🟢 <b>کاربر آنبلاک شد.</b>\n\n<b>{user_name(user)}</b>"
    except Exception as e:
        return f"❌ <b>آنبلاک نشد.</b>\n\n<code>{str(e)[:700]}</code>"


def block_list():
    cur = db.execute("""
    SELECT user_id, username, name, created_at FROM blocked
    ORDER BY created_at DESC
    """)
    rows = cur.fetchall()

    if not rows:
        return "🚫 <b>لیست بلاک خالی است.</b>"

    text = "🚫 <b>کاربران بلاک‌شده</b>\n\n━━━━━━━━━━━━━━━━━━\n\n"
    for i, row in enumerate(rows, 1):
        uid, username, name, _ = row
        text += (
            f"<b>{i}. {name}</b>\n"
            f"ID: <code>{uid}</code>\n"
            f"Username: <code>@{username or 'ندارد'}</code>\n\n"
        )
    text += f"━━━━━━━━━━━━━━━━━━\n<b>تعداد:</b> {len(rows)}"
    return text


# ============================================================
# MUTE (سکوت)
# ============================================================

def is_muted(user_id):
    row = db.execute("SELECT 1 FROM muted WHERE user_id=?", (int(user_id),)).fetchone()
    return row is not None


def add_mute(user):
    db.execute("""
    INSERT OR REPLACE INTO muted (user_id, username, name, created_at)
    VALUES (?, ?, ?, ?)
    """, (user.id, getattr(user, "username", None), user_name(user), now()))
    db.commit()


def remove_mute(user_id):
    db.execute("DELETE FROM muted WHERE user_id=?", (int(user_id),))
    db.commit()


def mute_list():
    cur = db.execute("""
    SELECT user_id, username, name, created_at FROM muted
    ORDER BY created_at DESC
    """)
    rows = cur.fetchall()

    if not rows:
        return "🔇 <b>لیست سکوت خالی است.</b>"

    text = "🔇 <b>افراد ساکت‌شده</b>\n\n━━━━━━━━━━━━━━━━━━\n\n"
    for i, row in enumerate(rows, 1):
        uid, username, name, _ = row
        text += (
            f"<b>{i}. {name}</b>\n"
            f"ID: <code>{uid}</code>\n"
            f"Username: <code>@{username or 'ندارد'}</code>\n\n"
        )
    text += f"━━━━━━━━━━━━━━━━━━\n<b>تعداد:</b> {len(rows)}"
    return text


# ============================================================
# DELETE MESSAGES IN PV
# ============================================================

async def delete_private_messages(event, count):
    if count <= 0:
        return
    if count > 1000:
        count = 1000

    try:
        message_ids = []
        async for message in client.iter_messages(entity=event.chat_id, limit=count):
            message_ids.append(message.id)

        if not message_ids:
            return

        for mid in message_ids:
            suppress_delete_ids.add(int(mid))

        await client.delete_messages(entity=event.chat_id, message_ids=message_ids, revoke=True)
    except Exception as e:
        print("DELETE ERROR:", type(e).__name__, str(e))


async def delete_replied_message(event):
    """پاک کردن سریع فقط همون پیامی که روش ریپلای شده (مال خودم یا طرف مقابل)."""
    try:
        reply_id = event.message.reply_to_msg_id
        if not reply_id:
            return False

        ids = [int(reply_id), int(event.message.id)]
        for mid in ids:
            suppress_delete_ids.add(mid)

        await client.delete_messages(entity=event.chat_id, message_ids=ids, revoke=True)
        return True
    except Exception as e:
        print("REPLY DELETE ERROR:", e)
        return False


# ============================================================
# آرشیو پیام‌های PV (متن + عکس/فیلم/ویس، حتی زمان‌دار)
# ============================================================

async def cache_incoming_message(event):
    if get_setting("archive_enabled", "1") != "1":
        return

    try:
        message = event.message
        sender = await event.get_sender()

        # فقط پیام آدم‌ها آرشیو می‌شه، ربات‌ها نه
        if sender is None or getattr(sender, "bot", False):
            return

        media_type = ""
        media_path = ""

        if message.media:
            try:
                size = getattr(message.file, "size", None)

                if size and size > MAX_MEDIA_MB * 1024 * 1024:
                    media_type = "large_skipped"
                else:
                    chat_dir = MEDIA_DIR / str(event.chat_id)
                    chat_dir.mkdir(parents=True, exist_ok=True)

                    path = await client.download_media(
                        message,
                        file=str(chat_dir / f"{message.id}")
                    )

                    if path:
                        media_path = str(path)

                    if message.photo:
                        media_type = "photo"
                    elif message.video_note:
                        media_type = "round_video"
                    elif message.video:
                        media_type = "video"
                    elif message.voice:
                        media_type = "voice"
                    elif message.gif:
                        media_type = "gif"
                    elif message.sticker:
                        media_type = "sticker"
                    elif message.document:
                        media_type = "document"
                    else:
                        media_type = "media"

            except Exception as e:
                media_type = "media_error:" + type(e).__name__

        db.execute("""
        INSERT OR REPLACE INTO deleted_archive
        (msg_id, chat_id, sender_id, name, username, text, media_type, media_path, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            int(message.id),
            int(event.chat_id),
            getattr(sender, "id", 0) or 0,
            user_name(sender) if sender else "نامشخص",
            (f"@{getattr(sender, 'username', None)}" if sender and getattr(sender, "username", None) else ""),
            message.message or "",
            media_type,
            media_path,
            int(time.time())
        ))
        db.commit()
    except Exception as e:
        print("ARCHIVE CACHE ERROR:", e)


async def send_deleted_archive(row):
    try:
        (msg_id, chat_id, sender_id, name, username, text,
         media_type, media_path, created_at) = row

        time_text = datetime.fromtimestamp(created_at, TEHRAN).strftime("%Y-%m-%d %H:%M:%S")

        caption = (
            "🗑 <b>پیام حذف‌شده</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"<b>فرستنده:</b> {name}\n"
        )
        if username:
            caption += f"<b>یوزرنیم:</b> {username}\n"
        caption += f"<b>ID:</b> <code>{sender_id}</code>\n<b>زمان:</b> {time_text}\n"

        if text:
            caption += f"\n<b>متن:</b>\n{text}"

        if media_path and Path(media_path).exists():
            try:
                await client.send_file("me", media_path, caption=caption[:1024], parse_mode="html")
                try:
                    Path(media_path).unlink()
                except Exception:
                    pass
                return
            except Exception as e:
                print("SEND MEDIA ARCHIVE ERROR:", e)

        if media_type == "large_skipped":
            caption += "\n\n<i>حجم رسانه بیشتر از حد مجاز بود و دانلود نشد.</i>"
        elif media_type and not media_path:
            caption += "\n\n<i>این پیام رسانه داشت ولی دانلودش موفق نبود.</i>"

        await client.send_message("me", caption[:4000], parse_mode="html")
    except Exception as e:
        print("SEND ARCHIVE ERROR:", e)


async def archive_cleanup_loop():
    """پیام‌های کش‌شده‌ای که حذف نشدن رو بعد از مدتی از دیسک و دیتابیس پاک می‌کنه."""
    while True:
        try:
            cutoff = int(time.time()) - (ARCHIVE_TTL_HOURS * 3600)
            rows = db.execute(
                "SELECT msg_id, media_path FROM deleted_archive WHERE created_at < ?",
                (cutoff,)
            ).fetchall()

            for msg_id, media_path in rows:
                if media_path:
                    try:
                        p = Path(media_path)
                        if p.exists():
                            p.unlink()
                    except Exception:
                        pass

            db.execute("DELETE FROM deleted_archive WHERE created_at < ?", (cutoff,))
            db.commit()
        except Exception as e:
            print("ARCHIVE CLEANUP ERROR:", e)

        await asyncio.sleep(3600)


# ============================================================
# پاسخ خودکار با محدودیت (هر ۱۰ دقیقه یکبار برای هر نفر)
# ============================================================

async def send_delayed_autoreply(event, sender_id):
    try:
        await asyncio.sleep(random.uniform(1, AUTO_REPLY_MAX_DELAY))

        # اگر تو همین فاصله یک پاسخ دیگه ارسال شده، دوباره نفرست
        last = get_last_auto_reply(sender_id)
        if time.time() - last < AUTO_REPLY_COOLDOWN:
            return

        raw_text, entities = get_auto_reply_payload()
        if not raw_text:
            return

        hold_style(event.chat_id, 8)
        await event.reply(raw_text, formatting_entities=entities)
        set_last_auto_reply(sender_id, int(time.time()))

        db.execute("UPDATE chats SET replies=replies+1 WHERE user_id=?", (sender_id,))
        db.commit()
    except Exception as e:
        print("AUTO REPLY ERROR:", e)


# ============================================================
# STATUS / ACCOUNT
# ============================================================

async def status():
    auto = get_setting("auto_reply", "0") == "1"
    name_clock = get_setting("name_clock", "0") == "1"
    bio_clock = get_setting("bio_clock", "0") == "1"
    archive = get_setting("archive_enabled", "1") == "1"
    style = get_setting("auto_style", "as_typed")
    style_fa = {"as_typed": "ساده (عیناً)", "bold": "بولد", "quote": "نقل‌قول"}.get(style, style)

    blocked = db.execute("SELECT COUNT(*) FROM blocked").fetchone()[0]
    muted = db.execute("SELECT COUNT(*) FROM muted").fetchone()[0]
    chats = db.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
    replies = db.execute("SELECT COALESCE(SUM(replies),0) FROM chats").fetchone()[0]
    cached = db.execute("SELECT COUNT(*) FROM deleted_archive").fetchone()[0]

    return (
        "⚡ <b>وضعیت MAXO</b>\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "<b>سلف:</b> 🟢 فعال\n"
        f"<b>پاسخ خودکار:</b> {'🟢 روشن' if auto else '🔴 خاموش'} (استایل: {style_fa})\n"
        f"<b>ساعت در اسم:</b> {'🟢 روشن' if name_clock else '🔴 خاموش'}\n"
        f"<b>ساعت در بیو:</b> {'🟢 روشن' if bio_clock else '🔴 خاموش'}\n"
        f"<b>آرشیو حذف‌شده‌ها:</b> {'🟢 روشن' if archive else '🔴 خاموش'}\n"
        f"<b>فونت چت:</b> {CHAT_FONT_MODES.get(get_setting('chat_font', '0'), '🔴 خاموش')}\n\n"
        f"<b>پاسخ‌های ارسال‌شده:</b> {replies}\n"
        f"<b>PVهای ثبت‌شده:</b> {chats}\n"
        f"<b>کاربران بلاک:</b> {blocked}\n"
        f"<b>کاربران ساکت:</b> {muted}\n"
        f"<b>پیام در کش آرشیو:</b> {cached}\n\n"
        "━━━━━━━━━━━━━━━━━━"
    )


async def account_info():
    me = await client.get_me()
    blocked = db.execute("SELECT COUNT(*) FROM blocked").fetchone()[0]
    unanswered = db.execute("SELECT COUNT(*) FROM chats WHERE received > replies").fetchone()[0]

    return (
        "👤 <b>اطلاعات اکانت</b>\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>نام:</b> {user_name(me)}\n"
        f"<b>Username:</b> <code>{user_username(me)}</code>\n"
        f"<b>ID:</b> <code>{me.id}</code>\n"
        f"<b>شماره:</b> <code>{me.phone or 'مخفی'}</code>\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>کاربران بلاک:</b> {blocked}\n"
        f"<b>PV بدون پاسخ:</b> {unanswered}\n"
    )


# ============================================================
# ساعت در اسم / بیو - فعال‌سازی، خاموش‌سازی، حلقه بروزرسانی
# ============================================================

async def enable_name_clock(font=None, label=None):
    try:
        me = await client.get_me()
        if get_setting("name_original", "") == "":
            set_setting("name_original", me.first_name or "")

        if font is not None:
            set_setting("name_font", str(font))
        if label is not None:
            set_setting("name_label", label[:20])

        set_setting("name_clock", "1")
        chosen_font = int(get_setting("name_font", "1"))
        await client(UpdateProfileRequest(first_name=build_name_text(chosen_font)))
        return True
    except Exception as e:
        print("NAME CLOCK ENABLE ERROR:", e)
        return False


async def disable_name_clock():
    set_setting("name_clock", "0")
    original = get_setting("name_original", "")
    try:
        await client(UpdateProfileRequest(first_name=original))
    except Exception as e:
        print("NAME CLOCK DISABLE ERROR:", e)
    set_setting("name_original", "")


async def enable_bio_clock(font):
    try:
        from telethon.tl.functions.users import GetFullUserRequest
        full = await client(GetFullUserRequest("me"))
        current_bio = getattr(full.full_user, "about", "") or ""

        if get_setting("bio_original", "") == "" and get_setting("bio_clock", "0") == "0":
            set_setting("bio_original", current_bio)

        set_setting("bio_font", str(font))
        set_setting("bio_clock", "1")

        await client(UpdateProfileRequest(about=build_bio_text(font)))
        return True
    except Exception as e:
        print("BIO CLOCK ENABLE ERROR:", e)
        return False


async def disable_bio_clock():
    set_setting("bio_clock", "0")
    original = get_setting("bio_original", "")
    try:
        await client(UpdateProfileRequest(about=original))
    except Exception as e:
        print("BIO CLOCK DISABLE ERROR:", e)
    set_setting("bio_original", "")


async def clock_loop():
    """
    هر دقیقه، دقیقاً سر دقیقه (بر اساس ساعت ایران) آپدیت می‌کنه تا دیلی نداشته باشه.
    """
    while True:
        wait_extra = 0

        if get_setting("name_clock", "0") == "1":
            font = int(get_setting("name_font", "1"))
            try:
                await client(UpdateProfileRequest(first_name=build_name_text(font)))
            except FloodWaitError as e:
                wait_extra = max(wait_extra, e.seconds + 1)
            except Exception as e:
                print("NAME CLOCK LOOP ERROR:", e)

        if get_setting("bio_clock", "0") == "1":
            font = int(get_setting("bio_font", "1"))
            try:
                await client(UpdateProfileRequest(about=build_bio_text(font)))
            except FloodWaitError as e:
                wait_extra = max(wait_extra, e.seconds + 1)
            except Exception as e:
                print("BIO CLOCK LOOP ERROR:", e)

        if wait_extra:
            await asyncio.sleep(wait_extra)
            continue

        now_dt = datetime.now(TEHRAN)
        sleep_s = 60 - now_dt.second - (now_dt.microsecond / 1_000_000)
        if sleep_s <= 0.5:
            sleep_s += 60
        await asyncio.sleep(sleep_s)


# ============================================================
# فونت چت (هر پیامی که می‌فرستی همون لحظه با استایل انتخابی ویرایش می‌شه)
# ============================================================

KASHIDA_COUNT = 2            # تعداد «ـ» بین حروف در حالت کشیده
CHAT_FONT_MAX_LEN = 3800     # بیشتر از این طول، ویرایش نمی‌شه (حد تلگرام ۴۰۹۶)

CHAT_FONT_MODES = {
    "1": "بولد",
    "2": "کشیده",
    "3": "تکی",
    "4": "نقل قول",
    "5": "نقل قول بولد",
    "6": "کشیده فونت دار",
}


def norm_fa(s):
    s = (s or "").replace("\u200c", " ").replace("ـ", "")
    s = s.replace("ي", "ی").replace("ك", "ک")
    return " ".join(s.split())


CHAT_FONT_BY_NAME = {norm_fa(v): k for k, v in CHAT_FONT_MODES.items()}

_PERSIAN_LETTERS = set("آابپتثجچحخدذرزژسشصضطظعغفقکگلمنوهیئأإؤةكيء")
_NON_JOINERS = set("ادذرزژوآأإؤةء")


def kashida(text, n=KASHIDA_COUNT):
    """بین حروف چسبان فارسی «ـ» می‌ذاره: سلام -> سـ‍ـلام (کشیده)"""
    out = []
    for i, ch in enumerate(text):
        out.append(ch)
        if i + 1 >= len(text):
            continue
        nxt = text[i + 1]
        if ch in _PERSIAN_LETTERS and ch not in _NON_JOINERS and nxt in _PERSIAN_LETTERS:
            if ch == "ل" and nxt == "ا":
                continue
            out.append("ـ" * n)
    return "".join(out)


def build_mono_entity(text):
    length = len(tl_utils.add_surrogate(text))
    if length <= 0:
        return []
    return [MessageEntityCode(offset=0, length=length)]


# فقط کاراکترهایی که خودشون به صورت خودکار ساخته می‌شن (لینک، منشن، هشتگ...)
AUTO_ENTITY_TYPES = (
    MessageEntityUrl, MessageEntityMention, MessageEntityHashtag,
    MessageEntityCashtag, MessageEntityBotCommand, MessageEntityEmail,
    MessageEntityPhone,
)


def style_chat_text(text, mode):
    """(متن جدید، لیست انتیتی) برای حالت انتخابی. اگه حالت نامعتبر بود None."""
    if mode == "1":
        return text, build_bold_entity(text)
    if mode == "2":
        return kashida(text), []
    if mode == "3":
        return text, build_mono_entity(text)
    if mode == "4":
        return text, build_quote_entity(text)
    if mode == "5":
        return text, build_bold_entity(text) + build_quote_entity(text)
    if mode == "6":
        new = apply_font(kashida(text), 1)
        return new, build_bold_entity(new)
    return None


# جلوگیری از ویرایش پیام‌هایی که خود ربات می‌فرسته (پاسخ خودکار، اسپم، ...)
style_hold = {}


def hold_style(chat_id, seconds=8):
    style_hold[chat_id] = time.time() + seconds


def clean_dashes(text):
    return re.sub(r"^[\s\-–—_]+|[\s\-–—_]+$", "", text or "")


SPAM_RE = re.compile(r"^اسپم\s+(.+?)\s+([0-9۰-۹]+)$", re.S)


def looks_like_command(text):
    """دستورهای PV / گروه که نباید با فونت چت ویرایش بشن."""
    t = (text or "").strip()
    if t in ("بلاک", "سکوت", "حذف سکوت", "حذف", "توقف اسپم"):
        return True
    if clean_dashes(t) == "چت فایل":
        return True
    if t.startswith("حذف "):
        return True
    if t.startswith("اسپم "):
        return True
    return False


# ============================================================
# اسپم در گروه / کانال:  «اسپم سلام 10»  (حداکثر ۱۵۰)
# ============================================================

SPAM_MAX = 150
spam_running = set()
spam_stop = set()


async def delete_command_message(event):
    try:
        suppress_delete_ids.add(int(event.message.id))
        await client.delete_messages(event.chat_id, [event.message.id], revoke=True)
    except Exception as e:
        print("DELETE CMD ERROR:", e)


async def run_spam(chat_id, text, count, reply_to=None):
    spam_running.add(chat_id)
    spam_stop.discard(chat_id)
    sent = 0
    error = None

    mode = get_setting("chat_font", "0")
    styled = style_chat_text(text, mode) if mode != "0" else None
    send_text, send_entities = styled if styled else (text, [])

    try:
        while sent < count:
            if chat_id in spam_stop:
                break
            hold_style(chat_id, 8)
            try:
                await client.send_message(
                    chat_id, send_text,
                    formatting_entities=send_entities,
                    reply_to=reply_to
                )
                sent += 1
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 1)
                continue
            except Exception as e:
                error = f"{type(e).__name__}: {str(e)[:300]}"
                break
            await asyncio.sleep(random.uniform(0.35, 0.7))
    finally:
        spam_running.discard(chat_id)
        stopped = chat_id in spam_stop
        spam_stop.discard(chat_id)

    if error or stopped:
        try:
            note = "⛔ <b>اسپم متوقف شد.</b>" if stopped and not error else "❌ <b>اسپم نیمه‌کاره ماند.</b>"
            note += f"\n<b>ارسال‌شده:</b> {sent} از {count}"
            if error:
                note += f"\n<code>{error}</code>"
            await client.send_message("me", note, parse_mode="html")
        except Exception:
            pass

# ============================================================
# چت فایل: ساخت فایل HTML از کل چت (دارک، شبیه تلگرام) و ارسال به Saved Messages
# ============================================================

EXPORT_MAX_PHOTOS = 300                      # حداکثر تعداد عکسی که داخل فایل جاسازی می‌شود
EXPORT_MAX_PHOTO_BYTES = 40 * 1024 * 1024    # حداکثر مجموع حجم عکس‌های جاسازی‌شده

JALALI_MONTHS = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
                 "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]
WEEKDAYS_FA = {0: "دوشنبه", 1: "سه‌شنبه", 2: "چهارشنبه", 3: "پنجشنبه",
               4: "جمعه", 5: "شنبه", 6: "یکشنبه"}


def to_fa_digits(s):
    return str(s).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def gregorian_to_jalali(gy, gm, gd):
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    if gy > 1600:
        jy = 979
        gy -= 1600
    else:
        jy = 0
        gy -= 621
    gy2 = gy + 1 if gm > 2 else gy
    days = (365 * gy) + ((gy2 + 3) // 4) - ((gy2 + 99) // 100) + ((gy2 + 399) // 400) - 80 + gd + g_d_m[gm - 1]
    jy += 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + days // 31
        jd = 1 + days % 31
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + (days - 186) % 30
    return jy, jm, jd


def jalali_short(dt):
    jy, jm, jd = gregorian_to_jalali(dt.year, dt.month, dt.day)
    return f"{jy}/{jm:02d}/{jd:02d}"


def jalali_long(dt):
    jy, jm, jd = gregorian_to_jalali(dt.year, dt.month, dt.day)
    return to_fa_digits(f"{jd} {JALALI_MONTHS[jm - 1]} {jy}")


def human_size(n):
    try:
        n = float(n)
    except Exception:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def fmt_duration(sec):
    try:
        sec = int(sec)
    except Exception:
        return ""
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def esc(s):
    return pyhtml.escape(str(s or ""), quote=True)


ACTION_TEXT = {
    "MessageActionScreenshotTaken": "📸 اسکرین‌شات از چت گرفته شد",
    "MessageActionHistoryClear": "🧹 تاریخچه چت پاک شد",
    "MessageActionContactSignUp": "🎉 مخاطب به تلگرام پیوست",
    "MessageActionSetMessagesTTL": "⏱ تایمر حذف خودکار تغییر کرد",
    "MessageActionChatCreate": "👥 گروه ساخته شد",
    "MessageActionPinMessage": "📌 پیامی پین شد",
    "MessageActionGiftPremium": "🎁 هدیه پریمیوم",
    "MessageActionCustomAction": "🔔 پیام سیستمی",
}


def describe_action(action):
    name = type(action).__name__
    if name == "MessageActionPhoneCall":
        kind = "تصویری" if getattr(action, "video", False) else "صوتی"
        dur = getattr(action, "duration", None)
        extra = f" — مدت {fmt_duration(dur)}" if dur else " — بی‌پاسخ/لغو"
        return f"📞 تماس {kind}{extra}"
    return ACTION_TEXT.get(name, f"🔔 پیام سیستمی ({name.replace('MessageAction', '')})")


def describe_media(m):
    """(آیکون، عنوان، توضیح) برای رسانه‌هایی که داخل فایل جاسازی نمی‌شن."""
    f = getattr(m, "file", None)
    dur = fmt_duration(getattr(f, "duration", None)) if f and getattr(f, "duration", None) else ""
    size = human_size(getattr(f, "size", None)) if f and getattr(f, "size", None) else ""
    sub = " · ".join(x for x in (dur, size) if x)

    if m.photo:
        return "🖼", "عکس", size
    if m.sticker:
        emoji = getattr(f, "emoji", "") or ""
        return "🏷", f"استیکر {emoji}".strip(), ""
    if m.video_note:
        return "⭕", "ویدیو مسیج", sub
    if m.voice:
        return "🎤", "پیام صوتی", sub
    if m.gif:
        return "🎞", "گیف", sub
    if m.video:
        return "🎬", "ویدیو", sub
    if m.audio:
        title = getattr(f, "title", None) or getattr(f, "name", None) or "فایل صوتی"
        return "🎵", title, sub
    if m.document:
        return "📄", getattr(f, "name", None) or "فایل", size
    if m.contact:
        c = m.contact
        return "👤", f"مخاطب: {(c.first_name or '')} {(c.last_name or '')}".strip(), c.phone_number or ""
    if m.venue:
        return "📍", m.venue.title or "مکان", m.venue.address or ""
    if m.geo:
        return "📍", "موقعیت مکانی", f"{m.geo.lat:.5f}, {m.geo.long:.5f}"
    if m.poll:
        q = m.poll.poll.question
        q = getattr(q, "text", q)
        return "📊", f"نظرسنجی: {q}", ""
    if m.dice:
        return "🎲", f"{m.dice.emoticon} {m.dice.value}", ""
    return "📎", "رسانه", ""


async def fetch_photo_b64(m, budget):
    if budget["count"] >= EXPORT_MAX_PHOTOS or budget["bytes"] >= EXPORT_MAX_PHOTO_BYTES:
        return ""
    for thumb in (1, 0):
        try:
            data = await client.download_media(m, file=bytes, thumb=thumb)
            if data:
                budget["count"] += 1
                budget["bytes"] += len(data)
                return base64.b64encode(data).decode("ascii")
        except Exception:
            continue
    return ""


async def message_to_item(m, me, other, budget):
    out = bool(m.out)
    who = me if out else other
    ts = m.date.astimezone(TEHRAN)

    item = {
        "id": int(m.id),
        "out": out,
        "ts": ts,
        "edit_ts": m.edit_date.astimezone(TEHRAN) if getattr(m, "edit_date", None) else None,
        "sender_name": user_name(who),
        "sender_username": getattr(who, "username", None) or "",
        "sender_id": getattr(who, "id", 0),
        "html": "",
        "plain": (m.message or "").strip(),
        "service": "",
        "media": None,
        "reply_id": None,
        "fwd": "",
        "reactions": "",
        "web": None,
    }

    if getattr(m, "action", None):
        item["service"] = describe_action(m.action)
        return item

    try:
        if m.message:
            item["html"] = tl_html.unparse(m.message, m.entities or []).replace("\n", "<br>")
    except Exception:
        item["html"] = esc(m.message).replace("\n", "<br>")

    try:
        if m.reply_to and getattr(m.reply_to, "reply_to_msg_id", None):
            item["reply_id"] = int(m.reply_to.reply_to_msg_id)
    except Exception:
        pass

    try:
        if m.fwd_from:
            fwd = m.forward
            name = getattr(m.fwd_from, "from_name", None)
            if not name and fwd is not None:
                ent = getattr(fwd, "sender", None) or getattr(fwd, "chat", None)
                name = tl_utils.get_display_name(ent) if ent else ""
            item["fwd"] = name or "ناشناس"
    except Exception:
        item["fwd"] = "ناشناس"

    try:
        if m.reactions and m.reactions.results:
            parts = []
            for r in m.reactions.results:
                emo = getattr(r.reaction, "emoticon", "") or "⭐"
                parts.append(f"{emo} {r.count}")
            item["reactions"] = "  ".join(parts)
    except Exception:
        pass

    try:
        if m.media:
            if type(m.media).__name__ == "MessageMediaWebPage":
                wp = getattr(m.media, "webpage", None)
                if wp is not None and getattr(wp, "url", None):
                    item["web"] = {
                        "site": getattr(wp, "site_name", "") or "",
                        "title": getattr(wp, "title", "") or "",
                        "desc": (getattr(wp, "description", "") or "")[:300],
                        "url": wp.url,
                    }
            else:
                icon, title, sub = describe_media(m)
                img = ""
                if m.photo:
                    img = await fetch_photo_b64(m, budget)
                item["media"] = {"icon": icon, "title": title, "sub": sub, "img": img}
    except Exception as e:
        item["media"] = {"icon": "📎", "title": "رسانه", "sub": type(e).__name__, "img": ""}

    return item


EXPORT_CSS = """
:root{--bg:#0e1621;--panel:#17212b;--in:#182533;--out:#2b5278;--text:#f5f5f5;--muted:#7f91a4;--accent:#6ab3f3;--line:#0b121a;--chip:rgba(24,37,51,.92);--font:'Vazirmatn',Tahoma,sans-serif}
body.light{--bg:#dfe7ee;--panel:#ffffff;--in:#ffffff;--out:#eaffd0;--text:#101820;--muted:#6b7b8a;--accent:#2481cc;--line:#cfd9e2;--chip:rgba(255,255,255,.92)}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--font);font-weight:600;line-height:1.9;font-size:15px}
body.nasta .t{line-height:2.6}
.top{position:sticky;top:0;z-index:20;background:var(--panel);border-bottom:1px solid var(--line);padding:8px 12px;display:flex;align-items:center;gap:10px;direction:ltr}
.av{width:44px;height:44px;border-radius:50%;flex:none;background:linear-gradient(135deg,#6ab3f3,#2b5278);display:flex;align-items:center;justify-content:center;font-weight:900;font-size:20px;color:#fff;overflow:hidden}
.av img{width:100%;height:100%;object-fit:cover}
.ti{flex:1;min-width:0;direction:rtl;text-align:right}
.ti b{display:block;font-weight:900;font-size:16px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.5}
.ti small{color:var(--muted);font-size:12px;line-height:1.4;display:block}
.ctl{display:flex;gap:6px;align-items:center;flex-wrap:wrap;justify-content:flex-end}
.ctl select,.ctl button,.ctl input{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:10px;padding:5px 8px;font-family:inherit;font-size:12px;font-weight:700}
.ctl input{width:110px}
.card{max-width:760px;margin:14px auto;background:var(--panel);border-radius:16px;padding:14px 16px;border:1px solid var(--line)}
.card h2{margin:0 0 8px;font-weight:900;font-size:17px;font-family:'Lalezar','Vazirmatn',sans-serif}
.row{display:flex;justify-content:space-between;gap:12px;padding:4px 0;border-bottom:1px dashed var(--line);font-size:13px}
.row:last-child{border:0}.row span:first-child{color:var(--muted)}.row span:last-child{font-weight:800;direction:ltr;text-align:left;word-break:break-all}
.chat{max-width:760px;margin:0 auto;padding:8px 10px 60px;direction:ltr}
.date{position:sticky;top:62px;z-index:10;text-align:center;margin:14px 0 8px;direction:rtl}
.date span{display:inline-block;background:var(--chip);color:var(--text);border-radius:14px;padding:2px 14px;font-size:12.5px;font-weight:800}
.date small{color:var(--muted);font-weight:600;margin-right:6px}
.m{display:flex;margin:2px 0}.m.out{justify-content:flex-end}.m.in{justify-content:flex-start}
.b{max-width:84%;min-width:90px;padding:6px 10px 4px;border-radius:14px;position:relative;word-wrap:break-word;overflow-wrap:anywhere;box-shadow:0 1px 1px rgba(0,0,0,.25)}
.in .b{background:var(--in);border-bottom-left-radius:4px}.out .b{background:var(--out);border-bottom-right-radius:4px}
.nm{font-weight:900;color:var(--accent);font-size:13px;direction:rtl;text-align:right;line-height:1.6}
.nm small{color:var(--muted);font-weight:600;font-size:10.5px;margin-right:6px;direction:ltr;display:inline-block}
.t{unicode-bidi:plaintext;text-align:start;font-size:15.5px;white-space:normal}
.t a{color:var(--accent)}.t code,.t pre{font-family:ui-monospace,Menlo,Consolas,monospace;background:rgba(127,145,164,.18);border-radius:6px;padding:0 4px;direction:ltr;unicode-bidi:plaintext}
.t pre{display:block;padding:6px 8px;overflow:auto}
.t blockquote{margin:4px 0;padding:2px 8px;border-inline-start:3px solid var(--accent);background:rgba(106,179,243,.10);border-radius:4px}
.reply{display:block;border-right:3px solid var(--accent);background:rgba(106,179,243,.12);border-radius:6px;padding:2px 8px;margin:2px 0 4px;direction:rtl;text-align:right;text-decoration:none;color:var(--text);font-size:12.5px;line-height:1.6}
.reply b{display:block;color:var(--accent);font-weight:900}.reply span{display:block;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:60vw}
.fwd{font-size:12px;color:var(--accent);font-weight:800;direction:rtl;text-align:right}
.media{display:flex;gap:8px;align-items:center;background:rgba(127,145,164,.15);border-radius:10px;padding:6px 10px;margin:3px 0;direction:rtl}
.media i{font-style:normal;font-size:22px}.media b{display:block;font-weight:800;font-size:13.5px;line-height:1.5}.media small{color:var(--muted);font-size:11.5px}
.photo{display:block;max-width:100%;border-radius:10px;margin:3px 0}
.web{border-right:3px solid var(--accent);padding:2px 8px;margin:4px 0;direction:rtl;text-align:right;font-size:13px;line-height:1.6}
.web b{display:block;color:var(--accent);font-weight:900}.web span{color:var(--muted);font-size:12px}
.rx{margin-top:3px;font-size:12px;direction:rtl;text-align:right}
.ft{display:flex;justify-content:flex-end;gap:6px;align-items:center;color:var(--muted);font-size:11px;font-weight:600;direction:ltr;margin-top:1px}
.out .ft{color:#a9c7e6}body.light .out .ft{color:#5a8a3a}
.det{display:none;margin-top:3px;padding-top:3px;border-top:1px dashed rgba(127,145,164,.4);font-size:11px;color:var(--muted);direction:ltr;text-align:left;line-height:1.7;font-weight:600}
.b.sel .det,body.alldet .det{display:block}
.svc{text-align:center;margin:8px 0;direction:rtl}.svc span{display:inline-block;background:var(--chip);border-radius:14px;padding:2px 14px;font-size:12.5px;font-weight:800;color:var(--muted)}
.hid{display:none!important}
.foot{text-align:center;color:var(--muted);font-size:12px;padding:20px}
"""

EXPORT_JS = """
const sel=document.getElementById('fontSel');
sel.onchange=()=>{document.documentElement.style.setProperty('--font',sel.value);document.body.classList.toggle('nasta',sel.selectedOptions[0].dataset.nasta==='1');};
document.getElementById('themeBtn').onclick=()=>document.body.classList.toggle('light');
document.getElementById('detBtn').onclick=()=>document.body.classList.toggle('alldet');
document.querySelectorAll('.b').forEach(b=>b.addEventListener('click',e=>{if(e.target.closest('a'))return;b.classList.toggle('sel');}));
document.getElementById('q').addEventListener('input',e=>{
  const q=e.target.value.trim().toLowerCase();
  document.querySelectorAll('.m').forEach(m=>{m.classList.toggle('hid',q!==''&&!m.textContent.toLowerCase().includes(q));});
});
"""

EXPORT_FONTS_LINK = (
    "https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;600;800;900"
    "&family=Lalezar&family=Aref+Ruqaa:wght@400;700&family=Noto+Kufi+Arabic:wght@400;700;900"
    "&family=Noto+Nastaliq+Urdu:wght@400;700&display=swap"
)


def _render_bubble(item, prev, by_id):
    cls = "out" if item["out"] else "in"
    ts = item["ts"]
    parts = [f'<div class="m {cls}" id="m{item["id"]}"><div class="b">']

    new_run = (prev is None) or prev["out"] != item["out"] or prev["ts"].date() != ts.date() or prev.get("service")
    if new_run:
        uname = f' <small>@{esc(item["sender_username"])}</small>' if item["sender_username"] else ""
        parts.append(
            f'<div class="nm">{esc(item["sender_name"])}{uname}'
            f' <small>ID: {item["sender_id"]}</small></div>'
        )

    if item["fwd"]:
        parts.append(f'<div class="fwd">↪️ فوروارد از {esc(item["fwd"])}</div>')

    if item["reply_id"]:
        src = by_id.get(item["reply_id"])
        if src:
            snippet = src["plain"] or (src["media"]["title"] if src["media"] else "") or "پیام"
            parts.append(
                f'<a class="reply" href="#m{src["id"]}"><b>{esc(src["sender_name"])}</b>'
                f'<span>{esc(snippet[:90])}</span></a>'
            )
        else:
            parts.append('<div class="reply"><b>پاسخ</b><span>پیام اصلی در دسترس نیست</span></div>')

    media = item["media"]
    if media:
        if media["img"]:
            parts.append(f'<img class="photo" src="data:image/jpeg;base64,{media["img"]}" alt="photo">')
        else:
            sub = f'<small>{esc(media["sub"])}</small>' if media["sub"] else ""
            parts.append(f'<div class="media"><i>{media["icon"]}</i><div><b>{esc(media["title"])}</b>{sub}</div></div>')

    if item["web"]:
        w = item["web"]
        parts.append(
            f'<div class="web"><span>{esc(w["site"])}</span><b>{esc(w["title"])}</b>'
            f'<span>{esc(w["desc"])}</span><br><a href="{esc(w["url"])}" style="color:var(--accent)">{esc(w["url"][:70])}</a></div>'
        )

    if item["html"]:
        parts.append(f'<div class="t" dir="auto">{item["html"]}</div>')

    if item["reactions"]:
        parts.append(f'<div class="rx">{esc(item["reactions"])}</div>')

    edited = " · ✏️ ویرایش‌شده" if item["edit_ts"] else ""
    parts.append(f'<div class="ft"><span>{ts.strftime("%H:%M")}{edited}</span></div>')

    det = (
        f'#{item["id"]} · {esc(item["sender_name"])} ({item["sender_id"]})<br>'
        f'{jalali_short(ts)} · {ts.strftime("%Y-%m-%d %H:%M:%S")} (Tehran)'
    )
    if item["edit_ts"]:
        det += f'<br>ویرایش: {jalali_short(item["edit_ts"])} · {item["edit_ts"].strftime("%Y-%m-%d %H:%M:%S")}'
    parts.append(f'<div class="det">{det}</div>')

    parts.append("</div></div>")
    return "".join(parts)


def render_chat_html(meta, items):
    by_id = {it["id"]: it for it in items}
    total = len(items)
    mine = sum(1 for it in items if it["out"] and not it["service"])
    theirs = sum(1 for it in items if not it["out"] and not it["service"])
    photos = sum(1 for it in items if it["media"] and it["media"]["img"])
    medias = sum(1 for it in items if it["media"])
    first_ts = items[0]["ts"] if items else None
    last_ts = items[-1]["ts"] if items else None

    name = meta["name"]
    initial = esc((name or "?")[:1])
    avatar = (f'<img src="data:image/jpeg;base64,{meta["avatar"]}" alt="">' if meta.get("avatar") else initial)
    sub = []
    if meta.get("username"):
        sub.append(f'@{esc(meta["username"])}')
    sub.append(f'ID: {meta["id"]}')

    out = []
    out.append('<!DOCTYPE html><html lang="fa" dir="rtl"><head><meta charset="utf-8">')
    out.append('<meta name="viewport" content="width=device-width,initial-scale=1">')
    out.append(f'<title>چت با {esc(name)}</title>')
    out.append('<link rel="preconnect" href="https://fonts.googleapis.com">')
    out.append(f'<link href="{EXPORT_FONTS_LINK}" rel="stylesheet">')
    out.append(f'<style>{EXPORT_CSS}</style></head><body>')

    out.append('<div class="top">')
    out.append('<div class="ctl">')
    out.append('<input id="q" placeholder="🔍 جستجو">')
    out.append('<select id="fontSel">'
               '<option value="\'Vazirmatn\',Tahoma,sans-serif">وزیرمتن</option>'
               '<option value="\'Lalezar\',\'Vazirmatn\',sans-serif">لاله‌زار (کلفت)</option>'
               '<option value="\'Aref Ruqaa\',\'Vazirmatn\',serif">عارف رقعه</option>'
               '<option value="\'Noto Kufi Arabic\',\'Vazirmatn\',sans-serif">کوفی</option>'
               '<option value="\'Noto Nastaliq Urdu\',\'Vazirmatn\',serif" data-nasta="1">نستعلیق</option>'
               '</select>')
    out.append('<button id="themeBtn">🌓</button><button id="detBtn">ℹ️</button></div>')
    out.append(f'<div class="ti"><b>{esc(name)}</b><small>{" · ".join(sub)}</small></div>')
    out.append(f'<div class="av">{avatar}</div></div>')

    rows = [
        ("نام", meta["name"]),
        ("یوزرنیم", f'@{meta["username"]}' if meta.get("username") else "ندارد"),
        ("آیدی عددی", meta["id"]),
        ("شماره", meta.get("phone") or "مخفی"),
    ]
    if meta.get("bio"):
        rows.append(("بیو", meta["bio"]))
    rows += [
        ("اکانت من", f'{meta["me_name"]} · {meta["me_id"]}'),
        ("کل پیام‌ها", f"{total}  (من: {mine} · طرف مقابل: {theirs})"),
        ("رسانه‌ها", f"{medias}  (عکس جاسازی‌شده: {photos})"),
    ]
    if first_ts and last_ts:
        rows.append(("اولین پیام", f'{jalali_short(first_ts)} · {first_ts.strftime("%Y-%m-%d %H:%M:%S")}'))
        rows.append(("آخرین پیام", f'{jalali_short(last_ts)} · {last_ts.strftime("%Y-%m-%d %H:%M:%S")}'))
    rows.append(("زمان ساخت فایل", f'{jalali_short(meta["generated"])} · {meta["generated"].strftime("%Y-%m-%d %H:%M:%S")}'))

    out.append('<div class="card"><h2>📋 اطلاعات چت</h2>')
    for k, v in rows:
        out.append(f'<div class="row"><span>{esc(k)}</span><span>{esc(v)}</span></div>')
    out.append('</div>')

    out.append('<div class="chat">')
    prev = None
    last_date = None
    for it in items:
        d = it["ts"].date()
        if d != last_date:
            last_date = d
            out.append(
                f'<div class="date"><span>{jalali_long(it["ts"])}'
                f'<small>{WEEKDAYS_FA[it["ts"].weekday()]} · {it["ts"].strftime("%Y-%m-%d")}</small></span></div>'
            )
            prev = None
        if it["service"]:
            out.append(f'<div class="svc"><span>{esc(it["service"])} · {it["ts"].strftime("%H:%M")}</span></div>')
        else:
            out.append(_render_bubble(it, prev, by_id))
        prev = it
    out.append('</div>')
    out.append('<div class="foot">⚡ MAXO SELF</div>')
    out.append(f'<script>{EXPORT_JS}</script></body></html>')
    return "".join(out)


async def export_chat_file(chat_id):
    status_msg = None
    try:
        me = await client.get_me()
        other = await client.get_entity(chat_id)

        status_msg = await client.send_message(
            "me", "⏳ <b>در حال ساخت فایل چت...</b>", parse_mode="html"
        )

        items = []
        budget = {"count": 0, "bytes": 0}
        async for m in client.iter_messages(chat_id, reverse=True):
            items.append(await message_to_item(m, me, other, budget))
            if len(items) % 500 == 0:
                try:
                    await status_msg.edit(
                        f"⏳ <b>در حال ساخت فایل چت...</b>\n{len(items)} پیام خونده شد",
                        parse_mode="html"
                    )
                except Exception:
                    pass

        phone = getattr(other, "phone", None)
        bio = ""
        try:
            from telethon.tl.functions.users import GetFullUserRequest
            full = await client(GetFullUserRequest(other))
            bio = getattr(full.full_user, "about", "") or ""
        except Exception:
            pass

        avatar = ""
        try:
            data = await client.download_profile_photo(other, file=bytes, download_big=False)
            if data:
                avatar = base64.b64encode(data).decode("ascii")
        except Exception:
            pass

        meta = {
            "name": user_name(other),
            "username": getattr(other, "username", None) or "",
            "id": other.id,
            "phone": f"+{phone}" if phone else "",
            "bio": bio,
            "avatar": avatar,
            "me_name": user_name(me),
            "me_id": me.id,
            "generated": datetime.now(TEHRAN),
        }

        html_text = render_chat_html(meta, items)

        out_dir = Path(DATA_DIR) / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(TEHRAN).strftime("%Y%m%d_%H%M")
        path = out_dir / f"chat_{other.id}_{stamp}.html"
        path.write_text(html_text, encoding="utf-8")

        caption = (
            "🗂 <b>فایل چت آماده شد</b>\n"
            f"<b>با:</b> {esc(meta['name'])}\n"
            f"<b>ID:</b> <code>{other.id}</code>\n"
            f"<b>تعداد پیام:</b> {len(items)}\n\n"
            "<i>فایل رو با مرورگر باز کن (دارک، قابل جستجو، با تپ روی هر پیام جزئیاتش میاد).</i>"
        )
        await client.send_file("me", str(path), caption=caption, parse_mode="html", force_document=True)

        try:
            path.unlink()
        except Exception:
            pass
        try:
            await status_msg.delete()
        except Exception:
            pass

    except Exception as e:
        print("EXPORT CHAT ERROR:", type(e).__name__, e)
        try:
            await client.send_message(
                "me",
                f"❌ <b>ساخت فایل چت ناموفق بود.</b>\n<code>{type(e).__name__}: {str(e)[:500]}</code>",
                parse_mode="html"
            )
        except Exception:
            pass



# ============================================================
# SAVED MESSAGES (دستورات)
# ============================================================

@client.on(events.NewMessage(outgoing=True))
async def saved_messages(event):
    if event.chat_id != ME_ID:
        return

    text = (event.raw_text or "").strip()
    if not text:
        return

    cmd = text.strip()

    # -------- در حال منتظر گرفتن متن جدید پاسخ خودکار --------
    if get_setting("waiting_for_text", "0") == "1":
        if cmd == "لغو":
            set_setting("waiting_for_text", "0")
            await event.reply("❌ <b>لغو شد.</b>", parse_mode="html")
            return

        raw_text = event.message.message or ""
        entities = event.message.entities or []
        set_setting("auto_text", raw_text)
        set_auto_entities(entities)
        set_setting("waiting_for_text", "0")
        await event.reply(
            "✅ <b>متن پاسخ خودکار ذخیره شد</b> (دقیقاً با همون فرمتی که فرستادی).",
            parse_mode="html"
        )
        return

    # -------- HELP --------
    if cmd in ("دستورات", "لیست دستورات", "لیست", "منو"):
        await reply_help(event)
        return

    # -------- فونت چت --------
    norm_cmd = norm_fa(cmd)
    if norm_cmd == "فونت چت" or norm_cmd.startswith("فونت چت "):
        arg = normalize_number(norm_cmd[len("فونت چت"):].strip())

        if arg in ("خاموش", "0", "حذف"):
            set_setting("chat_font", "0")
            await event.reply("🔴 <b>فونت چت خاموش شد.</b>", parse_mode="html")
            return

        mode = arg if arg in CHAT_FONT_MODES else CHAT_FONT_BY_NAME.get(arg)
        if mode:
            set_setting("chat_font", mode)
            sample, sample_ents = style_chat_text("سلام، این یک نمونه است MAXO", mode)
            await event.reply(
                f"✅ <b>فونت چت:</b> {CHAT_FONT_MODES[mode]}\nاز الان هر پیامی بفرستی همون لحظه با این فونت ویرایش می‌شه. نمونه:",
                parse_mode="html"
            )
            await client.send_message("me", sample, formatting_entities=sample_ents)
            return

        cur = CHAT_FONT_MODES.get(get_setting("chat_font", "0"), "خاموش")
        lines = "\n".join(
            f"<code>فونت چت {k}</code> — {v}" for k, v in CHAT_FONT_MODES.items()
        )
        await event.reply(
            "🔤 <b>فونت چت</b>\n"
            "<blockquote>هر پیامی که می‌فرستی همون لحظه با فونت انتخابی ویرایش می‌شه</blockquote>\n"
            f"{lines}\n"
            "<code>فونت چت خاموش</code> — غیرفعال\n\n"
            f"<b>حالت فعلی:</b> {cur}",
            parse_mode="html"
        )
        return

    # -------- AUTO REPLY --------
    if cmd == "روشن":
        set_setting("auto_reply", "1")
        await event.reply("🟢 <b>پاسخ خودکار:</b> <code>روشن</code>", parse_mode="html")
        return

    if cmd == "خاموش":
        set_setting("auto_reply", "0")
        await event.reply("🔴 <b>پاسخ خودکار:</b> <code>خاموش</code>", parse_mode="html")
        return

    if cmd == "متن":
        raw_text = get_setting("auto_text", "")
        if not raw_text:
            await event.reply(
                "📝 هنوز متنی تنظیم نشده.\n\nبرای تنظیم:\n<code>تغییر متن</code>",
                parse_mode="html"
            )
            return

        style = get_setting("auto_style", "as_typed")
        style_fa = {"as_typed": "ساده (دقیقاً مثل نوشتنت)", "bold": "بولد", "quote": "نقل‌قول"}.get(style, style)

        await event.reply(
            f"📝 <b>متن فعلی پاسخ خودکار</b>\n<b>استایل:</b> {style_fa}\n━━━━━━━━━━━━━━━━━━",
            parse_mode="html"
        )
        preview_text, preview_entities = get_auto_reply_payload()
        await client.send_message("me", preview_text, formatting_entities=preview_entities)
        return

    if cmd == "تغییر متن":
        set_setting("waiting_for_text", "1")
        await event.reply(
            "✍️ <b>پیام بعدیت رو به هر شکلی که می‌خوای بفرست</b> "
            "(بولد، ایتالیک، نقل‌قول، لینک، ساده...) — دقیقاً همونجوری ذخیره می‌شه.\n\n"
            "برای لغو: <code>لغو</code>",
            parse_mode="html"
        )
        return

    if cmd == "استایل ساده":
        set_setting("auto_style", "as_typed")
        await event.reply("✅ <b>استایل پاسخ خودکار:</b> ساده (دقیقاً مثل نوشتنت)", parse_mode="html")
        return

    if cmd == "استایل بولد":
        set_setting("auto_style", "bold")
        await event.reply("✅ <b>استایل پاسخ خودکار:</b> بولد", parse_mode="html")
        return

    if cmd in ("استایل نقل قول", "استایل نقل‌قول"):
        set_setting("auto_style", "quote")
        await event.reply("✅ <b>استایل پاسخ خودکار:</b> نقل‌قول", parse_mode="html")
        return

    # -------- NAME CLOCK --------
    if cmd.startswith("اسم "):
        arg = text[4:].strip()
        norm = normalize_number(arg)

        if norm.isdigit() and 1 <= int(norm) <= len(FONT_MAPS):
            font = int(norm)
            ok = await enable_name_clock(font=font)
            preview = build_name_text(font)
            await event.reply(
                f"🟢 <b>ساعت در اسم فعال شد (فونت {font}).</b>\n<code>{preview}</code>"
                if ok else "❌ <b>فعال‌سازی ناموفق بود.</b>",
                parse_mode="html"
            )
        elif arg:
            ok = await enable_name_clock(label=arg)
            font = int(get_setting("name_font", "1"))
            preview = build_name_text(font)
            await event.reply(
                f"🟢 <b>برچسب اسم تغییر کرد و ساعت فعال شد.</b>\n<code>{preview}</code>"
                if ok else "❌ <b>فعال‌سازی ناموفق بود.</b>",
                parse_mode="html"
            )
        return

    if cmd == "حذف اسم":
        await disable_name_clock()
        await event.reply("🔴 <b>ساعت در اسم خاموش شد و اسم قبلی برگشت.</b>", parse_mode="html")
        return

    # -------- BIO CLOCK --------
    if cmd.startswith("بیو "):
        arg = normalize_number(text[4:].strip())
        if arg.isdigit() and 1 <= int(arg) <= len(FONT_MAPS):
            font = int(arg)
            ok = await enable_bio_clock(font)
            await event.reply(
                f"🟢 <b>ساعت در بیو فعال شد (فونت {font}).</b>" if ok else "❌ <b>فعال‌سازی ناموفق بود.</b>",
                parse_mode="html"
            )
        return

    if cmd == "حذف بیو":
        await disable_bio_clock()
        await event.reply("🔴 <b>ساعت در بیو خاموش شد و بیوی قبلی برگشت.</b>", parse_mode="html")
        return

    # -------- ARCHIVE TOGGLE --------
    if cmd == "آرشیو روشن":
        set_setting("archive_enabled", "1")
        await event.reply("🟢 <b>آرشیو پیام‌های حذف‌شده روشن شد.</b>", parse_mode="html")
        return

    if cmd == "آرشیو خاموش":
        set_setting("archive_enabled", "0")
        await event.reply("🔴 <b>آرشیو پیام‌های حذف‌شده خاموش شد.</b>", parse_mode="html")
        return

    # -------- STATUS / ACCOUNT --------
    if cmd == "وضعیت":
        await event.reply(await status(), parse_mode="html")
        return

    if cmd == "اکانت":
        await event.reply(await account_info(), parse_mode="html")
        return

    # -------- BLOCK LIST / MUTE LIST --------
    if cmd == "بلاک‌ها":
        await event.reply(block_list(), parse_mode="html")
        return

    if cmd == "سکوت‌ها":
        await event.reply(mute_list(), parse_mode="html")
        return

    # -------- BLOCK / UNBLOCK BY VALUE --------
    if cmd.startswith("بلاک "):
        value = text[5:].strip()
        user = await resolve_user(value)
        result = await block_user(user)
        await event.reply(result, parse_mode="html")
        return

    if cmd.startswith("انبلاک "):
        value = text[7:].strip()
        user = await resolve_user(value)
        result = await unblock_user(user)
        await event.reply(result, parse_mode="html")
        return


# ============================================================
# PRIVATE CHAT (داخل PV هر شخص)
# ============================================================

@client.on(events.NewMessage)
async def private_handler(event):
    if not event.is_private:
        return

    if event.chat_id == ME_ID:
        return

    text = (event.raw_text or "").strip()

    # =====================================================
    # OUTGOING (پیام‌هایی که خودم داخل PV می‌فرستم)
    # =====================================================

    if event.out:

        # -------- بلاک همین PV --------
        if text == "بلاک":
            try:
                user = await client.get_entity(event.chat_id)
                result = await block_user(user)
                suppress_delete_ids.add(int(event.message.id))
                await event.reply(result, parse_mode="html")
            except Exception as e:
                print("BLOCK PV ERROR:", e)
            return

        # -------- سکوت همین PV --------
        if text == "سکوت":
            try:
                user = await client.get_entity(event.chat_id)
                add_mute(user)
                suppress_delete_ids.add(int(event.message.id))
                await client.delete_messages(event.chat_id, [event.message.id], revoke=True)
                await client.send_message(
                    "me",
                    f"🔇 <b>سکوت فعال شد برای:</b>\n{user_name(user)}\n<code>{user.id}</code>",
                    parse_mode="html"
                )
            except Exception as e:
                print("MUTE PV ERROR:", e)
            return

        # -------- حذف سکوت همین PV --------
        if text == "حذف سکوت":
            try:
                remove_mute(event.chat_id)
                suppress_delete_ids.add(int(event.message.id))
                await client.delete_messages(event.chat_id, [event.message.id], revoke=True)
                await client.send_message("me", "🔊 <b>سکوت این مخاطب برداشته شد.</b>", parse_mode="html")
            except Exception as e:
                print("UNMUTE PV ERROR:", e)
            return

        # -------- چت فایل: ساخت HTML از کل این چت و ارسال به Saved Messages --------
        if clean_dashes(text) == "چت فایل":
            await delete_command_message(event)
            asyncio.create_task(export_chat_file(event.chat_id))
            return

        # -------- حذف سریع با ریپلای روی یک پیام خاص --------
        if text == "حذف" and event.is_reply:
            await delete_replied_message(event)
            return

        # -------- حذف N پیام --------
        if text.startswith("حذف"):
            parts = text.split(maxsplit=1)
            if len(parts) != 2:
                return
            value = normalize_number(parts[1].strip())
            if not value.isdigit():
                return
            await delete_private_messages(event, int(value))
            return

        return

    # =====================================================
    # INCOMING (پیام‌هایی که طرف مقابل می‌فرستد)
    # =====================================================

    sender = await event.get_sender()
    if not sender:
        return

    save_chat(sender, text)

    # -------- اگر ساکت است: کش کن و پاک کن برای هردو --------
    if is_muted(sender.id):
        try:
            await cache_incoming_message(event)
        except Exception:
            pass
        try:
            suppress_delete_ids.add(int(event.message.id))
            await client.delete_messages(event.chat_id, [event.message.id], revoke=True)
        except Exception as e:
            print("MUTE DELETE ERROR:", e)
        return

    # -------- بلاک از PV --------
    if text == "بلاک":
        result = await block_user(sender)
        await event.reply(result, parse_mode="html")
        return

    # -------- کش کردن پیام (متن/عکس/فیلم/ویس/زمان‌دار) برای آرشیو حذف --------
    try:
        await cache_incoming_message(event)
    except Exception:
        pass

    # =====================================================
    # AUTO REPLY (حداکثر هر ۱۰ دقیقه یکبار، با تاخیر تصادفی تا ۲۰ ثانیه)
    # =====================================================

    if get_setting("auto_reply", "0") != "1":
        return

    last = get_last_auto_reply(sender.id)
    if time.time() - last < AUTO_REPLY_COOLDOWN:
        return

    asyncio.create_task(send_delayed_autoreply(event, sender.id))


# ============================================================
# گروه / کانال: اسپم
# ============================================================

@client.on(events.NewMessage(outgoing=True))
async def group_handler(event):
    if event.is_private or event.chat_id == ME_ID:
        return

    text = (event.raw_text or "").strip()
    if not text:
        return

    if text == "توقف اسپم":
        spam_stop.add(event.chat_id)
        await delete_command_message(event)
        return

    match = SPAM_RE.match(text)
    if not match:
        return

    spam_text = match.group(1).strip()
    count = int(normalize_number(match.group(2)))
    if not spam_text or count < 1:
        return
    count = min(count, SPAM_MAX)

    await delete_command_message(event)

    if event.chat_id in spam_running:
        return

    reply_to = event.message.reply_to_msg_id
    asyncio.create_task(run_spam(event.chat_id, spam_text, count, reply_to))


# ============================================================
# فونت چت: ویرایش آنی پیام‌های خودم با استایل انتخابی
# ============================================================

@client.on(events.NewMessage(outgoing=True))
async def chat_font_handler(event):
    mode = get_setting("chat_font", "0")
    if mode not in CHAT_FONT_MODES:
        return
    if event.chat_id == ME_ID:
        return
    if time.time() < style_hold.get(event.chat_id, 0):
        return

    msg = event.message
    text = msg.message or ""
    if not text or len(text) > CHAT_FONT_MAX_LEN:
        return
    if text.startswith("/") or looks_like_command(text):
        return
    if msg.fwd_from or getattr(msg, "via_bot_id", None):
        return

    entities = list(msg.entities or [])
    # اگه خودم دستی فرمت زده باشم (بولد، لینک متنی، ...) دست نمی‌زنم
    if any(not isinstance(e, AUTO_ENTITY_TYPES) for e in entities):
        return
    # حالت‌هایی که متن رو تغییر می‌دن با لینک/منشن/هشتگ جور درنمیان
    if mode in ("2", "6") and entities:
        return

    try:
        chat = await event.get_chat()
        if getattr(chat, "bot", False):
            return
    except Exception:
        pass

    styled = style_chat_text(text, mode)
    if not styled:
        return
    new_text, new_entities = styled

    try:
        await client.edit_message(
            event.chat_id, msg,
            new_text,
            formatting_entities=entities + new_entities
        )
    except Exception as e:
        if type(e).__name__ != "MessageNotModifiedError":
            print("CHAT FONT EDIT ERROR:", type(e).__name__, e)


# ============================================================
# DELETED MESSAGE EVENT (برای آرشیو)
# ============================================================

@client.on(events.MessageDeleted())
async def deleted_handler(event):
    if get_setting("archive_enabled", "1") != "1":
        return

    for message_id in (event.deleted_ids or []):
        message_id = int(message_id)

        if message_id in suppress_delete_ids:
            suppress_delete_ids.discard(message_id)
            continue

        try:
            row = db.execute("""
            SELECT msg_id, chat_id, sender_id, name, username, text, media_type, media_path, created_at
            FROM deleted_archive WHERE msg_id=?
            """, (message_id,)).fetchone()

            if not row:
                continue

            db.execute("DELETE FROM deleted_archive WHERE msg_id=?", (message_id,))
            db.commit()

            await send_deleted_archive(row)
        except Exception as e:
            print("DELETED HANDLER ERROR:", e)


# ============================================================
# START
# ============================================================

async def main():
    global ME_ID

    print("=================================")
    print("        MAXO SELF - GOD")
    print("=================================")
    print("Session:", "SESSION_STRING (env)" if SESSION_STRING else SESSION + ".session")
    print("Database:", DB_FILE)
    print("Data dir:", DATA_DIR)

    await client.start()

    me = await client.get_me()
    ME_ID = me.id

    print()
    print("Logged in:", user_name(me))
    print("Username:", user_username(me))
    print("ID:", ME_ID)
    print()
    print("MAXO SELF IS RUNNING...")

    asyncio.create_task(clock_loop())
    asyncio.create_task(archive_cleanup_loop())

    try:
        await client.send_message(
            "me",
            (
                "⚡ <b>MAXO SELF فعال شد.</b>\n\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                "<b>وضعیت:</b> 🟢 فعال\n"
                f"<b>پاسخ خودکار:</b> "
                f"{'🟢 روشن' if get_setting('auto_reply') == '1' else '🔴 خاموش'}\n\n"
                "برای مشاهده تمام دستورات:\n"
                "<code>دستورات</code>\n\n"
                "━━━━━━━━━━━━━━━━━━"
            ),
            parse_mode="html"
        )
    except Exception as e:
        print("STARTUP ERROR:", e)

    await client.run_until_disconnected()


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nMAXO stopped.")
    except Exception as e:
        print("\nFATAL ERROR:")
        print(type(e).__name__, str(e))
