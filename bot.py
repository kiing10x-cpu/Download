"""
Instagram Reel Downloader + Dynamic Premium Bot
------------------------------------------------
Core idea: every menu (start, help, etc.) is a DB record — text, image,
buttons, auto-delete — edited live from the admin panel, never hardcoded.
See README section at the bottom of this file for what's implemented vs
intentionally skipped for scope.

Setup:
  pip install -r requirements.txt
  export BOT_TOKEN="123:ABC"
  export OWNER_ID="123456789"
  # optional:
  export MONGO_URI="mongodb+srv://..."
  export BACKUP_INTERVAL_HOURS="12"
  python3 bot.py
"""

import os
import re
import json
import csv
import time
import shutil
import logging
import tempfile
from datetime import datetime, timedelta
from urllib.parse import quote

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    ReplyKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)

import yt_dlp

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or 0)
MONGO_URI = os.environ.get("MONGO_URI", "").strip()
BACKUP_INTERVAL_HOURS = int(os.environ.get("BACKUP_INTERVAL_HOURS", "12"))

DATA_FILE = "bot_data.json"
BACKUP_DIR = "backups"
DOWNLOAD_DIR = "downloads"
MAX_LOCAL_BACKUPS = 10

os.makedirs(BACKUP_DIR, exist_ok=True)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ffmpeg is only needed when yt-dlp has to MERGE separate video+audio streams.
# Most Instagram reels are already a single muxed file, so we don't strictly
# need it — but when a merge is required and ffmpeg is missing, downloads
# used to crash. We now auto-detect ffmpeg (system install, or the portable
# binary from the `imageio-ffmpeg` package) and gracefully fall back to a
# no-merge format if neither is available.
FFMPEG_PATH = shutil.which("ffmpeg")
if not FFMPEG_PATH:
    try:
        import imageio_ffmpeg

        FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        FFMPEG_PATH = None
FFMPEG_AVAILABLE = bool(FFMPEG_PATH)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

if FFMPEG_AVAILABLE:
    log.info("ffmpeg found at: %s", FFMPEG_PATH)
else:
    log.warning(
        "ffmpeg not found. Downloads will use a no-merge format (still works, "
        "occasionally slightly lower max quality). Install ffmpeg or "
        "`pip install imageio-ffmpeg` to always get the absolute best quality."
    )

START_TIME = time.time()

INSTAGRAM_URL_RE = re.compile(
    r"(https?://(?:www\.)?instagram\.com/(?:reel|reels|p|tv)/[A-Za-z0-9_\-]+/?\S*)"
)

# ----------------------------------------------------------------------------
# Unicode "style" helpers (#1 — Style Text)
# ----------------------------------------------------------------------------

SMALL_CAPS_MAP = {
    "a": "ᴀ", "b": "ʙ", "c": "ᴄ", "d": "ᴅ", "e": "ᴇ", "f": "ꜰ", "g": "ɢ",
    "h": "ʜ", "i": "ɪ", "j": "ᴊ", "k": "ᴋ", "l": "ʟ", "m": "ᴍ", "n": "ɴ",
    "o": "ᴏ", "p": "ᴘ", "q": "ǫ", "r": "ʀ", "s": "ꜱ", "t": "ᴛ", "u": "ᴜ",
    "v": "ᴠ", "w": "ᴡ", "x": "x", "y": "ʏ", "z": "ᴢ",
}


def to_small_caps(text: str) -> str:
    return "".join(SMALL_CAPS_MAP.get(ch.lower(), ch) for ch in text)


# ----------------------------------------------------------------------------
# v2 build prompt — centralized small-caps strings (Section 10)
# ----------------------------------------------------------------------------
STR = {
    "processing": to_small_caps("processing your reel...") + "\n📥 " + to_small_caps("fetching") + " • 🔄 "
                  + to_small_caps("optimizing") + " • ✅ " + to_small_caps("almost done"),
    "done": "✅ " + to_small_caps("your reel is ready!") + "\n🎬 " + to_small_caps("saved and sent below"),
    "usage_title": to_small_caps("usage overview"),
    "how_to_use": (
        to_small_caps("how to use") + "\n\n"
        + "1️⃣ " + to_small_caps("copy any instagram reel link") + "\n"
        + "2️⃣ " + to_small_caps("paste it here in chat") + "\n"
        + "3️⃣ " + to_small_caps("wait a few seconds") + "\n"
        + "4️⃣ " + to_small_caps("download your reel instantly") + "\n\n"
        + to_small_caps("tips") + "\n"
        + "✅ " + to_small_caps("no watermark") + "\n"
        + "✅ " + to_small_caps("works with private links too") + "\n"
        + "✅ " + to_small_caps("unlimited with premium")
    ),
    "support_prompt": to_small_caps("describe your issue (text/photo/video)"),
    "ticket_created": lambda tid: "✅ " + to_small_caps(f"ticket #{tid} created. we'll reply soon"),
    "ticket_closed": lambda tid: "🔒 " + to_small_caps(f"ticket #{tid} closed. need help again? tap") + " 🎧 " + to_small_caps("support"),
}

RKB_DOWNLOAD = "⬇️ Download reel"
RKB_USAGE = "📊 My usage"
RKB_GIFT = "🎁 Send a gift"
RKB_LANGUAGE = "🌐 Language"
RKB_DEVELOPER = "👨‍💻 Developer"
RKB_HOWTO = "📘 How to use"
RKB_SUPPORT = "🎧 Support"


def main_reply_keyboard() -> ReplyKeyboardMarkup:
    """v2 Section 1 — persistent bottom keyboard, alongside the existing
    inline /start menu (doesn't replace it)."""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(RKB_DOWNLOAD)],
            [KeyboardButton(RKB_USAGE), KeyboardButton(RKB_GIFT)],
            [KeyboardButton(RKB_LANGUAGE), KeyboardButton(RKB_DEVELOPER)],
            [KeyboardButton(RKB_HOWTO), KeyboardButton(RKB_SUPPORT)],
        ],
        resize_keyboard=True,
    )


def _map_alpha_digit(text: str, upper_base: int, lower_base: int, digit_base=None) -> str:
    out = []
    for ch in text:
        if "A" <= ch <= "Z":
            out.append(chr(upper_base + (ord(ch) - ord("A"))))
        elif "a" <= ch <= "z":
            out.append(chr(lower_base + (ord(ch) - ord("a"))))
        elif digit_base and "0" <= ch <= "9":
            out.append(chr(digit_base + (ord(ch) - ord("0"))))
        else:
            out.append(ch)
    return "".join(out)


def to_bold_sans(text: str) -> str:
    return _map_alpha_digit(text, 0x1D5D4, 0x1D5EE, 0x1D7EC)


def to_bold_italic_sans(text: str) -> str:
    return _map_alpha_digit(text, 0x1D63C, 0x1D656, 0x1D7EC)


def to_monospace(text: str) -> str:
    return _map_alpha_digit(text, 0x1D670, 0x1D68A, 0x1D7F6)


def to_fullwidth(text: str) -> str:
    out = []
    for ch in text:
        if ch == " ":
            out.append("\u3000")
        elif "!" <= ch <= "~":
            out.append(chr(ord(ch) + 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def to_deco(text: str) -> str:
    return f"『✦ {text} ✦』"


STYLE_OPTIONS = [
    ("Small Caps", to_small_caps),
    ("Bold", to_bold_sans),
    ("Bold Italic", to_bold_italic_sans),
    ("Monospace", to_monospace),
    ("Fullwidth", to_fullwidth),
    ("Decorative", to_deco),
]

# ----------------------------------------------------------------------------
# Native colorful buttons (Bot API 9.4+ `style` field) — graceful fallback
# ----------------------------------------------------------------------------

try:
    InlineKeyboardButton(text="probe", callback_data="probe", style="primary")
    SUPPORTS_BUTTON_STYLE = True
except TypeError:
    SUPPORTS_BUTTON_STYLE = False
    log.warning(
        "Installed python-telegram-bot does not support button `style` "
        "(needs v22.7+). Colorful buttons will fall back to default look. "
        "Run: pip install -U python-telegram-bot"
    )


def styled_button(text, callback_data=None, url=None, style=None):
    kwargs = {}
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if url is not None:
        kwargs["url"] = url
    if style and SUPPORTS_BUTTON_STYLE:
        kwargs["style"] = style
    return InlineKeyboardButton(text, **kwargs)


# ----------------------------------------------------------------------------
# Default data / menu records (#1, #2, #4, #7)
# ----------------------------------------------------------------------------

DEFAULT_MENUS = {
    "start": {
        "text": (
            f"{to_deco(to_small_caps('welcome'))}\n\n"
            f"{to_small_caps('send any instagram reel link below')}\n"
            f"{to_small_caps('get it back in the best quality, instantly')}\n\n"
            f"『 {to_small_caps('tap guide for the full walkthrough')} 』"
        ),
        "parse_mode": None,
        "image_file_id": None,
        "buttons": [
            {"label": to_small_caps("📖 guide"), "type": "menu", "value": "help_user", "row": 1, "style": "primary"}
        ],
        "auto_delete_seconds": None,
        "updated_by": None,
        "updated_at": None,
        "translations": {},
    },
    "help_user": {
        "text": (
            f"{to_deco(to_small_caps('guide'))}\n\n"
            f"① {to_small_caps('send a reel link')}\n"
            f"② {to_small_caps('get it in best quality')}\n"
            f"③ {to_small_caps('tap get caption for a short quote')}"
        ),
        "parse_mode": None,
        "image_file_id": None,
        "buttons": [
            {"label": to_small_caps("🏠 main menu"), "type": "menu", "value": "start", "row": 1, "style": "primary"},
            {"label": "🆘 Support", "type": "callback", "value": "support_start", "row": 2, "style": "primary"},
            {"label": "🚫 Report Copyright Issue", "type": "callback", "value": "report_copyright", "row": 2, "style": "danger"},
        ],
        "auto_delete_seconds": None,
        "updated_by": None,
        "updated_at": None,
        "translations": {},
    },
    "reel_result": {
        "text": STR["done"],
        "parse_mode": "HTML",
        "image_file_id": None,
        "buttons": [
            {"label": to_small_caps("📝 caption"), "type": "callback", "value": "get_caption", "row": 1, "style": "primary"},
            {"label": to_small_caps("🔁 download another"), "type": "callback", "value": "download_another", "row": 1, "style": "primary"},
            {"label": to_small_caps("🏠 main menu"), "type": "menu", "value": "start", "row": 2, "style": "primary"},
        ],
        "auto_delete_seconds": None,
        "updated_by": None,
        "updated_at": None,
        "translations": {},
    },
    "disclaimer": {
        "text": (
            "<b>Disclaimer &amp; Terms of Use</b>\n\n"
            "This bot is a general-purpose media-downloading tool provided for "
            "personal and fair-use purposes only. It does not host, store, own, "
            "or claim any rights over the content it retrieves.\n\n"
            "By using this bot, you confirm that:\n"
            "• You have the necessary rights or permissions to download the "
            "content you request, or that your use qualifies as fair use / "
            "fair dealing under applicable law.\n"
            "• You will not use this bot to download, redistribute, or "
            "republish copyrighted material without the rights holder's consent.\n"
            "• You are solely and fully responsible for how you use any content "
            "obtained through this bot.\n\n"
            "The bot operator does not monitor, endorse, or verify the "
            "ownership of any content requested by users, and accepts no "
            "liability for any misuse, copyright infringement, or violation of "
            "third-party rights arising from your use of this service. Files "
            "are delivered directly to you and are not permanently stored on "
            "the bot's servers.\n\n"
            "Tap <b>I Agree &amp; Continue</b> to confirm you have read and "
            "accepted these terms."
        ),
        "parse_mode": "HTML",
        "image_file_id": None,
        "buttons": [
            {"label": "✅ I Agree & Continue", "type": "callback", "value": "agree_terms", "row": 1, "style": "success"}
        ],
        "auto_delete_seconds": None,
        "updated_by": None,
        "updated_at": None,
        "translations": {},
    },
    "maintenance": {
        "text": (
            "🛠 <b>Under Maintenance</b>\n"
            "We're currently performing scheduled improvements.\n"
            "Please check back in a little while — thanks for your patience!"
        ),
        "parse_mode": "HTML",
        "image_file_id": None,
        "buttons": [],
        "auto_delete_seconds": None,
        "updated_by": None,
        "updated_at": None,
        "translations": {},
    },
    "help_admin": {
        "text": (
            "❓ Admin Help\n\n"
            "📊 Stats & Activity — bot ke numbers dekho\n"
            "👥 Users & Groups — users list/message karo\n"
            "📢 Broadcast — sabko bhejo (forward-lock ke saath)\n"
            "🎨 Menu & UI — har menu ka text/image/buttons edit karo\n"
            "⚙️ Settings & Admins — welcome/admins/maintenance/languages\n"
            "🛑 Danger Zone — destructive actions"
        ),
        "parse_mode": None,
        "image_file_id": None,
        "buttons": [
            {"label": "🔙 Admin Panel", "type": "callback", "value": "adm_home", "row": 1, "style": "primary"}
        ],
        "auto_delete_seconds": None,
        "updated_by": None,
        "updated_at": None,
        "translations": {},
    },
}

DEFAULT_DATA = {
    "users": {},
    "groups": {},
    "admins": [OWNER_ID] if OWNER_ID else [],
    "blocked": [],
    "menus": json.loads(json.dumps(DEFAULT_MENUS)),
    "settings": {
        "maintenance": False,
        "protect_broadcasts": True,
        "global_auto_delete_seconds": 0,
        "small_caps_buttons_default": True,
        "auto_replies": {},
        "rate_limit_max": 20,
        "rate_limit_window_seconds": 60,
        "inactive_reengage_days": 0,
        "languages": [],  # e.g. ["en", "hi"] — admin-added via Settings > Languages
        "lock_all_content": False,  # #4 — master forwarding/sharing lock
        "logger_channel_id": None,  # #14 — dedicated logger channel
        "logger_enabled": False,
        "owner_display_user_id": None,  # #10 — credit/contact button
        "owner_display_label": None,
        "support_chat_id": None,  # #11 — where support messages land; None = all admins
        "premium_enabled": False,
        "upi_id": None,
        "developer_id": None,
        "developer_link": None,
        "daily_limit": 20,
        "admin_group_id": None,   # #6 — ticket cards posted here
        "owner_id": None,         # #12 — /export gate
    },
    "broadcast_log": [],
    "restore_log": [],
    "sent_messages": {},        # #5 — chat_id (str) -> [message_id, ...] ring buffer, last 200
    "copyright_reports": [],    # PDF #3 — DMCA-style user reports
    "blocked_links": [],        # PDF #3 — specific links blocked by admin
    "blocked_domains": [],      # PDF #3 — whole domains blocked by admin
    "error_log": [],            # #13 — capped ring buffer of recent errors
    "metrics": {"reels_downloaded": 0, "start_count": 0, "broadcasts_sent": 0},
    "tickets": {},               # v2 §6 — ticket_id(str) -> {...}
    "ticket_msg_map": {},        # v2 §6 — admin_group_message_id(str) -> ticket_id
    "next_ticket_id": 1,
    "panel_msg": {},              # v2 §11 — chat_id(str) -> last panel message_id
    "gift_orders": {},            # v2 §4 — order_id(str) -> {...} (UPI pending payments)
    "next_gift_id": 1,
}

# ----------------------------------------------------------------------------
# Storage layer (#10 — already existed, kept, menus/settings merge into it)
# ----------------------------------------------------------------------------

BOT_DATA = {}
_mongo_client = None
_mongo_collection = None
_mongo_last_error = None
_rate_state = {}  # in-memory only, not persisted
_caption_cache = {}  # (chat_id, message_id) -> real Instagram caption, in-memory only
CAPTION_CACHE_MAX = 500


def _deep_merge_defaults(data: dict) -> dict:
    merged = json.loads(json.dumps(DEFAULT_DATA))
    for k, v in data.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k].update(v)
        else:
            merged[k] = v
    # ensure any newly-added default menus (and newly-added fields on
    # existing menus, e.g. "translations") exist even in old data files
    for menu_id, menu in DEFAULT_MENUS.items():
        if menu_id not in merged["menus"]:
            merged["menus"][menu_id] = json.loads(json.dumps(menu))
        else:
            for field, default_val in menu.items():
                merged["menus"][menu_id].setdefault(field, json.loads(json.dumps(default_val)))
    return merged


def get_mongo_collection():
    global _mongo_client, _mongo_collection, _mongo_last_error
    if not MONGO_URI:
        return None
    if _mongo_collection is not None:
        return _mongo_collection
    try:
        from pymongo import MongoClient

        _mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        _mongo_client.admin.command("ping")
        db = _mongo_client.get_default_database() or _mongo_client["bot_db"]
        _mongo_collection = db["bot_data"]
        _mongo_last_error = None
        return _mongo_collection
    except Exception as e:  # noqa: BLE001
        _mongo_last_error = str(e)
        _mongo_collection = None
        return None


def load_data():
    global BOT_DATA
    col = get_mongo_collection()

    if col is not None:
        doc = col.find_one({"_id": "bot_data"})
        if doc:
            doc.pop("_id", None)
            BOT_DATA = _deep_merge_defaults(doc)
            log.info("Loaded data from MongoDB.")
        else:
            if os.path.exists(DATA_FILE):
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    local = json.load(f)
                BOT_DATA = _deep_merge_defaults(local)
                col.update_one({"_id": "bot_data"}, {"$set": BOT_DATA}, upsert=True)
                log.info("Migrated local JSON data into MongoDB.")
            else:
                BOT_DATA = json.loads(json.dumps(DEFAULT_DATA))
                col.update_one({"_id": "bot_data"}, {"$set": BOT_DATA}, upsert=True)
        return

    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            BOT_DATA = _deep_merge_defaults(json.load(f))
        log.info("Loaded data from local JSON file.")
    else:
        BOT_DATA = json.loads(json.dumps(DEFAULT_DATA))
        save_data()


def save_data():
    col = get_mongo_collection()
    if col is not None:
        col.update_one({"_id": "bot_data"}, {"$set": BOT_DATA}, upsert=True)
        return
    tmp_path = DATA_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(BOT_DATA, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, DATA_FILE)


def make_backup_snapshot(reason: str = "scheduled") -> str:
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(BACKUP_DIR, f"backup_{ts}_{reason}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(BOT_DATA, f, ensure_ascii=False, indent=2)
    with open(path, "r", encoding="utf-8") as f:
        json.load(f)  # integrity check
    files = sorted(
        [os.path.join(BACKUP_DIR, x) for x in os.listdir(BACKUP_DIR)], key=os.path.getmtime
    )
    while len(files) > MAX_LOCAL_BACKUPS:
        os.remove(files.pop(0))
    return path


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def is_owner(user_id: int) -> bool:
    return OWNER_ID != 0 and user_id == OWNER_ID


def is_admin(user_id: int) -> bool:
    return is_owner(user_id) or user_id in BOT_DATA.get("admins", [])


def touch_user(update: Update) -> bool:
    """Records/updates the user record. Returns True if this is a brand-new user."""
    user = update.effective_user
    if not user:
        return False
    uid = str(user.id)
    now = datetime.utcnow().isoformat()
    users = BOT_DATA["users"]
    is_new = uid not in users
    if is_new:
        users[uid] = {
            "name": user.full_name, "username": user.username,
            "joined": now, "last_active": now, "last_reengaged": None,
            "lang": None, "lang_prompted": False,
            "accepted_terms": False, "accepted_terms_at": None,
            "downloads_today": 0, "downloads_today_date": None,
            "downloads_month": 0, "downloads_month_key": None,
            "plan": "Free", "open_ticket_id": None,
        }
    else:
        users[uid]["last_active"] = now
        users[uid]["name"] = user.full_name
    save_data()
    return is_new


def is_blocked(user_id: int) -> bool:
    return user_id in BOT_DATA.get("blocked", [])


def is_link_blocked(url: str) -> bool:
    if url in BOT_DATA.get("blocked_links", []):
        return True
    for domain in BOT_DATA.get("blocked_domains", []):
        if domain.lower() in url.lower():
            return True
    return False


def check_rate_limit(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    limit = BOT_DATA["settings"].get("rate_limit_max", 20)
    window = BOT_DATA["settings"].get("rate_limit_window_seconds", 60)
    if limit <= 0:
        return True
    now = time.time()
    bucket = _rate_state.setdefault(user_id, [])
    while bucket and now - bucket[0] > window:
        bucket.pop(0)
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True


async def log_event(context: ContextTypes.DEFAULT_TYPE, text: str):
    """#14 — send a short line to the admin-configured logger channel, if any."""
    settings = BOT_DATA.get("settings", {})
    if not settings.get("logger_enabled") or not settings.get("logger_channel_id"):
        return
    try:
        await context.bot.send_message(chat_id=settings["logger_channel_id"], text=text)
    except Exception:
        log.exception("Failed to send to logger channel")


def track_sent_message(chat_id: int, message_id: int):
    """#5 — small per-chat ring buffer so 'Delete All Bot Messages' has something to work with."""
    key = str(chat_id)
    buf = BOT_DATA.setdefault("sent_messages", {}).setdefault(key, [])
    buf.append(message_id)
    if len(buf) > 200:
        del buf[: len(buf) - 200]


def bump_usage(uid: str):
    """v2 §3 — daily/monthly download counters, resetting on date/month change."""
    u = BOT_DATA["users"].get(uid)
    if not u:
        return
    today = datetime.utcnow().strftime("%Y-%m-%d")
    month = datetime.utcnow().strftime("%Y-%m")
    if u.get("downloads_today_date") != today:
        u["downloads_today"] = 0
        u["downloads_today_date"] = today
    if u.get("downloads_month_key") != month:
        u["downloads_month"] = 0
        u["downloads_month_key"] = month
    u["downloads_today"] += 1
    u["downloads_month"] += 1


def human_uptime() -> str:
    secs = int(time.time() - START_TIME)
    d, secs = divmod(secs, 86400)
    h, secs = divmod(secs, 3600)
    m, _ = divmod(secs, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    parts.append(f"{m}m")
    return " ".join(parts)


def get_memory_usage_mb():
    try:
        import psutil

        return round(psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024), 1)
    except Exception:
        try:
            import resource

            return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
        except Exception:
            return None


async def _delete_message_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    try:
        await context.bot.delete_message(chat_id=data["chat_id"], message_id=data["message_id"])
    except Exception:
        pass


async def schedule_delete(context, chat_id, message_id, seconds):
    if seconds and seconds > 0:
        context.job_queue.run_once(
            _delete_message_job, when=timedelta(seconds=seconds),
            data={"chat_id": chat_id, "message_id": message_id},
        )


# ----------------------------------------------------------------------------
# Dynamic menu engine — render_menu is the ONE function every command/callback
# uses to show a menu (#1, #2, #3). Same-message edit-in-place navigation.
# ----------------------------------------------------------------------------

def build_keyboard_from_buttons(buttons, menu_id):
    if not buttons:
        return None
    rows = {}
    for b in buttons:
        rows.setdefault(b.get("row", 1), []).append(b)
    kb_rows = []
    for row_num in sorted(rows.keys()):
        row_widgets = []
        for b in rows[row_num]:
            style = b.get("style")
            btype = b.get("type")
            if btype == "url":
                row_widgets.append(styled_button(b["label"], url=b["value"]))
            elif btype == "menu":
                row_widgets.append(styled_button(b["label"], callback_data=f"nav:{b['value']}", style=style or "primary"))
            elif btype == "toggle":
                current = bool(BOT_DATA["settings"].get(b["value"], False))
                st = "success" if current else "danger"
                row_widgets.append(
                    styled_button(b["label"], callback_data=f"tgl:{b['value']}:{menu_id}", style=st)
                )
            elif btype == "callback":
                row_widgets.append(styled_button(b["label"], callback_data=b["value"], style=style))
            else:
                continue
        if row_widgets:
            kb_rows.append(row_widgets)
    return InlineKeyboardMarkup(kb_rows) if kb_rows else None


async def render_menu(context: ContextTypes.DEFAULT_TYPE, chat_id: int, menu_id: str, existing_message=None, lang: str = None):
    menu = BOT_DATA["menus"].get(menu_id)
    if not menu:
        await context.bot.send_message(chat_id, f"⚠️ Menu '{menu_id}' nahi mila.")
        return

    # Resolve language: explicit arg > saved user preference > base (default) text.
    if lang is None:
        lang = BOT_DATA["users"].get(str(chat_id), {}).get("lang")
    translation = menu.get("translations", {}).get(lang) if lang else None

    buttons = (translation or {}).get("buttons") or menu.get("buttons", [])
    text = (translation or {}).get("text") or menu.get("text", "")
    kb = build_keyboard_from_buttons(buttons, menu_id)
    parse_mode = menu.get("parse_mode") or None
    image = menu.get("image_file_id")

    # #10 — owner/developer credit button, injected at render time (not part
    # of the admin-editable button list) so it can't be accidentally deleted
    # by editing menu buttons.
    if menu_id in ("start", "help_user"):
        owner_id = BOT_DATA["settings"].get("owner_display_user_id")
        if owner_id:
            label = BOT_DATA["settings"].get("owner_display_label") or "👑 Developer"
            owner_id_str = str(owner_id)
            url = f"tg://user?id={owner_id_str}" if owner_id_str.isdigit() else f"https://t.me/{owner_id_str.lstrip('@')}"
            owner_row = [styled_button(label, url=url)]
            kb = InlineKeyboardMarkup((kb.inline_keyboard if kb else []) + [owner_row])

    # #4 — global forwarding/sharing lock applies to every menu the bot sends.
    protect = bool(BOT_DATA["settings"].get("lock_all_content", False))

    try:
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    except Exception:
        pass

    sent_message = None
    try:
        if existing_message is not None:
            has_photo = bool(existing_message.photo)
            if image and has_photo:
                await existing_message.edit_media(
                    media=InputMediaPhoto(media=image, caption=text, parse_mode=parse_mode), reply_markup=kb
                )
                sent_message = existing_message
            elif image and not has_photo:
                await existing_message.delete()
                sent_message = await context.bot.send_photo(
                    chat_id, photo=image, caption=text, parse_mode=parse_mode, reply_markup=kb, protect_content=protect
                )
            elif not image and has_photo:
                await existing_message.delete()
                sent_message = await context.bot.send_message(
                    chat_id, text=text, parse_mode=parse_mode, reply_markup=kb, protect_content=protect
                )
            else:
                await existing_message.edit_text(text=text, parse_mode=parse_mode, reply_markup=kb)
                sent_message = existing_message
        else:
            if image:
                sent_message = await context.bot.send_photo(
                    chat_id, photo=image, caption=text, parse_mode=parse_mode, reply_markup=kb, protect_content=protect
                )
            else:
                sent_message = await context.bot.send_message(
                    chat_id, text=text, parse_mode=parse_mode, reply_markup=kb, protect_content=protect
                )
    except Exception:
        log.exception("render_menu failed for %s, sending fresh", menu_id)
        try:
            if image:
                sent_message = await context.bot.send_photo(
                    chat_id, photo=image, caption=text, parse_mode=parse_mode, reply_markup=kb, protect_content=protect
                )
            else:
                sent_message = await context.bot.send_message(
                    chat_id, text=text, parse_mode=parse_mode, reply_markup=kb, protect_content=protect
                )
        except Exception:
            log.exception("render_menu completely failed for %s", menu_id)
            return

    seconds = menu.get("auto_delete_seconds")
    if seconds is None:
        seconds = BOT_DATA["settings"].get("global_auto_delete_seconds", 0)
    if sent_message:
        track_sent_message(chat_id, sent_message.message_id)
        await schedule_delete(context, chat_id, sent_message.message_id, seconds)
    return sent_message


async def track_and_refresh_panel(context: ContextTypes.DEFAULT_TYPE, chat_id: int, key: str, sent_message):
    """v2 §11 — first-ever panel message stays forever; every later /start or
    /admin deletes the previous panel message and leaves only the fresh one."""
    if not sent_message:
        return
    key = f"{key}:{chat_id}"
    prev = BOT_DATA["panel_msg"].get(key)
    if prev and prev != sent_message.message_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=prev)
        except Exception:
            pass
    BOT_DATA["panel_msg"][key] = sent_message.message_id
    save_data()


async def cb_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, menu_id = query.data.split(":", 1)
    await render_menu(context, query.message.chat_id, menu_id, existing_message=query.message)


async def cb_toggle_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle button embedded inside a dynamic menu (#4 toggle-type button)."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    _, key, menu_id = query.data.split(":", 2)
    BOT_DATA["settings"][key] = not bool(BOT_DATA["settings"].get(key, False))
    save_data()
    await render_menu(context, query.message.chat_id, menu_id, existing_message=query.message)


# ----------------------------------------------------------------------------
# Style Text picker (#1) — reusable for menu body text AND button labels
# ----------------------------------------------------------------------------

async def send_style_preview(context, chat_id, source_text):
    rows = []
    for i, (label, func) in enumerate(STYLE_OPTIONS):
        preview = func(source_text)
        display = preview if len(preview) <= 30 else preview[:27] + "..."
        rows.append([styled_button(display, callback_data=f"styleset:{i}")])
    await context.bot.send_message(chat_id, "🅰️ Ek style choose karo:", reply_markup=InlineKeyboardMarkup(rows))


async def cb_styleset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    idx = int(query.data.split(":", 1)[1])
    src = context.user_data.pop("style_source_text", None)
    target = context.user_data.pop("style_target", None)
    if src is None or target is None:
        await query.edit_message_text("Session expire ho gayi, dobara try karo.")
        return
    label, func = STYLE_OPTIONS[idx]
    styled_text = func(src)

    if target.startswith("menu_text:"):
        menu_id = target.split(":", 1)[1]
        BOT_DATA["menus"][menu_id]["text"] = styled_text
        BOT_DATA["menus"][menu_id]["updated_by"] = update.effective_user.id
        BOT_DATA["menus"][menu_id]["updated_at"] = datetime.utcnow().isoformat()
        save_data()
        await query.edit_message_text(f"✅ Menu text updated ({label} style).")
    elif target.startswith("button_label:"):
        _, menu_id, idx_str = target.split(":", 2)
        BOT_DATA["menus"][menu_id]["buttons"][int(idx_str)]["label"] = styled_text
        save_data()
        await query.edit_message_text(f"✅ Button label updated ({label} style).")


# ----------------------------------------------------------------------------
# Basic user-facing commands
# ----------------------------------------------------------------------------

async def delete_incoming(update: Update):
    """#6 — best-effort cleanup of the user's own command message."""
    try:
        await update.message.delete()
    except Exception:
        pass


async def require_disclaimer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """PDF #1 — gate every command behind the disclaimer/agree flow.
    Returns True if the user may proceed; otherwise shows the disclaimer
    and returns False. Admins are exempt."""
    user_obj = update.effective_user
    if not user_obj:
        return True
    if is_admin(user_obj.id):
        return True
    uid = str(user_obj.id)
    if BOT_DATA["users"].get(uid, {}).get("accepted_terms"):
        return True
    await render_menu(context, update.effective_chat.id, "disclaimer")
    return False


LANG_NAMES = {
    "en": "🇬🇧 English", "hi": "🇮🇳 हिन्दी", "es": "🇪🇸 Español",
    "fr": "🇫🇷 Français", "ar": "🇸🇦 العربية", "pt": "🇵🇹 Português",
    "id": "🇮🇩 Indonesia", "bn": "🇧🇩 বাংলা", "ur": "🇵🇰 اردو",
}


def build_language_keyboard() -> InlineKeyboardMarkup:
    langs = BOT_DATA["settings"].get("languages", [])
    rows = [[styled_button("✨ Default (Hinglish)", callback_data="setlang:default")]]
    for code in langs:
        rows.append([styled_button(LANG_NAMES.get(code, code), callback_data=f"setlang:{code}")])
    return InlineKeyboardMarkup(rows)


async def show_post_onboarding(context: ContextTypes.DEFAULT_TYPE, chat_id: int, uid: str):
    """#1 — one-time language picker, shown before the welcome menu only the
    very first time a user reaches here (and only if the admin has actually
    configured extra languages — otherwise there's nothing to pick and we
    just fall through to the normal start menu). Shared by /start and by the
    disclaimer's 'I Agree & Continue' button so both paths land the user in
    the same place."""
    user = BOT_DATA["users"].get(uid, {})
    langs = BOT_DATA["settings"].get("languages", [])
    if not user.get("lang_prompted") and langs:
        user["lang_prompted"] = True
        save_data()
        sent = await context.bot.send_message(
            chat_id, "🌐 Welcome! Pick your language to get started:", reply_markup=build_language_keyboard()
        )
        return sent
    sent = await render_menu(context, chat_id, "start")
    if not BOT_DATA["users"].get(uid, {}).get("reply_kb_sent"):
        try:
            await context.bot.send_message(chat_id, "⌨️", reply_markup=main_reply_keyboard())
        except Exception:
            pass
        BOT_DATA["users"].setdefault(uid, {})["reply_kb_sent"] = True
        save_data()
    return sent


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_obj = update.effective_user
    if is_blocked(user_obj.id) and not is_admin(user_obj.id):
        return  # silently ignored, per spec
    is_new = touch_user(update)
    if not check_rate_limit(user_obj.id):
        await update.message.reply_text("⏳ Thoda slow karo, bahut jaldi jaldi requests aa rahi hain.")
        await delete_incoming(update)
        return
    if BOT_DATA["settings"].get("maintenance") and not is_admin(user_obj.id):
        await render_menu(context, update.effective_chat.id, "maintenance")
        await delete_incoming(update)
        return

    if not await require_disclaimer(update, context):
        await delete_incoming(update)
        return

    BOT_DATA["metrics"]["start_count"] = BOT_DATA["metrics"].get("start_count", 0) + 1
    if is_new:
        await log_event(
            context,
            f"👋 New user — {user_obj.id} (@{user_obj.username or 'no username'})",
        )
    save_data()

    sent = await show_post_onboarding(context, update.effective_chat.id, str(user_obj.id))
    await track_and_refresh_panel(context, update.effective_chat.id, "start", sent)
    await delete_incoming(update)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_obj = update.effective_user
    if is_blocked(user_obj.id) and not is_admin(user_obj.id):
        return
    touch_user(update)
    if not await require_disclaimer(update, context):
        await delete_incoming(update)
        return
    menu_id = "help_admin" if is_admin(user_obj.id) else "help_user"
    await render_menu(context, update.effective_chat.id, menu_id)
    await delete_incoming(update)


async def cmd_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_obj = update.effective_user
    if is_blocked(user_obj.id) and not is_admin(user_obj.id):
        return
    touch_user(update)
    if not await require_disclaimer(update, context):
        await delete_incoming(update)
        return
    langs = BOT_DATA["settings"].get("languages", [])
    if not langs:
        await update.message.reply_text("Abhi koi extra language configure nahi hui hai.")
        await delete_incoming(update)
        return
    await update.message.reply_text("🌐 Apni language choose karo:", reply_markup=build_language_keyboard())
    await delete_incoming(update)


async def cb_setlang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    code = query.data.split(":", 1)[1]
    uid = str(update.effective_user.id)
    if uid in BOT_DATA["users"]:
        BOT_DATA["users"][uid]["lang"] = None if code == "default" else code
        # Belt-and-suspenders: a selection from any source means the picker
        # has now been shown/handled for this user.
        BOT_DATA["users"][uid]["lang_prompted"] = True
        save_data()
    await render_menu(context, query.message.chat_id, "start", existing_message=query.message)


async def cb_agree_terms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """PDF #1 — 'I Agree & Continue' button on the disclaimer screen."""
    query = update.callback_query
    await query.answer()
    uid = str(update.effective_user.id)
    if uid in BOT_DATA["users"]:
        BOT_DATA["users"][uid]["accepted_terms"] = True
        BOT_DATA["users"][uid]["accepted_terms_at"] = datetime.utcnow().isoformat()
        save_data()
    try:
        await query.message.delete()
    except Exception:
        pass
    await show_post_onboarding(context, query.message.chat_id, uid)


# ----------------------------------------------------------------------------
# Reel download
# ----------------------------------------------------------------------------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_obj = update.effective_user
    if is_blocked(user_obj.id) and not is_admin(user_obj.id):
        return  # silently ignored, per spec

    # v2 §6 — an admin replying (in the admin group / their DM) to a forwarded
    # ticket message routes straight back to that user, bypassing everything else.
    if update.message.reply_to_message and is_admin(user_obj.id):
        tid = BOT_DATA["ticket_msg_map"].get(str(update.message.reply_to_message.message_id))
        if tid:
            await handle_admin_ticket_reply(update, context, tid)
            return

    touch_user(update)
    user_id = user_obj.id
    uid = str(user_id)

    # v2 §6 — while a user has an open ticket, every message they send auto-
    # forwards into it (no command/button needed).
    open_tid = BOT_DATA["users"].get(uid, {}).get("open_ticket_id")
    if open_tid and str(open_tid) in BOT_DATA["tickets"] and BOT_DATA["tickets"][str(open_tid)]["status"] == "open":
        await forward_to_ticket(update, context, open_tid)
        return

    text = update.message.text or ""

    # v2 §1 — persistent reply-keyboard routing
    if text == RKB_DOWNLOAD:
        await update.message.reply_text("🔗 " + to_small_caps("paste your instagram reel link here"))
        return
    if text == RKB_USAGE:
        await show_usage_screen(update, context)
        return
    if text == RKB_GIFT:
        await show_gift_menu(update, context)
        return
    if text == RKB_LANGUAGE:
        await cmd_language(update, context)
        return
    if text == RKB_DEVELOPER:
        await show_developer_button(update, context)
        return
    if text == RKB_HOWTO:
        await update.message.reply_text(STR["how_to_use"])
        return
    if text == RKB_SUPPORT:
        await support_button_entry(update, context)
        return

    awaiting = context.user_data.get("awaiting")

    # PDF #3 / #11 — user-facing text-collection flows (copyright report,
    # support message) run regardless of admin status, before the
    # admin-only dispatcher below.
    if awaiting in (
        "support_message", "copyright_report_link", "copyright_report_details",
        "ticket_new", "gift_stars_custom_amount", "gift_upi_amount",
    ):
        await handle_user_awaiting_input(update, context, awaiting)
        return

    if awaiting and is_admin(user_id):
        await handle_admin_text_input(update, context, awaiting)
        return

    if not await require_disclaimer(update, context):
        return

    if not check_rate_limit(user_id):
        await update.message.reply_text("⏳ Thoda slow karo, bahut jaldi jaldi requests aa rahi hain.")
        return

    match = INSTAGRAM_URL_RE.search(text)

    if not match:
        # #15 — simple keyword auto-reply
        low = text.lower()
        for phrase, reply in BOT_DATA["settings"].get("auto_replies", {}).items():
            if phrase in low:
                await update.message.reply_text(reply)
                return
        await update.message.reply_text(
            "Ye Instagram reel link jaisa nahi lag raha. Ek valid reel link bhejo, jaise:\n"
            "https://www.instagram.com/reel/XXXXXXXX/"
        )
        return

    if BOT_DATA["settings"].get("maintenance") and not is_admin(user_id):
        await render_menu(context, update.effective_chat.id, "maintenance")
        return

    url = match.group(1)

    if is_link_blocked(url) and not is_admin(user_id):
        await update.message.reply_text("🚫 Ye link ya domain admin ne block kar diya hai.")
        return

    status_msg = await update.message.reply_text(STR["processing"])
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_video")
    except Exception:
        pass

    out_template = os.path.join(DOWNLOAD_DIR, f"%(id)s_{int(time.time())}.%(ext)s")

    def build_ydl_opts(use_merge: bool) -> dict:
        opts = {
            "format": "bestvideo+bestaudio/best" if use_merge else "best",
            "outtmpl": out_template,
            "quiet": True,
            "no_warnings": True,
        }
        if use_merge:
            opts["merge_output_format"] = "mp4"
            if FFMPEG_PATH:
                opts["ffmpeg_location"] = FFMPEG_PATH
        return opts

    def run_download(use_merge: bool):
        opts = build_ydl_opts(use_merge)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            fp = ydl.prepare_filename(info)
            if not fp.endswith(".mp4") and os.path.exists(fp.rsplit(".", 1)[0] + ".mp4"):
                fp = fp.rsplit(".", 1)[0] + ".mp4"
            ig_caption = (info.get("description") or "").strip()
            uploader = (info.get("uploader") or info.get("uploader_id") or "").strip()
            return fp, ig_caption, uploader

    file_path = None
    try:
        try:
            file_path, ig_caption, ig_uploader = run_download(use_merge=FFMPEG_AVAILABLE)
        except Exception as e:
            # Self-heal: if a merge was attempted and ffmpeg turned out to be
            # the problem, retry once with a no-merge (progressive) format.
            if "ffmpeg" in str(e).lower():
                log.warning("Merge failed (ffmpeg issue), retrying with progressive format.")
                file_path, ig_caption, ig_uploader = run_download(use_merge=False)
            else:
                raise

        uid = str(update.effective_user.id)
        lang = BOT_DATA["users"].get(uid, {}).get("lang")
        menu = BOT_DATA["menus"]["reel_result"]
        translation = menu.get("translations", {}).get(lang) if lang else None
        base_caption = (translation or {}).get("text") or menu.get("text", "")
        buttons = (translation or {}).get("buttons") or menu.get("buttons", [])
        kb = build_keyboard_from_buttons(buttons, "reel_result")
        parse_mode = menu.get("parse_mode") or None

        # v2 §9 — native Telegram blockquote with extra reel info, HTML only.
        if parse_mode == "HTML":
            import html as _html
            preview = ig_caption[:300] + ("…" if len(ig_caption) > 300 else "")
            bq = (
                "<blockquote expandable>"
                f"📋 Caption: {_html.escape(preview) or '(none)'}\n"
                f"👤 Uploader: {_html.escape(ig_uploader) or 'n/a'}"
                "</blockquote>"
            )
            result_caption = f"{base_caption}\n\n{bq}"
        else:
            result_caption = base_caption

        protect = bool(BOT_DATA["settings"].get("lock_all_content", False))
        with open(file_path, "rb") as vid:
            sent = await update.message.reply_video(
                video=vid, caption=result_caption, parse_mode=parse_mode, reply_markup=kb, protect_content=protect
            )

        # Cache the real Instagram caption so the "Get Caption" button under
        # THIS specific video can show it, keyed to this exact message.
        _caption_cache[(sent.chat_id, sent.message_id)] = ig_caption
        if len(_caption_cache) > CAPTION_CACHE_MAX:
            _caption_cache.pop(next(iter(_caption_cache)))

        track_sent_message(sent.chat_id, sent.message_id)
        bump_usage(uid)
        BOT_DATA["metrics"]["reels_downloaded"] = BOT_DATA["metrics"].get("reels_downloaded", 0) + 1
        save_data()
        await log_event(
            context,
            f"📥 New download — user {user_id} (@{user_obj.username or 'no username'}) — {url}",
        )

        seconds = menu.get("auto_delete_seconds")
        if seconds is None:
            seconds = BOT_DATA["settings"].get("global_auto_delete_seconds", 0)
        await schedule_delete(context, sent.chat_id, sent.message_id, seconds)
        await status_msg.delete()
    except Exception as e:  # noqa: BLE001
        log.exception("Download failed")
        await status_msg.edit_text(f"❌ Download fail ho gaya: {e}")
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass


async def cb_get_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key = (query.message.chat_id, query.message.message_id)
    caption = _caption_cache.get(key)
    if not caption:
        await query.message.reply_text("ℹ️ Is post ka koi caption nahi mila (ya cache expire ho gaya).")
        return
    # Telegram message limit is 4096 chars — split if needed.
    for i in range(0, len(caption), 4000):
        await query.message.reply_text(caption[i:i + 4000])


async def cb_download_another(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("🔗 Paste your next Instagram reel link.")


# ----------------------------------------------------------------------------
# v2 §6 — Livegram-style support ticket system
# ----------------------------------------------------------------------------

async def create_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE) -> dict:
    uid = str(update.effective_user.id)
    tid = BOT_DATA["next_ticket_id"]
    BOT_DATA["next_ticket_id"] += 1
    ticket = {
        "id": tid, "user_id": update.effective_user.id, "status": "open",
        "created_at": datetime.utcnow().isoformat(), "closed_at": None,
    }
    BOT_DATA["tickets"][str(tid)] = ticket
    BOT_DATA["users"].setdefault(uid, {})["open_ticket_id"] = tid
    save_data()
    return ticket


async def post_ticket_card(context: ContextTypes.DEFAULT_TYPE, ticket: dict, user_obj):
    group_id = BOT_DATA["settings"].get("admin_group_id")
    targets = [group_id] if group_id else BOT_DATA.get("admins", [])
    card_text = f"👤 {user_obj.full_name} | 🆔 {user_obj.id} | 🎫 #{ticket['id']} | Status: 🟢 Open"
    kb = InlineKeyboardMarkup([[
        styled_button("✅ Close Ticket", callback_data=f"tk_close:{ticket['id']}", style="danger"),
        styled_button("🔁 Reopen", callback_data=f"tk_reopen:{ticket['id']}", style="success"),
    ]])
    for target in targets:
        if not target:
            continue
        try:
            sent = await context.bot.send_message(chat_id=target, text=card_text, reply_markup=kb)
            BOT_DATA["ticket_msg_map"][str(sent.message_id)] = str(ticket["id"])
        except Exception:
            pass
    save_data()


async def forward_to_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE, ticket_id: int):
    group_id = BOT_DATA["settings"].get("admin_group_id")
    targets = [group_id] if group_id else BOT_DATA.get("admins", [])
    for target in targets:
        if not target:
            continue
        try:
            copied = await context.bot.copy_message(
                chat_id=target, from_chat_id=update.effective_chat.id, message_id=update.message.message_id
            )
            BOT_DATA["ticket_msg_map"][str(copied.message_id)] = str(ticket_id)
        except Exception:
            pass
    save_data()


async def handle_admin_ticket_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, tid: str):
    ticket = BOT_DATA["tickets"].get(tid)
    if not ticket or ticket["status"] != "open":
        return
    try:
        await context.bot.copy_message(
            chat_id=ticket["user_id"], from_chat_id=update.effective_chat.id, message_id=update.message.message_id
        )
    except Exception:
        pass


async def support_button_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = str(user.id)
    user_rec = BOT_DATA["users"].setdefault(uid, {})
    open_tid = user_rec.get("open_ticket_id")
    target_msg = update.callback_query.message if update.callback_query else update.message
    if open_tid and str(open_tid) in BOT_DATA["tickets"] and BOT_DATA["tickets"][str(open_tid)]["status"] == "open":
        await target_msg.reply_text(f"🎫 Ticket #{open_tid} already open — just send your message here.")
        return
    context.user_data["awaiting"] = "ticket_new"
    await target_msg.reply_text(STR["support_prompt"])


async def cb_ticket_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    tid = query.data.split(":", 1)[1]
    ticket = BOT_DATA["tickets"].get(tid)
    if not ticket:
        return
    ticket["status"] = "closed"
    ticket["closed_at"] = datetime.utcnow().isoformat()
    uid = str(ticket["user_id"])
    if BOT_DATA["users"].get(uid, {}).get("open_ticket_id") == int(tid):
        BOT_DATA["users"][uid]["open_ticket_id"] = None
    save_data()
    try:
        await context.bot.send_message(chat_id=ticket["user_id"], text=STR["ticket_closed"](tid))
    except Exception:
        pass
    try:
        await query.edit_message_reply_markup(
            InlineKeyboardMarkup([[styled_button("🔁 Reopen", callback_data=f"tk_reopen:{tid}", style="success")]])
        )
    except Exception:
        pass
    await log_event(context, f"🎫 Ticket #{tid} closed by {update.effective_user.id}")


async def cb_ticket_reopen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    tid = query.data.split(":", 1)[1]
    ticket = BOT_DATA["tickets"].get(tid)
    if not ticket:
        return
    ticket["status"] = "open"
    ticket["closed_at"] = None
    uid = str(ticket["user_id"])
    BOT_DATA["users"].setdefault(uid, {})["open_ticket_id"] = int(tid)
    save_data()
    try:
        await query.edit_message_reply_markup(
            InlineKeyboardMarkup([[styled_button("✅ Close Ticket", callback_data=f"tk_close:{tid}", style="danger")]])
        )
    except Exception:
        pass
    await log_event(context, f"🎫 Ticket #{tid} reopened by {update.effective_user.id}")


# ----------------------------------------------------------------------------
# v2 §3 — My usage
# ----------------------------------------------------------------------------

async def show_usage_screen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    u = BOT_DATA["users"].setdefault(uid, {})
    today = datetime.utcnow().strftime("%Y-%m-%d")
    month = datetime.utcnow().strftime("%Y-%m")
    today_count = u.get("downloads_today", 0) if u.get("downloads_today_date") == today else 0
    month_count = u.get("downloads_month", 0) if u.get("downloads_month_key") == month else 0
    limit = BOT_DATA["settings"].get("daily_limit", 20)
    plan = u.get("plan", "Free")
    pct = min(100, int((today_count / limit) * 100)) if limit else 0
    filled = pct // 10
    bar = "▓" * filled + "░" * (10 - filled)
    now = datetime.utcnow()
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    remaining = tomorrow - now
    hh, mm = remaining.seconds // 3600, (remaining.seconds % 3600) // 60
    text = (
        f"{STR['usage_title']}\n\n"
        f"📥 {to_small_caps('today')}: {today_count}/{limit} {to_small_caps('downloads')}\n"
        f"📅 {to_small_caps('this month')}: {month_count} {to_small_caps('downloads')}\n"
        f"⚡ {to_small_caps('plan')}: {plan}\n"
        f"⏳ {to_small_caps('resets in')}: {hh}ʜ {mm}ᴍ\n\n"
        f"{bar} {pct}%"
    )
    kb_rows = []
    if BOT_DATA["settings"].get("premium_enabled"):
        kb_rows.append([styled_button(to_small_caps("🚀 upgrade for more"), callback_data="gift_menu", style="success")])
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    except Exception:
        pass
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb_rows) if kb_rows else None)


# ----------------------------------------------------------------------------
# v2 §5 — Developer button
# ----------------------------------------------------------------------------

def resolve_developer_url() -> str | None:
    link = BOT_DATA["settings"].get("developer_link")
    if link:
        return link
    dev_id = BOT_DATA["settings"].get("developer_id")
    if dev_id:
        return f"tg://user?id={dev_id}"
    return None


async def show_developer_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = resolve_developer_url()
    if not url:
        await update.message.reply_text(to_small_caps("developer contact not set up yet."))
        return
    kb = InlineKeyboardMarkup([[styled_button("👨‍💻 " + to_small_caps("message developer"), url=url)]])
    await update.message.reply_text(to_small_caps("tap below to message the developer:"), reply_markup=kb)


# ----------------------------------------------------------------------------
# v2 §4 — Gift flow (Telegram Stars + UPI)
# ----------------------------------------------------------------------------

async def show_gift_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb_rows = [[styled_button("⭐ Send Stars", callback_data="gift_stars", style="success")]]
    if BOT_DATA["settings"].get("upi_id"):
        kb_rows.append([styled_button("💳 Pay via UPI", callback_data="gift_upi", style="primary")])
    target = update.callback_query.message if update.callback_query else update.message
    await target.reply_text("🎁 " + to_small_caps("send a gift — pick a method:"), reply_markup=InlineKeyboardMarkup(kb_rows))


async def cb_gift_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await show_gift_menu(update, context)


async def cb_gift_stars(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kb = InlineKeyboardMarkup([
        [styled_button("50⭐", callback_data="gift_stars_amt:50"), styled_button("100⭐", callback_data="gift_stars_amt:100")],
        [styled_button("250⭐", callback_data="gift_stars_amt:250"), styled_button("500⭐", callback_data="gift_stars_amt:500")],
        [styled_button("✏️ Enter custom amount", callback_data="gift_stars_custom")],
    ])
    await query.message.reply_text("⭐ " + to_small_caps("choose an amount:"), reply_markup=kb)


async def cb_gift_stars_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["awaiting"] = "gift_stars_custom_amount"
    await query.message.reply_text("Numeric ⭐ amount type karo (e.g. 150).")


async def send_stars_invoice(context: ContextTypes.DEFAULT_TYPE, chat_id: int, amount: int):
    await context.bot.send_invoice(
        chat_id=chat_id,
        title="Gift the developer ⭐",
        description=f"Send {amount} Telegram Stars as a gift.",
        payload=f"stars_gift:{amount}:{chat_id}:{int(time.time())}",
        provider_token="",  # not used for XTR
        currency="XTR",
        prices=[LabeledPrice(label=f"{amount} Stars", amount=amount)],
    )


async def cb_gift_stars_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    amount = int(query.data.split(":", 1)[1])
    await send_stars_invoice(context, query.message.chat_id, amount)


async def cmd_precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)


async def cmd_successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sp = update.message.successful_payment
    await update.message.reply_text(f"✅ " + to_small_caps(f"thanks for the {sp.total_amount}⭐ gift!"))
    await log_event(context, f"⭐ Gift received — {sp.total_amount} stars from {update.effective_user.id}")


async def cb_gift_upi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not BOT_DATA["settings"].get("upi_id"):
        await query.message.reply_text("UPI abhi configure nahi hai.")
        return
    context.user_data["awaiting"] = "gift_upi_amount"
    await query.message.reply_text("💳 Amount (₹) type karo:")


async def start_upi_order(update: Update, context: ContextTypes.DEFAULT_TYPE, amount: int):
    upi_id = BOT_DATA["settings"].get("upi_id")
    oid = str(BOT_DATA["next_gift_id"])
    BOT_DATA["next_gift_id"] += 1
    expires_at = time.time() + 600  # 10 minutes
    order = {
        "id": oid, "user_id": update.effective_user.id, "amount": amount,
        "expires_at": expires_at, "status": "pending",
    }
    BOT_DATA["gift_orders"][oid] = order
    save_data()
    upi_uri = f"upi://pay?pa={upi_id}&am={amount}&cu=INR&tn=Gift%20Order%20{oid}"
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={quote(upi_uri)}"
    kb = InlineKeyboardMarkup([[styled_button("✅ I've Paid", callback_data=f"gift_upi_paid:{oid}", style="success")]])
    msg = await context.bot.send_photo(
        chat_id=update.effective_chat.id, photo=qr_url,
        caption=f"💳 ₹{amount} — {to_small_caps('scan to pay via upi')}\n⏳ expires in 10:00",
        reply_markup=kb,
    )
    context.job_queue.run_repeating(
        upi_countdown_job, interval=20, first=20,
        data={"oid": oid, "chat_id": msg.chat_id, "message_id": msg.message_id},
        name=f"upi_countdown_{oid}",
    )


async def upi_countdown_job(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    oid, chat_id, message_id = job.data["oid"], job.data["chat_id"], job.data["message_id"]
    order = BOT_DATA["gift_orders"].get(oid)
    if not order or order["status"] != "pending":
        job.schedule_removal()
        return
    remaining = int(order["expires_at"] - time.time())
    if remaining <= 0:
        order["status"] = "expired"
        save_data()
        try:
            kb = InlineKeyboardMarkup([[styled_button("🔁 Generate New QR", callback_data="gift_upi", style="primary")]])
            await context.bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption="❌ QR expired, generate new one", reply_markup=kb)
        except Exception:
            pass
        job.schedule_removal()
        return
    mm, ss = remaining // 60, remaining % 60
    try:
        await context.bot.edit_message_caption(
            chat_id=chat_id, message_id=message_id,
            caption=f"💳 ₹{order['amount']} — {to_small_caps('scan to pay via upi')}\n⏳ expires in {mm:02d}:{ss:02d}",
            reply_markup=InlineKeyboardMarkup([[styled_button("✅ I've Paid", callback_data=f"gift_upi_paid:{oid}", style="success")]]),
        )
    except Exception:
        pass


async def cb_gift_upi_paid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    oid = query.data.split(":", 1)[1]
    order = BOT_DATA["gift_orders"].get(oid)
    if not order:
        return
    if order["expires_at"] < time.time():
        await query.message.reply_text("❌ QR expired, generate new one via 🎁 Send a gift.")
        return
    order["status"] = "claimed_pending_verify"
    save_data()
    await query.message.reply_text("✅ Marked as paid — an admin will verify shortly.")
    targets = BOT_DATA.get("admins", [])
    for target in targets:
        try:
            await context.bot.send_message(target, f"💳 UPI order #{oid} — ₹{order['amount']} — user {order['user_id']} claims paid.")
        except Exception:
            pass


# ----------------------------------------------------------------------------
# PDF #3 / #11 — Copyright report + Support flows (user-facing, not admin-only)
# ----------------------------------------------------------------------------

async def cb_report_copyright(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["awaiting"] = "copyright_report_link"
    await query.message.reply_text(
        "🚫 Report Copyright Issue\n\nPlease paste the link to the content you believe infringes your copyright."
    )


async def cb_support_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["awaiting"] = "support_message"
    await query.message.reply_text("🆘 Support\n\nDescribe your issue and we'll forward it to the team.")


async def handle_user_awaiting_input(update: Update, context: ContextTypes.DEFAULT_TYPE, awaiting: str):
    text = (update.message.text or "").strip()
    user_obj = update.effective_user

    if awaiting == "support_message":
        context.user_data.pop("awaiting", None)
        payload = f"🆘 Support message from {user_obj.id} (@{user_obj.username or 'no username'}):\n\n{text}"
        support_chat_id = BOT_DATA["settings"].get("support_chat_id")
        targets = [support_chat_id] if support_chat_id else BOT_DATA.get("admins", [])
        for target in targets:
            if not target:
                continue
            try:
                await context.bot.send_message(chat_id=target, text=payload)
            except Exception:
                pass
        await log_event(context, f"🆘 Support message from {user_obj.id}")
        await update.message.reply_text("✅ Your message has been sent to support, we'll get back to you soon.")

    elif awaiting == "ticket_new":
        context.user_data.pop("awaiting", None)
        ticket = await create_ticket(update, context)
        await update.message.reply_text(STR["ticket_created"](ticket["id"]))
        await post_ticket_card(context, ticket, user_obj)
        await forward_to_ticket(update, context, ticket["id"])
        await log_event(context, f"🎫 Ticket #{ticket['id']} opened by {user_obj.id}")

    elif awaiting == "gift_stars_custom_amount":
        context.user_data.pop("awaiting", None)
        if not text.isdigit() or int(text) <= 0:
            await update.message.reply_text("Valid ⭐ number bhejo.")
            return
        await send_stars_invoice(context, update.effective_chat.id, int(text))

    elif awaiting == "gift_upi_amount":
        context.user_data.pop("awaiting", None)
        if not text.isdigit() or int(text) <= 0:
            await update.message.reply_text("Valid ₹ amount bhejo.")
            return
        await start_upi_order(update, context, int(text))

    elif awaiting == "copyright_report_link":
        context.user_data["report_link_draft"] = text
        context.user_data["awaiting"] = "copyright_report_details"
        await update.message.reply_text(
            "Thanks. Now briefly describe your ownership / proof of rights (or paste a link to proof)."
        )

    elif awaiting == "copyright_report_details":
        context.user_data.pop("awaiting", None)
        link = context.user_data.pop("report_link_draft", "")
        report = {
            "id": len(BOT_DATA["copyright_reports"]) + 1,
            "reporter_id": user_obj.id,
            "reporter_username": user_obj.username,
            "link": link,
            "details": text,
            "at": datetime.utcnow().isoformat(),
            "status": "open",
        }
        BOT_DATA["copyright_reports"].append(report)
        save_data()
        await update.message.reply_text(
            "✅ Thanks — your report has been received and will be reviewed and acted upon promptly."
        )

        domain = None
        try:
            from urllib.parse import urlparse
            domain = urlparse(link).netloc or None
        except Exception:
            domain = None

        alert_lines = [
            f"🚫 New copyright report #{report['id']}",
            f"From: {user_obj.id} (@{user_obj.username or 'no username'})",
            f"Link: {link or '(none given)'}",
            f"Details: {text[:500]}",
        ]
        kb_rows = []
        if link:
            kb_rows.append([styled_button("🚫 Block This Link", callback_data=f"adm_block_link:{report['id']}", style="danger")])
        if domain:
            kb_rows.append([styled_button(f"🚫 Block Domain ({domain})", callback_data=f"adm_block_domain:{report['id']}", style="danger")])
        kb = InlineKeyboardMarkup(kb_rows) if kb_rows else None

        support_chat_id = BOT_DATA["settings"].get("support_chat_id")
        targets = [support_chat_id] if support_chat_id else BOT_DATA.get("admins", [])
        for target in targets:
            if not target:
                continue
            try:
                await context.bot.send_message(chat_id=target, text="\n".join(alert_lines), reply_markup=kb)
            except Exception:
                pass
        await log_event(context, f"🚫 Copyright report #{report['id']} filed against {link or '(no link)'}")


async def cb_adm_block_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    report_id = int(query.data.split(":", 1)[1])
    report = next((r for r in BOT_DATA["copyright_reports"] if r["id"] == report_id), None)
    if not report or not report.get("link"):
        await query.message.reply_text("⚠️ Report/link not found.")
        return
    link = report["link"]
    if link not in BOT_DATA["blocked_links"]:
        BOT_DATA["blocked_links"].append(link)
    report["status"] = "link_blocked"
    save_data()
    await query.message.reply_text(f"✅ Blocked link: {link}")
    await log_event(context, f"🚫 Admin blocked link: {link}")


async def cb_adm_block_domain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    report_id = int(query.data.split(":", 1)[1])
    report = next((r for r in BOT_DATA["copyright_reports"] if r["id"] == report_id), None)
    if not report or not report.get("link"):
        await query.message.reply_text("⚠️ Report/link not found.")
        return
    try:
        from urllib.parse import urlparse
        domain = urlparse(report["link"]).netloc
    except Exception:
        domain = None
    if not domain:
        await query.message.reply_text("⚠️ Couldn't parse a domain from that link.")
        return
    if domain not in BOT_DATA["blocked_domains"]:
        BOT_DATA["blocked_domains"].append(domain)
    report["status"] = "domain_blocked"
    save_data()
    await query.message.reply_text(f"✅ Blocked domain: {domain}")
    await log_event(context, f"🚫 Admin blocked domain: {domain}")


# ----------------------------------------------------------------------------
# Admin panel — top level (#9 categorized, functional dispatcher — not a
# content menu, since these are actions, not editable copy)
# ----------------------------------------------------------------------------

def admin_panel_keyboard():
    return InlineKeyboardMarkup(
        [
            [styled_button("💎 Premium Plans", callback_data="adm_premium", style="primary"),
             styled_button("💳 UPI Settings", callback_data="adm_upi", style="primary")],
            [styled_button("👨‍💻 Developer Settings", callback_data="adm_devsettings", style="primary"),
             styled_button("🎧 Support Settings", callback_data="adm_support_settings", style="primary")],
            [styled_button("🎫 Tickets", callback_data="adm_tickets", style="success"),
             styled_button("📊 Bot Stats", callback_data="adm_stats", style="success")],
            [styled_button("🌐 Language Settings", callback_data="adm_lang_manage", style="primary"),
             styled_button("📢 Broadcast", callback_data="adm_broadcast", style="primary")],
            [styled_button("👥 Users & Groups", callback_data="adm_users", style="primary"),
             styled_button("🎨 Menu & UI", callback_data="adm_menu_ui", style="primary")],
            [styled_button("⚙️ Settings & Admins", callback_data="adm_settings", style="primary"),
             styled_button("🛑 Danger Zone", callback_data="adm_danger", style="danger")],
        ]
    )


def back_row(cb="adm_back", label="🔙 Back"):
    return [styled_button(label, callback_data=cb)]


def home_row():
    """#3 — extra row shown only on top-level category screens, alongside
    the regular (stack-aware) 🔙 Back row."""
    return [styled_button("🏠 Admin Home", callback_data="adm_home")]


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    context.user_data["adm_nav_stack"] = ["adm_home"]
    sent = await update.message.reply_text("🛠️ Admin Panel", reply_markup=admin_panel_keyboard())
    await track_and_refresh_panel(context, update.effective_chat.id, "admin", sent)
    await delete_incoming(update)


async def _render_adm_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("🛠️ Admin Panel", reply_markup=admin_panel_keyboard())


async def cb_adm_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["adm_nav_stack"] = ["adm_home"]
    await _render_adm_home(update, context)


# ---- #3 — generic back-stack navigation --------------------------------------
# Screens registered here can be reached via the stack-aware "adm_back"
# button regardless of how deep the user has drilled in. Leaf actions (add /
# remove / toggle / confirm) intentionally aren't part of this table — they
# fall back to a hardcoded parent, same as before.
SCREEN_RENDERERS = {}  # populated just above build_app, once every screen fn exists


def nav_tracked(screen_key):
    """Wraps a screen's callback handler so entering it gets pushed onto the
    per-admin nav stack, so 'Back' can unwind through however many screens
    were visited, not just to a single hardcoded parent."""
    def deco(fn):
        async def wrapped(update, context):
            stack = context.user_data.setdefault("adm_nav_stack", ["adm_home"])
            if not stack or stack[-1] != screen_key:
                stack.append(screen_key)
                if len(stack) > 15:
                    del stack[0]
            return await fn(update, context)
        return wrapped
    return deco


async def cb_adm_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    stack = context.user_data.setdefault("adm_nav_stack", ["adm_home"])
    if len(stack) > 1:
        stack.pop()  # drop the screen we're currently on
    target = stack[-1] if stack else "adm_home"
    renderer = SCREEN_RENDERERS.get(target)
    if renderer is None:
        stack[:] = ["adm_home"]
        renderer = SCREEN_RENDERERS["adm_home"]
    await renderer(update, context)


# ---- Stats & Activity -------------------------------------------------------

async def _render_adm_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    col = get_mongo_collection()
    backend = "MongoDB ✅" if col is not None else "Local JSON file (fallback)"
    if col is None and _mongo_last_error and MONGO_URI:
        backend += f"\n   ⚠️ Mongo error: {_mongo_last_error}"
    mem = get_memory_usage_mb()
    text = (
        "📊 Stats & Activity\n\n"
        f"👥 Users: {len(BOT_DATA['users'])}\n"
        f"👨‍👩‍👧 Groups: {len(BOT_DATA['groups'])}\n"
        f"🚫 Blocked: {len(BOT_DATA['blocked'])}\n"
        f"📢 Broadcasts sent: {len(BOT_DATA['broadcast_log'])}\n"
        f"⬇️ Reels downloaded: {BOT_DATA['metrics'].get('reels_downloaded', 0)}\n"
        f"🚀 /start count: {BOT_DATA['metrics'].get('start_count', 0)}\n"
        f"🚫 Copyright reports: {len(BOT_DATA['copyright_reports'])}\n"
        f"⏱ Uptime: {human_uptime()}\n"
        f"💾 Memory: {mem if mem is not None else 'n/a'} MB\n"
        f"🗄 Storage backend: {backend}\n"
    )
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([back_row(), home_row()]))


async def cb_adm_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _render_adm_stats(update, context)


# ---- Users & Groups ----------------------------------------------------------

async def _render_adm_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    kb = InlineKeyboardMarkup(
        [
            [styled_button("📋 List Users (last 20)", callback_data="adm_users_list")],
            [styled_button("✉️ Message a User", callback_data="adm_users_msg")],
            back_row(),
            home_row(),
        ]
    )
    await query.edit_message_text("👥 Users & Groups", reply_markup=kb)


async def cb_adm_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _render_adm_users(update, context)


async def cb_adm_users_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    users = list(BOT_DATA["users"].items())[-20:]
    if not users:
        text = "Koi user record nahi mila abhi."
    else:
        lines = ["📋 Last 20 Users\n"]
        for uid, info in users:
            uname = f"@{info.get('username')}" if info.get("username") else "(no username)"
            lines.append(f"• {uid} — {info.get('name')} {uname}")
        text = "\n".join(lines)
    kb = InlineKeyboardMarkup([[styled_button("🔙 Back", callback_data="adm_users")]])
    await query.edit_message_text(text, reply_markup=kb)


async def cb_adm_users_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["awaiting"] = "message_user_id"
    await query.message.reply_text("User ki ID bhejo jisko message karna hai.")


# ---- Broadcast ---------------------------------------------------------------

async def _render_adm_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    protect = BOT_DATA["settings"].get("protect_broadcasts", True)
    kb = InlineKeyboardMarkup(
        [
            [styled_button("📢 New Broadcast", callback_data="adm_bc_new", style="primary")],
            [styled_button(
                f"🔐 Forward-Lock: {'ON' if protect else 'OFF'}",
                callback_data="stgl:protect_broadcasts:adm_broadcast",
                style="success" if protect else "danger",
            )],
            [styled_button("📜 Broadcast Log", callback_data="adm_bc_log")],
            back_row(),
            home_row(),
        ]
    )
    await query.edit_message_text("📢 Broadcast", reply_markup=kb)


async def cb_adm_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _render_adm_broadcast(update, context)


async def cb_adm_bc_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["awaiting"] = "broadcast_content"
    await query.message.reply_text(
        "Ab jo bhi bhejna hai (text/photo/video) — ek message mein bhejo. "
        "Forward-lock current setting ke hisaab se apply hoga."
    )


async def cb_adm_bc_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    entries = BOT_DATA["broadcast_log"][-10:]
    if not entries:
        text = "Abhi tak koi broadcast nahi bheja gaya."
    else:
        lines = ["📜 Last 10 Broadcasts\n"]
        for e in entries:
            lines.append(f"• {e['at']} — {e['recipients']} users ko bheja gaya")
        text = "\n".join(lines)
    kb = InlineKeyboardMarkup([[styled_button("🔙 Back", callback_data="adm_broadcast")]])
    await query.edit_message_text(text, reply_markup=kb)


async def do_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    # #4 — the master "lock everything" switch ORs together with the
    # broadcast-specific forward-lock toggle.
    protect = BOT_DATA["settings"].get("protect_broadcasts", True) or BOT_DATA["settings"].get("lock_all_content", False)
    sent = 0
    failed = 0
    for uid in list(BOT_DATA["users"].keys()):
        try:
            copied = await context.bot.copy_message(
                chat_id=int(uid), from_chat_id=msg.chat_id, message_id=msg.message_id,
                protect_content=protect,
            )
            track_sent_message(int(uid), copied.message_id)
            await schedule_delete(context, int(uid), copied.message_id, BOT_DATA["settings"].get("global_auto_delete_seconds", 0))
            sent += 1
        except Exception:
            failed += 1
    BOT_DATA["broadcast_log"].append(
        {"by": update.effective_user.id, "at": datetime.utcnow().isoformat(), "recipients": sent}
    )
    BOT_DATA["metrics"]["broadcasts_sent"] = BOT_DATA["metrics"].get("broadcasts_sent", 0) + 1
    save_data()
    await update.message.reply_text(
        f"✅ Broadcast bhej diya.\nSent: {sent} | Failed: {failed}\nForward-lock: {'ON' if protect else 'OFF'}"
    )
    await log_event(context, f"📢 Broadcast sent by {update.effective_user.id} — {sent} recipients")


# ---- Menu & UI (#1, #2, #4, #7 controls) -------------------------------------

async def _render_adm_menu_ui(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    rows = [[styled_button(f"📝 {mid}", callback_data=f"adm_menu_edit:{mid}")] for mid in BOT_DATA["menus"]]
    rows.append(back_row())
    rows.append(home_row())
    await query.edit_message_text("🎨 Menu & UI — kaun sa menu edit karna hai?", reply_markup=InlineKeyboardMarkup(rows))


async def cb_adm_menu_ui(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _render_adm_menu_ui(update, context)


async def cb_adm_menu_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    menu_id = query.data.split(":", 1)[1]
    menu = BOT_DATA["menus"][menu_id]
    parse_mode_label = menu.get("parse_mode") or "OFF (raw text)"
    override = menu.get("auto_delete_seconds")
    override_label = f"{override}s" if override is not None else "uses global"
    kb = InlineKeyboardMarkup(
        [
            [styled_button("✏️ Edit Text", callback_data=f"adm_menu_txt:{menu_id}")],
            [styled_button("🅰️ Style Text", callback_data=f"adm_menu_style:{menu_id}")],
            [styled_button(f"🔤 Parse Mode: {parse_mode_label}", callback_data=f"adm_menu_parsemode:{menu_id}")],
            [styled_button("🖼️ Set Image", callback_data=f"adm_menu_img:{menu_id}"),
             styled_button("🗑️ Remove Image", callback_data=f"adm_menu_rmimg:{menu_id}")],
            [styled_button("🔘 Manage Buttons", callback_data=f"adm_menu_btns:{menu_id}")],
            [styled_button(f"⏱ Auto-Delete: {override_label}", callback_data=f"adm_menu_autodel:{menu_id}")],
            [styled_button("🌐 Translations", callback_data=f"adm_menu_trans:{menu_id}")],
            [styled_button("🔙 Back", callback_data="adm_menu_ui")],
        ]
    )
    await query.edit_message_text(f"📝 Editing: {menu_id}", reply_markup=kb)


async def cb_adm_menu_trans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    menu_id = query.data.split(":", 1)[1]
    langs = BOT_DATA["settings"].get("languages", [])
    if not langs:
        await query.message.reply_text(
            "Pehle Settings & Admins → 🌐 Manage Languages se kam se kam ek language add karo."
        )
        return
    rows = []
    have = BOT_DATA["menus"][menu_id].get("translations", {})
    for code in langs:
        mark = "✅" if code in have else "➕"
        rows.append([styled_button(f"{mark} {LANG_NAMES.get(code, code)}", callback_data=f"adm_menu_trans_edit:{menu_id}:{code}")])
    rows.append([styled_button("🔙 Back", callback_data=f"adm_menu_edit:{menu_id}")])
    await query.edit_message_text(f"🌐 Translations for {menu_id}", reply_markup=InlineKeyboardMarkup(rows))


async def cb_adm_menu_trans_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, menu_id, code = query.data.split(":", 2)
    context.user_data["awaiting"] = f"menu_trans_text:{menu_id}:{code}"
    await query.message.reply_text(
        f"'{menu_id}' ka {LANG_NAMES.get(code, code)} translation text bhejo "
        "(buttons wahi rahenge jo base menu mein hain, translate nahi honge)."
    )


async def cb_adm_menu_txt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    menu_id = query.data.split(":", 1)[1]
    context.user_data["awaiting"] = f"menu_text:{menu_id}"
    await query.message.reply_text(
        "Naya text bhejo — jo bhi bhejoge, exactly wahi save hoga (koi auto-reformat nahi hota)."
    )


async def cb_adm_menu_style(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    menu_id = query.data.split(":", 1)[1]
    context.user_data["awaiting"] = f"menu_style_source:{menu_id}"
    await query.message.reply_text("Plain text bhejo, main usko alag-alag styles mein preview dikhaunga.")


async def cb_adm_menu_parsemode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    menu_id = query.data.split(":", 1)[1]
    cycle = [None, "HTML", "MarkdownV2"]
    current = BOT_DATA["menus"][menu_id].get("parse_mode")
    nxt = cycle[(cycle.index(current) + 1) % len(cycle)] if current in cycle else None
    BOT_DATA["menus"][menu_id]["parse_mode"] = nxt
    save_data()
    await cb_adm_menu_edit(update, context)


async def cb_adm_menu_img(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    menu_id = query.data.split(":", 1)[1]
    context.user_data["awaiting"] = f"menu_image:{menu_id}"
    await query.message.reply_text("Photo bhejo (sirf image, video nahi).")


async def cb_adm_menu_rmimg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    menu_id = query.data.split(":", 1)[1]
    BOT_DATA["menus"][menu_id]["image_file_id"] = None
    save_data()
    await query.message.reply_text("✅ Image hata di gayi, ab text-only menu hai.")
    await cb_adm_menu_edit(update, context)


async def cb_adm_menu_autodel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    menu_id = query.data.split(":", 1)[1]
    context.user_data["awaiting"] = f"menu_autodel:{menu_id}"
    await query.message.reply_text(
        "Seconds bhejo is menu ke liye (0 = never, ya 'global' likho global default use karne ke liye)."
    )


async def cb_adm_menu_btns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    menu_id = query.data.split(":", 1)[1]
    buttons = BOT_DATA["menus"][menu_id]["buttons"]
    rows = []
    for i, b in enumerate(buttons):
        rows.append([
            styled_button(f"{i}: {b['label']}", callback_data=f"noop"),
            styled_button("🅰️", callback_data=f"adm_btn_style:{menu_id}:{i}"),
            styled_button("❌", callback_data=f"adm_btn_del:{menu_id}:{i}", style="danger"),
        ])
    rows.append([styled_button("➕ Add Button", callback_data=f"adm_btn_add:{menu_id}", style="success")])
    rows.append([styled_button("🔙 Back", callback_data=f"adm_menu_edit:{menu_id}")])
    await query.edit_message_text(f"🔘 Buttons for {menu_id}", reply_markup=InlineKeyboardMarkup(rows))


async def cb_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


async def cb_adm_btn_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, menu_id, idx = query.data.split(":", 2)
    idx = int(idx)
    if 0 <= idx < len(BOT_DATA["menus"][menu_id]["buttons"]):
        BOT_DATA["menus"][menu_id]["buttons"].pop(idx)
        save_data()
    await cb_adm_menu_btns_by_id(update, context, menu_id)


async def cb_adm_btn_style(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, menu_id, idx = query.data.split(":", 2)
    label = BOT_DATA["menus"][menu_id]["buttons"][int(idx)]["label"]
    context.user_data["style_source_text"] = label
    context.user_data["style_target"] = f"button_label:{menu_id}:{idx}"
    await send_style_preview(context, query.message.chat_id, label)


async def cb_adm_menu_btns_by_id(update, context, menu_id):
    """Helper to redraw the buttons list after a delete, without a fresh query.data."""
    buttons = BOT_DATA["menus"][menu_id]["buttons"]
    rows = []
    for i, b in enumerate(buttons):
        rows.append([
            styled_button(f"{i}: {b['label']}", callback_data="noop"),
            styled_button("🅰️", callback_data=f"adm_btn_style:{menu_id}:{i}"),
            styled_button("❌", callback_data=f"adm_btn_del:{menu_id}:{i}", style="danger"),
        ])
    rows.append([styled_button("➕ Add Button", callback_data=f"adm_btn_add:{menu_id}", style="success")])
    rows.append([styled_button("🔙 Back", callback_data=f"adm_menu_edit:{menu_id}")])
    await update.callback_query.edit_message_text(f"🔘 Buttons for {menu_id}", reply_markup=InlineKeyboardMarkup(rows))


async def cb_adm_btn_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    menu_id = query.data.split(":", 1)[1]
    context.user_data["btn_flow"] = {"menu_id": menu_id, "data": {}}
    context.user_data["awaiting"] = "btn_step_label"
    await query.message.reply_text("Naye button ka label bhejo (emoji use kar sakte ho).")


async def cb_btn_type_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, btype = query.data.split(":", 1)
    flow = context.user_data.get("btn_flow")
    if not flow:
        await query.message.reply_text("Session expire ho gayi, dobara /admin se try karo.")
        return
    flow["data"]["type"] = btype
    context.user_data["awaiting"] = "btn_step_value"
    prompts = {
        "menu": "Kaunse menu_id pe le jana hai? (jaise: start, help_user)",
        "url": "URL bhejo (https:// se shuru).",
        "callback": "Internal action ka callback_data bhejo (jaise: adm_stats).",
        "toggle": "Settings key bhejo jise toggle karna hai (jaise: maintenance).",
    }
    await query.message.reply_text(prompts.get(btype, "Value bhejo:"))


# ---- Settings & Admins --------------------------------------------------------

async def _render_adm_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    s = BOT_DATA["settings"]
    kb = InlineKeyboardMarkup(
        [
            [styled_button("🖼 Set Welcome Image", callback_data="adm_menu_img:start")],
            [styled_button(
                f"🔒 Maintenance: {'ON' if s.get('maintenance') else 'OFF'}",
                callback_data="stgl:maintenance:adm_settings",
                style="danger" if s.get("maintenance") else "success",
            )],
            [styled_button(f"⏱ Global Auto-Delete: {s.get('global_auto_delete_seconds', 0)}s", callback_data="adm_set_autodelete")],
            [styled_button(
                f"🅰️ Small-Caps Buttons: {'ON' if s.get('small_caps_buttons_default') else 'OFF'}",
                callback_data="stgl:small_caps_buttons_default:adm_settings",
                style="success" if s.get("small_caps_buttons_default") else "danger",
            )],
            [styled_button("💬 Auto-Replies", callback_data="adm_autoreply_list")],
            [styled_button("🌐 Manage Languages", callback_data="adm_lang_manage")],
            [styled_button("👤 Manage Admins", callback_data="adm_manage_admins")],
            [styled_button("📥 Restore Backup", callback_data="adm_restore_info")],
            [styled_button(
                f"🔐 Lock All Forwarding: {'ON' if s.get('lock_all_content') else 'OFF'}",
                callback_data="stgl:lock_all_content:adm_settings",
                style="success" if s.get("lock_all_content") else "danger",
            )],
            [styled_button("👑 Owner/Developer Contact", callback_data="adm_owner_contact")],
            [styled_button("📋 Logger Channel", callback_data="adm_logger_channel")],
            back_row(),
            home_row(),
        ]
    )
    await query.edit_message_text("⚙️ Settings & Admins", reply_markup=kb)


async def cb_adm_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _render_adm_settings(update, context)


# ---- Owner/Developer credit button (#10) --------------------------------------

async def _render_adm_owner_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    s = BOT_DATA["settings"]
    current = s.get("owner_display_user_id")
    label = s.get("owner_display_label") or "👑 Developer"
    text = (
        "👑 Owner/Developer Contact\n\n"
        f"Current target: {current or '(not set)'}\n"
        f"Button label: {label}\n\n"
        "This shows a display/credit button on Start & Help — it does NOT "
        "grant that user any bot-admin permissions."
    )
    kb = InlineKeyboardMarkup(
        [
            [styled_button("✏️ Set Contact", callback_data="adm_owner_contact_set", style="success")],
            [styled_button("❌ Clear", callback_data="adm_owner_contact_clear", style="danger")],
            back_row(),
        ]
    )
    await query.edit_message_text(text, reply_markup=kb)


async def cb_adm_owner_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _render_adm_owner_contact(update, context)


async def cb_adm_owner_contact_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["awaiting"] = "owner_contact_label"
    await query.message.reply_text("Button label bhejo (e.g. '👑 Developer' or '💬 Contact Us').")


async def cb_adm_owner_contact_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    BOT_DATA["settings"]["owner_display_user_id"] = None
    BOT_DATA["settings"]["owner_display_label"] = None
    save_data()
    await query.edit_message_text("✅ Owner/Developer contact button cleared.", reply_markup=InlineKeyboardMarkup([back_row()]))


# ---- Logger channel (#14) ------------------------------------------------------

async def _render_adm_logger_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    s = BOT_DATA["settings"]
    text = (
        "📋 Logger Channel\n\n"
        f"Channel ID: {s.get('logger_channel_id') or '(not set)'}\n"
        f"Enabled: {'ON' if s.get('logger_enabled') else 'OFF'}\n\n"
        "Logs new users, downloads, broadcasts, admin changes, copyright "
        "reports, and errors here."
    )
    kb = InlineKeyboardMarkup(
        [
            [styled_button("✏️ Set Channel", callback_data="adm_logger_channel_set")],
            [styled_button(
                f"🔀 Enabled: {'ON' if s.get('logger_enabled') else 'OFF'}",
                callback_data="stgl:logger_enabled:adm_logger_channel",
                style="success" if s.get("logger_enabled") else "danger",
            )],
            back_row(),
        ]
    )
    await query.edit_message_text(text, reply_markup=kb)


async def cb_adm_logger_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _render_adm_logger_channel(update, context)


async def cb_adm_logger_channel_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["awaiting"] = "logger_channel_id"
    await query.message.reply_text(
        "Forward any message from the target channel here (bot must be an "
        "admin there), or just type its numeric ID (looks like -100xxxxxxxxxx)."
    )


async def cb_settings_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    _, key, return_to = query.data.split(":", 2)
    BOT_DATA["settings"][key] = not bool(BOT_DATA["settings"].get(key, False))
    save_data()
    if return_to == "adm_settings":
        await _render_adm_settings(update, context)
    elif return_to == "adm_broadcast":
        await _render_adm_broadcast(update, context)
    elif return_to == "adm_logger_channel":
        await _render_adm_logger_channel(update, context)


async def cb_adm_lang_manage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    langs = BOT_DATA["settings"].get("languages", [])
    text = "🌐 Enabled Languages\n\n" + ("\n".join(f"• {LANG_NAMES.get(c, c)}" for c in langs) if langs else "Koi nahi — sirf Default (Hinglish).")
    kb = InlineKeyboardMarkup(
        [
            [styled_button("➕ Add Language", callback_data="adm_lang_add", style="success")],
            [styled_button("➖ Remove Language", callback_data="adm_lang_remove", style="danger")],
            [styled_button("🔙 Back", callback_data="adm_settings")],
        ]
    )
    await query.edit_message_text(text, reply_markup=kb)


async def cb_adm_lang_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    available = [c for c in LANG_NAMES if c not in BOT_DATA["settings"].get("languages", [])]
    if not available:
        await query.message.reply_text("Saari suggested languages already add ho chuki hain.")
        return
    rows = [[styled_button(LANG_NAMES[c], callback_data=f"adm_lang_add_do:{c}")] for c in available]
    await query.message.reply_text("Kaunsi language add karni hai?", reply_markup=InlineKeyboardMarkup(rows))


async def cb_adm_lang_add_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    code = query.data.split(":", 1)[1]
    if code not in BOT_DATA["settings"]["languages"]:
        BOT_DATA["settings"]["languages"].append(code)
        save_data()
    await query.edit_message_text(f"✅ {LANG_NAMES.get(code, code)} add ho gayi. Ab har menu mein 🌐 Translations se text daal sakte ho.")


async def cb_adm_lang_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    langs = BOT_DATA["settings"].get("languages", [])
    if not langs:
        await query.message.reply_text("Koi language add nahi hai abhi.")
        return
    rows = [[styled_button(LANG_NAMES.get(c, c), callback_data=f"adm_lang_remove_do:{c}")] for c in langs]
    await query.message.reply_text("Kaunsi language remove karni hai?", reply_markup=InlineKeyboardMarkup(rows))


async def cb_adm_lang_remove_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    code = query.data.split(":", 1)[1]
    if code in BOT_DATA["settings"]["languages"]:
        BOT_DATA["settings"]["languages"].remove(code)
        save_data()
    await query.edit_message_text(f"✅ {LANG_NAMES.get(code, code)} remove ho gayi.")


async def cb_adm_set_autodelete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["awaiting"] = "global_autodelete"
    await query.message.reply_text("Global auto-delete kitne seconds ka ho (0 = disable)?")


async def cb_adm_autoreply_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    replies = BOT_DATA["settings"].get("auto_replies", {})
    lines = ["💬 Auto-Replies\n"] + [f"• `{k}` → {v[:30]}" for k, v in replies.items()] or ["Koi auto-reply set nahi hai."]
    kb = InlineKeyboardMarkup(
        [
            [styled_button("➕ Add", callback_data="adm_autoreply_add", style="success")],
            [styled_button("❌ Remove", callback_data="adm_autoreply_del")],
            [styled_button("🔙 Back", callback_data="adm_settings")],
        ]
    )
    await query.edit_message_text("\n".join(lines), reply_markup=kb)


async def cb_adm_autoreply_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["awaiting"] = "autoreply_key"
    await query.message.reply_text("Trigger keyword/phrase bhejo (jab message mein ye aayega tab reply jayega).")


async def cb_adm_autoreply_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["awaiting"] = "autoreply_delkey"
    await query.message.reply_text("Kaunsa keyword hatana hai, wo bhejo.")


async def cb_adm_manage_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    admins = BOT_DATA.get("admins", [])
    text = "👤 Current Admins\n\n" + "\n".join(f"• {a}" for a in admins)
    kb = InlineKeyboardMarkup(
        [
            [styled_button("➕ Add Admin", callback_data="adm_add_admin", style="success")],
            [styled_button("➖ Remove Admin", callback_data="adm_remove_admin", style="danger")],
            [styled_button("🔙 Back", callback_data="adm_settings")],
        ]
    )
    await query.edit_message_text(text, reply_markup=kb)


async def cb_adm_add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_owner(update.effective_user.id):
        await query.message.reply_text("Sirf owner hi admin add kar sakta hai.")
        return
    context.user_data["awaiting"] = "add_admin_id"
    await query.message.reply_text("Naye admin ki user ID bhejo.")


async def cb_adm_remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_owner(update.effective_user.id):
        await query.message.reply_text("Sirf owner hi admin remove kar sakta hai.")
        return
    context.user_data["awaiting"] = "remove_admin_id"
    await query.message.reply_text("Hatane wale admin ki user ID bhejo.")


async def cb_adm_restore_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "📥 Restore Backup\n\nMujhe DM mein seedha .json backup file bhej do (jo /database se export hui thi). "
        "Pehle current data ki auto-backup lunga, fir confirmation dikhaunga."
    )


# ---- Danger Zone --------------------------------------------------------------

async def _render_adm_danger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    kb = InlineKeyboardMarkup(
        [
            [styled_button("🧹 Clear Broadcast Log", callback_data="adm_clear_bclog", style="danger")],
            [styled_button("🧹 Delete All Bot Messages In This Chat", callback_data="adm_delete_chat_msgs", style="danger")],
            [styled_button("🔄 Reset Menus to Default", callback_data="adm_reset_menus_confirm", style="danger")],
            [styled_button("❌ Reset ALL Bot Data", callback_data="adm_reset_confirm", style="danger")],
            back_row(),
            home_row(),
        ]
    )
    await query.edit_message_text("🛑 Danger Zone\n(Ye actions destructive hain.)", reply_markup=kb)


# ---- v2 §8 new admin screens: Premium / UPI / Developer / Support / Tickets --

async def _render_adm_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    s = BOT_DATA["settings"]
    text = (
        f"💎 Premium Plans\n\nEnabled: {'ON' if s.get('premium_enabled') else 'OFF'}\n"
        f"Daily free limit: {s.get('daily_limit', 20)}"
    )
    kb = InlineKeyboardMarkup([
        [styled_button(f"🔀 Premium: {'ON' if s.get('premium_enabled') else 'OFF'}",
                        callback_data="stgl:premium_enabled:adm_premium",
                        style="success" if s.get("premium_enabled") else "danger")],
        [styled_button("✏️ Set Daily Limit", callback_data="adm_set_dailylimit")],
        back_row(), home_row(),
    ])
    await query.edit_message_text(text, reply_markup=kb)


async def cb_adm_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _render_adm_premium(update, context)


async def cb_adm_set_dailylimit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["awaiting"] = "daily_limit"
    await query.message.reply_text("Naya daily free-download limit (number) bhejo.")


async def _render_adm_upi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    upi = BOT_DATA["settings"].get("upi_id")
    kb = InlineKeyboardMarkup([
        [styled_button("✏️ Set UPI ID", callback_data="adm_upi_set")],
        [styled_button("❌ Clear", callback_data="adm_upi_clear", style="danger")],
        back_row(), home_row(),
    ])
    await query.edit_message_text(f"💳 UPI Settings\n\nCurrent: {upi or '(not set)'}", reply_markup=kb)


async def cb_adm_upi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _render_adm_upi(update, context)


async def cb_adm_upi_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["awaiting"] = "upi_id"
    await query.message.reply_text("UPI ID bhejo (e.g. name@bank).")


async def cb_adm_upi_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    BOT_DATA["settings"]["upi_id"] = None
    save_data()
    await query.edit_message_text("✅ UPI ID cleared.", reply_markup=InlineKeyboardMarkup([back_row()]))


async def _render_adm_devsettings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    s = BOT_DATA["settings"]
    text = (
        f"👨‍💻 Developer Settings\n\nNumeric ID: {s.get('developer_id') or '(not set)'}\n"
        f"Link override: {s.get('developer_link') or '(not set)'}"
    )
    kb = InlineKeyboardMarkup([
        [styled_button("✏️ Set Numeric ID", callback_data="adm_dev_id")],
        [styled_button("✏️ Set Link Override", callback_data="adm_dev_link")],
        back_row(), home_row(),
    ])
    await query.edit_message_text(text, reply_markup=kb)


async def cb_adm_devsettings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _render_adm_devsettings(update, context)


async def cb_adm_dev_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["awaiting"] = "developer_id"
    await query.message.reply_text("Developer ki numeric user ID bhejo.")


async def cb_adm_dev_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["awaiting"] = "developer_link"
    await query.message.reply_text("t.me/username link bhejo (ya 'clear' likh do hatane ke liye).")


async def _render_adm_support_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    gid = BOT_DATA["settings"].get("admin_group_id")
    kb = InlineKeyboardMarkup([
        [styled_button("✏️ Set Ticket Group", callback_data="adm_group_set")],
        back_row(), home_row(),
    ])
    await query.edit_message_text(
        f"🎧 Support Settings\n\nTicket group: {gid or '(not set — falls back to admin DMs)'}", reply_markup=kb
    )


async def cb_adm_support_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _render_adm_support_settings(update, context)


async def cb_adm_group_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["awaiting"] = "admin_group_id"
    await query.message.reply_text(
        "Forward any message from the ticket group here (bot must be admin there), "
        "or type its numeric ID (-100...)."
    )


async def _render_adm_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    tickets = list(BOT_DATA["tickets"].values())
    open_n = sum(1 for t in tickets if t["status"] == "open")
    lines = [f"🎫 Tickets — {open_n} open / {len(tickets)} total\n"]
    for t in tickets[-15:]:
        icon = "🟢" if t["status"] == "open" else "🔴"
        lines.append(f"#{t['id']} — user {t['user_id']} — {icon} {t['status']}")
    kb = InlineKeyboardMarkup([back_row(), home_row()])
    await query.edit_message_text("\n".join(lines), reply_markup=kb)


async def cb_adm_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _render_adm_tickets(update, context)


async def cb_adm_danger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _render_adm_danger(update, context)


async def cb_adm_delete_chat_msgs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """#5 — wipes only the chat the admin runs this from (Telegram allows
    deleting messages up to 48h old; older ones fail silently)."""
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    ids = BOT_DATA.get("sent_messages", {}).pop(str(chat_id), [])
    save_data()
    deleted = 0
    for mid in ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
            deleted += 1
        except Exception:
            pass
    try:
        await query.message.delete()
    except Exception:
        pass
    await context.bot.send_message(chat_id, f"✅ Deleted {deleted}/{len(ids)} tracked bot messages in this chat.")


async def cb_adm_clear_bclog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    BOT_DATA["broadcast_log"] = []
    save_data()
    await query.message.reply_text("✅ Broadcast log clear ho gaya.")


async def cb_adm_reset_menus_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kb = InlineKeyboardMarkup(
        [[styled_button("⚠️ Haan, reset karo", callback_data="adm_reset_menus_do", style="danger"),
          styled_button("Cancel", callback_data="adm_danger")]]
    )
    await query.edit_message_text("⚠️ Sab menus (text/image/buttons) default pe reset ho jayenge. Pakka?", reply_markup=kb)


async def cb_adm_reset_menus_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    make_backup_snapshot(reason="pre_menu_reset")
    BOT_DATA["menus"] = json.loads(json.dumps(DEFAULT_MENUS))
    save_data()
    await query.edit_message_text("✅ Menus default pe reset ho gaye.")


async def cb_adm_reset_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kb = InlineKeyboardMarkup(
        [[styled_button("⚠️ Haan, sab reset karo", callback_data="adm_reset_do", style="danger"),
          styled_button("Cancel", callback_data="adm_danger")]]
    )
    await query.edit_message_text(
        "⚠️ Pakka? Ye SAARA bot data (users, settings, menus, sab) delete kar dega.\nEk auto-backup pehle le li jayegi.",
        reply_markup=kb,
    )


async def cb_adm_reset_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_DATA
    query = update.callback_query
    await query.answer()
    make_backup_snapshot(reason="pre_reset")
    BOT_DATA = json.loads(json.dumps(DEFAULT_DATA))
    save_data()
    await query.edit_message_text("✅ Reset ho gaya. Purana data backup mein safe hai.")


# ----------------------------------------------------------------------------
# Text-input flows triggered from admin panel buttons
# ----------------------------------------------------------------------------

async def handle_admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE, awaiting: str):
    text = (update.message.text or "").strip()

    if awaiting.startswith("menu_text:"):
        menu_id = awaiting.split(":", 1)[1]
        context.user_data.pop("awaiting", None)
        menu = BOT_DATA["menus"][menu_id]
        if menu.get("image_file_id") and len(text) > 1024:
            await update.message.reply_text(
                f"⚠️ Ye menu image ke saath hai, caption limit 1024 characters hai, tumhara text {len(text)} hai. "
                "Chhota karo ya pehle image hatao."
            )
            return
        menu["text"] = text
        menu["updated_by"] = update.effective_user.id
        menu["updated_at"] = datetime.utcnow().isoformat()
        save_data()
        await update.message.reply_text(f"✅ '{menu_id}' text update ho gaya.")

    elif awaiting.startswith("menu_style_source:"):
        menu_id = awaiting.split(":", 1)[1]
        context.user_data.pop("awaiting", None)
        context.user_data["style_source_text"] = text
        context.user_data["style_target"] = f"menu_text:{menu_id}"
        await send_style_preview(context, update.effective_chat.id, text)

    elif awaiting.startswith("menu_autodel:"):
        menu_id = awaiting.split(":", 1)[1]
        context.user_data.pop("awaiting", None)
        if text.lower() == "global":
            BOT_DATA["menus"][menu_id]["auto_delete_seconds"] = None
        elif text.isdigit():
            BOT_DATA["menus"][menu_id]["auto_delete_seconds"] = int(text)
        else:
            await update.message.reply_text("Number bhejo, ya 'global' likho.")
            context.user_data["awaiting"] = awaiting
            return
        save_data()
        await update.message.reply_text(f"✅ Auto-delete override set for '{menu_id}'.")

    elif awaiting.startswith("menu_trans_text:"):
        _, menu_id, code = awaiting.split(":", 2)
        context.user_data.pop("awaiting", None)
        menu = BOT_DATA["menus"][menu_id]
        menu.setdefault("translations", {})
        menu["translations"][code] = {"text": text, "buttons": None}
        save_data()
        await update.message.reply_text(f"✅ '{menu_id}' ka {LANG_NAMES.get(code, code)} translation save ho gaya.")

    elif awaiting == "global_autodelete":
        context.user_data.pop("awaiting", None)
        if not text.isdigit():
            await update.message.reply_text("Sirf number bhejo.")
            context.user_data["awaiting"] = awaiting
            return
        BOT_DATA["settings"]["global_auto_delete_seconds"] = int(text)
        save_data()
        await update.message.reply_text(f"✅ Global auto-delete set to {text}s.")

    elif awaiting == "autoreply_key":
        context.user_data["autoreply_key_draft"] = text.lower()
        context.user_data["awaiting"] = "autoreply_value"
        await update.message.reply_text("Ab reply text bhejo.")

    elif awaiting == "autoreply_value":
        context.user_data.pop("awaiting", None)
        key = context.user_data.pop("autoreply_key_draft", None)
        if key:
            BOT_DATA["settings"]["auto_replies"][key] = text
            save_data()
            await update.message.reply_text(f"✅ Auto-reply set for '{key}'.")

    elif awaiting == "autoreply_delkey":
        context.user_data.pop("awaiting", None)
        BOT_DATA["settings"]["auto_replies"].pop(text.lower(), None)
        save_data()
        await update.message.reply_text("✅ Agar wo keyword tha, hata diya gaya.")

    elif awaiting == "add_admin_id":
        context.user_data.pop("awaiting", None)
        if not text.isdigit():
            await update.message.reply_text("Valid numeric user ID bhejo.")
            return
        new_id = int(text)
        if new_id not in BOT_DATA["admins"]:
            BOT_DATA["admins"].append(new_id)
            save_data()
        await update.message.reply_text(f"✅ {new_id} ab admin hai.")
        await log_event(context, f"👤 Admin added: {new_id} (by {update.effective_user.id})")

    elif awaiting == "remove_admin_id":
        context.user_data.pop("awaiting", None)
        if not text.isdigit():
            await update.message.reply_text("Valid numeric user ID bhejo.")
            return
        rem_id = int(text)
        if rem_id in BOT_DATA["admins"]:
            BOT_DATA["admins"].remove(rem_id)
            save_data()
        await update.message.reply_text(f"✅ {rem_id} admin list se hata diya.")
        await log_event(context, f"👤 Admin removed: {rem_id} (by {update.effective_user.id})")

    elif awaiting == "owner_contact_label":
        context.user_data["owner_contact_label_draft"] = text
        context.user_data["awaiting"] = "owner_contact_id"
        await update.message.reply_text("Ab user ID ya @username bhejo (jispe button point karega).")

    elif awaiting == "owner_contact_id":
        context.user_data.pop("awaiting", None)
        label = context.user_data.pop("owner_contact_label_draft", "👑 Developer")
        BOT_DATA["settings"]["owner_display_label"] = label
        BOT_DATA["settings"]["owner_display_user_id"] = text.lstrip("@")
        save_data()
        await update.message.reply_text(f"✅ Owner/Developer contact set: {label} → {text}")

    elif awaiting == "logger_channel_id":
        context.user_data.pop("awaiting", None)
        chat_id = None
        forward_origin = getattr(update.message, "forward_origin", None)
        if forward_origin is not None:
            chat_obj = getattr(forward_origin, "chat", None)
            if chat_obj is not None:
                chat_id = chat_obj.id
        if chat_id is None:
            legacy_fwd = getattr(update.message, "forward_from_chat", None)
            if legacy_fwd is not None:
                chat_id = legacy_fwd.id
        if chat_id is None and text.lstrip("-").isdigit():
            chat_id = int(text)
        if chat_id is None:
            await update.message.reply_text(
                "Channel detect nahi hua. Ya to channel se ek message forward karo, "
                "ya numeric ID (-100...) type karo."
            )
            context.user_data["awaiting"] = "logger_channel_id"
            return
        BOT_DATA["settings"]["logger_channel_id"] = chat_id
        BOT_DATA["settings"]["logger_enabled"] = True
        save_data()
        await update.message.reply_text(f"✅ Logger channel set to {chat_id} and enabled.")

    elif awaiting == "daily_limit":
        context.user_data.pop("awaiting", None)
        if not text.isdigit():
            await update.message.reply_text("Valid number bhejo.")
            return
        BOT_DATA["settings"]["daily_limit"] = int(text)
        save_data()
        await update.message.reply_text(f"✅ Daily limit set to {text}.")

    elif awaiting == "upi_id":
        context.user_data.pop("awaiting", None)
        BOT_DATA["settings"]["upi_id"] = text
        save_data()
        await update.message.reply_text(f"✅ UPI ID set: {text}")

    elif awaiting == "developer_id":
        context.user_data.pop("awaiting", None)
        if not text.isdigit():
            await update.message.reply_text("Valid numeric ID bhejo.")
            return
        BOT_DATA["settings"]["developer_id"] = int(text)
        save_data()
        await update.message.reply_text(f"✅ Developer ID set: {text}")

    elif awaiting == "developer_link":
        context.user_data.pop("awaiting", None)
        BOT_DATA["settings"]["developer_link"] = None if text.lower() == "clear" else text
        save_data()
        await update.message.reply_text("✅ Developer link updated.")

    elif awaiting == "admin_group_id":
        context.user_data.pop("awaiting", None)
        chat_id = None
        forward_origin = getattr(update.message, "forward_origin", None)
        if forward_origin is not None:
            chat_obj = getattr(forward_origin, "chat", None)
            if chat_obj is not None:
                chat_id = chat_obj.id
        if chat_id is None:
            legacy_fwd = getattr(update.message, "forward_from_chat", None)
            if legacy_fwd is not None:
                chat_id = legacy_fwd.id
        if chat_id is None and text.lstrip("-").isdigit():
            chat_id = int(text)
        if chat_id is None:
            await update.message.reply_text("Group detect nahi hua. Forward a message ya numeric ID type karo.")
            context.user_data["awaiting"] = "admin_group_id"
            return
        BOT_DATA["settings"]["admin_group_id"] = chat_id
        save_data()
        await update.message.reply_text(f"✅ Ticket group set to {chat_id}.")

    elif awaiting == "message_user_id":
        if not text.isdigit():
            await update.message.reply_text("Valid numeric user ID bhejo.")
            return
        context.user_data["awaiting"] = "message_user_body"
        context.user_data["message_target"] = int(text)
        await update.message.reply_text("Ab wo message bhejo jo is user ko bhejna hai.")

    elif awaiting == "message_user_body":
        context.user_data.pop("awaiting", None)
        target = context.user_data.pop("message_target", None)
        if target is None:
            await update.message.reply_text("Kuch galat ho gaya, dobara try karo.")
            return
        try:
            await context.bot.send_message(chat_id=target, text=text)
            await update.message.reply_text("✅ Message bhej diya.")
        except Exception as e:  # noqa: BLE001
            await update.message.reply_text(f"❌ Bhej nahi paya: {e}")

    elif awaiting == "broadcast_content":
        context.user_data.pop("awaiting", None)
        await do_broadcast(update, context)

    elif awaiting == "btn_step_label":
        flow = context.user_data.get("btn_flow")
        if not flow:
            context.user_data.pop("awaiting", None)
            return
        label = text
        if BOT_DATA["settings"].get("small_caps_buttons_default", True):
            label = to_small_caps(label)
        flow["data"]["label"] = label
        context.user_data["awaiting"] = None
        kb = InlineKeyboardMarkup(
            [
                [styled_button("📄 Open Menu", callback_data="btntype:menu")],
                [styled_button("🔗 URL Link", callback_data="btntype:url")],
                [styled_button("⚙️ Run Action", callback_data="btntype:callback")],
                [styled_button("🔀 Toggle Setting", callback_data="btntype:toggle")],
            ]
        )
        await update.message.reply_text("Button kis type ka hai?", reply_markup=kb)

    elif awaiting == "btn_step_value":
        flow = context.user_data.get("btn_flow")
        if not flow:
            context.user_data.pop("awaiting", None)
            return
        flow["data"]["value"] = text
        context.user_data["awaiting"] = "btn_step_row"
        await update.message.reply_text("Kaunsi row mein ye button aaye (1, 2, 3...)?")

    elif awaiting == "btn_step_row":
        flow = context.user_data.pop("btn_flow", None)
        context.user_data.pop("awaiting", None)
        if not flow:
            return
        if not text.isdigit():
            await update.message.reply_text("Number bhejo.")
            context.user_data["btn_flow"] = flow
            context.user_data["awaiting"] = "btn_step_row"
            return
        flow["data"]["row"] = int(text)
        menu_id = flow["menu_id"]
        BOT_DATA["menus"][menu_id]["buttons"].append(flow["data"])
        save_data()
        await update.message.reply_text(f"✅ Button add ho gaya '{menu_id}' mein.")

    else:
        context.user_data.pop("awaiting", None)
        await update.message.reply_text("Samajh nahi aaya, dobara /admin try karo.")


async def handle_admin_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    awaiting = context.user_data.get("awaiting")
    user_id = update.effective_user.id
    if not awaiting or not is_admin(user_id):
        return

    if awaiting.startswith("menu_image:") and update.message.photo:
        menu_id = awaiting.split(":", 1)[1]
        context.user_data.pop("awaiting", None)
        file_id = update.message.photo[-1].file_id
        menu = BOT_DATA["menus"][menu_id]
        if len(menu.get("text", "")) > 1024:
            await update.message.reply_text(
                "⚠️ Is menu ka text 1024 characters se lamba hai, image caption mein fit nahi hoga. "
                "Pehle text chhota karo, fir image lagao."
            )
            return
        menu["image_file_id"] = file_id
        save_data()
        await update.message.reply_text(f"✅ '{menu_id}' ki image update ho gayi.")
        return

    if awaiting == "broadcast_content":
        context.user_data.pop("awaiting", None)
        await do_broadcast(update, context)
        return


# ----------------------------------------------------------------------------
# Health / DB status / export / restore  (#11, #12, #13)
# ----------------------------------------------------------------------------

async def cmd_dbstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    col = get_mongo_collection()
    if col is not None:
        text = "✅ MongoDB: connected"
    elif MONGO_URI:
        text = f"❌ MongoDB: not connected\nReason: {_mongo_last_error}"
    else:
        text = "ℹ️ MongoDB not configured — using local JSON file."
    text += f"\n\nUsers: {len(BOT_DATA['users'])} | Groups: {len(BOT_DATA['groups'])} | Admins: {len(BOT_DATA['admins'])}"
    await update.message.reply_text(text)


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    col = get_mongo_collection()
    backend = "MongoDB ✅" if col is not None else "Local JSON fallback"
    mem = get_memory_usage_mb()
    text = (
        "🩺 Health\n\n"
        f"⏱ Uptime: {human_uptime()}\n"
        f"💾 Memory: {mem if mem is not None else 'n/a'} MB\n"
        f"🗄 Storage: {backend}\n"
        f"👥 Users: {len(BOT_DATA['users'])}\n"
        f"📢 Broadcasts: {len(BOT_DATA['broadcast_log'])}\n"
        f"🔒 Maintenance: {'ON' if BOT_DATA['settings'].get('maintenance') else 'OFF'}\n"
        f"🎛 Rate limit: {BOT_DATA['settings'].get('rate_limit_max')}/{BOT_DATA['settings'].get('rate_limit_window_seconds')}s\n"
        f"🔘 Button style support: {'yes' if SUPPORTS_BUTTON_STYLE else 'no (upgrade python-telegram-bot to 22.7+)'}\n"
    )
    await update.message.reply_text(text)


async def cmd_database(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    path = os.path.join(tempfile.gettempdir(), f"bot_data_export_{int(time.time())}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(BOT_DATA, f, ensure_ascii=False, indent=2)
    with open(path, "rb") as f:
        await update.message.reply_document(document=f, filename="bot_data_backup.json")
    os.remove(path)


async def cmd_exportusers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """CSV export of users."""
    if not is_owner(update.effective_user.id):
        return
    path = os.path.join(tempfile.gettempdir(), f"users_export_{int(time.time())}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["user_id", "name", "username", "joined", "last_active"])
        for uid, info in BOT_DATA["users"].items():
            writer.writerow([uid, info.get("name"), info.get("username"), info.get("joined"), info.get("last_active")])
    with open(path, "rb") as f:
        await update.message.reply_document(document=f, filename="users_export.csv")
    os.remove(path)


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """v2 §12 — latency check, available to everyone."""
    t0 = time.monotonic()
    msg = await update.message.reply_text("🏓 Pong!")
    ms = int((time.monotonic() - t0) * 1000)
    await msg.edit_text(f"🏓 Pong! {ms}ms")


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """v2 §12 — owner-only, zips the bot source dir and DMs it to the owner."""
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("🚫 " + to_small_caps("access denied"))
        return
    src_dir = os.path.dirname(os.path.abspath(__file__))
    zip_base = os.path.join(tempfile.gettempdir(), f"bot_export_{int(time.time())}")
    try:
        zip_path = shutil.make_archive(zip_base, "zip", src_dir)
    except Exception:
        log.exception("Export zip failed")
        await update.message.reply_text("⚠️ Export failed, check logs.")
        return
    with open(zip_path, "rb") as f:
        await context.bot.send_document(chat_id=update.effective_user.id, document=f, filename="bot_source.zip")
    os.remove(zip_path)
    if update.effective_chat.id != update.effective_user.id:
        await update.message.reply_text("✅ Sent to your DM.")


# ----------------------------------------------------------------------------
# PDF #6 — Blocked users list, plus admin-command blocking of links/domains
# ----------------------------------------------------------------------------

async def cmd_block(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /block <user_id | link | domain>")
        return
    target = context.args[0]
    if target.isdigit():
        uid_int = int(target)
        if uid_int not in BOT_DATA["blocked"]:
            BOT_DATA["blocked"].append(uid_int)
            save_data()
        await update.message.reply_text(f"✅ User {uid_int} blocked.")
        await log_event(context, f"🚫 Admin blocked user {uid_int}")
    elif target.startswith("http://") or target.startswith("https://"):
        if target not in BOT_DATA["blocked_links"]:
            BOT_DATA["blocked_links"].append(target)
            save_data()
        await update.message.reply_text(f"✅ Link blocked: {target}")
        await log_event(context, f"🚫 Admin blocked link {target}")
    else:
        if target not in BOT_DATA["blocked_domains"]:
            BOT_DATA["blocked_domains"].append(target)
            save_data()
        await update.message.reply_text(f"✅ Domain blocked: {target}")
        await log_event(context, f"🚫 Admin blocked domain {target}")


async def cmd_unblock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /unblock <user_id | link | domain>")
        return
    target = context.args[0]
    removed = False
    if target.isdigit() and int(target) in BOT_DATA["blocked"]:
        BOT_DATA["blocked"].remove(int(target))
        removed = True
    if target in BOT_DATA["blocked_links"]:
        BOT_DATA["blocked_links"].remove(target)
        removed = True
    if target in BOT_DATA["blocked_domains"]:
        BOT_DATA["blocked_domains"].remove(target)
        removed = True
    if removed:
        save_data()
        await update.message.reply_text(f"✅ Unblocked: {target}")
        await log_event(context, f"✅ Admin unblocked {target}")
    else:
        await update.message.reply_text("Wasn't on any blocked list.")


async def handle_restore_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_owner(user.id):
        return
    if update.effective_chat.type != "private":
        return
    if context.user_data.get("awaiting") or context.user_data.get("btn_flow"):
        return  # owner mid another flow — don't collide

    doc = update.message.document
    if not doc or not doc.file_name.lower().endswith(".json"):
        return

    tg_file = await doc.get_file()
    raw_path = os.path.join(tempfile.gettempdir(), f"incoming_{int(time.time())}.json")
    await tg_file.download_to_drive(raw_path)

    try:
        with open(raw_path, "r", encoding="utf-8") as f:
            incoming = json.load(f)
    except Exception:
        await update.message.reply_text("❌ Ye valid JSON file nahi hai.")
        os.remove(raw_path)
        return

    if not set(DEFAULT_DATA.keys()).issubset(set(incoming.keys())):
        await update.message.reply_text("❌ Ye backup file jaisi nahi lag rahi. Restore cancel kar diya.")
        os.remove(raw_path)
        return

    context.user_data["pending_restore"] = incoming
    os.remove(raw_path)

    cur_users, new_users = len(BOT_DATA["users"]), len(incoming.get("users", {}))
    cur_admins, new_admins = len(BOT_DATA["admins"]), len(incoming.get("admins", []))
    text = (
        f"⚠️ Restore Confirmation\n\nUsers: {cur_users} → {new_users}\nAdmins: {cur_admins} → {new_admins}\n\n"
        "⚠️ Ye current LIVE data ko poori tarah REPLACE kar dega.\n(Current data ki backup pehle le li jayegi.)"
    )
    kb = InlineKeyboardMarkup(
        [[styled_button("✅ Confirm Restore", callback_data="restore_confirm", style="danger"),
          styled_button("❌ Cancel", callback_data="restore_cancel")]]
    )
    await update.message.reply_text(text, reply_markup=kb)


async def cb_restore_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_DATA
    query = update.callback_query
    await query.answer()
    if not is_owner(update.effective_user.id):
        return
    incoming = context.user_data.pop("pending_restore", None)
    if incoming is None:
        await query.edit_message_text("Kuch expire ho gaya, dobara file bhejo.")
        return
    before_path = make_backup_snapshot(reason="pre_restore")
    before_users = len(BOT_DATA["users"])
    BOT_DATA = _deep_merge_defaults(incoming)
    save_data()
    BOT_DATA["restore_log"].append(
        {"by": update.effective_user.id, "at": datetime.utcnow().isoformat(),
         "before_users": before_users, "after_users": len(BOT_DATA["users"])}
    )
    save_data()
    await query.edit_message_text(
        f"✅ Restore complete.\nBefore: {before_users} → After: {len(BOT_DATA['users'])}\n"
        f"Pre-restore snapshot: {os.path.basename(before_path)}\n\nConfirm karne ke liye /dbstatus chalao."
    )


async def cb_restore_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop("pending_restore", None)
    await query.edit_message_text("Restore cancel kar diya gaya.")


async def scheduled_backup_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        path = make_backup_snapshot(reason="scheduled")
        if OWNER_ID:
            with open(path, "rb") as f:
                await context.bot.send_document(chat_id=OWNER_ID, document=f, filename=os.path.basename(path), caption="🗄 Scheduled backup snapshot.")
    except Exception:
        log.exception("Scheduled backup failed")


async def inactive_reengage_job(context: ContextTypes.DEFAULT_TYPE):
    """#15 — lightweight re-engagement, reuses render_menu, no new sending logic."""
    days = BOT_DATA["settings"].get("inactive_reengage_days", 0)
    if not days:
        return
    cutoff = datetime.utcnow() - timedelta(days=days)
    for uid, info in BOT_DATA["users"].items():
        try:
            last_active = datetime.fromisoformat(info.get("last_active"))
        except Exception:
            continue
        last_reengaged = info.get("last_reengaged")
        already_today = last_reengaged and (datetime.utcnow() - datetime.fromisoformat(last_reengaged)) < timedelta(days=days)
        if last_active < cutoff and not already_today:
            try:
                await render_menu(context, int(uid), "start")
                info["last_reengaged"] = datetime.utcnow().isoformat()
            except Exception:
                pass
    save_data()


# ----------------------------------------------------------------------------
# App wiring
# ----------------------------------------------------------------------------

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """#13 — escape hatch out of any stuck admin/user text-input flow."""
    for key in (
        "awaiting", "btn_flow", "style_source_text", "style_target",
        "message_target", "report_link_draft", "owner_contact_label_draft",
        "autoreply_key_draft",
    ):
        context.user_data.pop(key, None)
    await update.message.reply_text("✅ Cancelled. Any pending flow has been cleared.")


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """#13 — catch-all so one bad update can't silently kill processing, and
    every failure is visible from the admin 'Recent Errors' screen / logger channel."""
    log.exception("Unhandled error", exc_info=context.error)
    entry = {
        "at": datetime.utcnow().isoformat(),
        "update_type": type(update).__name__ if update else "unknown",
        "error": str(context.error),
    }
    BOT_DATA.setdefault("error_log", []).append(entry)
    if len(BOT_DATA["error_log"]) > 200:
        del BOT_DATA["error_log"][: len(BOT_DATA["error_log"]) - 200]
    save_data()
    try:
        await log_event(context, f"🐞 Error: {entry['error'][:300]}")
    except Exception:
        pass


SCREEN_RENDERERS.update(
    {
        "adm_home": _render_adm_home,
        "adm_stats": _render_adm_stats,
        "adm_users": _render_adm_users,
        "adm_broadcast": _render_adm_broadcast,
        "adm_menu_ui": _render_adm_menu_ui,
        "adm_settings": _render_adm_settings,
        "adm_danger": _render_adm_danger,
        "adm_owner_contact": _render_adm_owner_contact,
        "adm_logger_channel": _render_adm_logger_channel,
        "adm_premium": _render_adm_premium,
        "adm_upi": _render_adm_upi,
        "adm_devsettings": _render_adm_devsettings,
        "adm_support_settings": _render_adm_support_settings,
        "adm_tickets": _render_adm_tickets,
    }
)


def build_app() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("language", cmd_language))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("dbstatus", cmd_dbstatus))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("database", cmd_database))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("exportusers", cmd_exportusers))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("block", cmd_block))
    app.add_handler(CommandHandler("unblock", cmd_unblock))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    app.add_handler(CallbackQueryHandler(cb_agree_terms, pattern="^agree_terms$"))
    app.add_handler(CallbackQueryHandler(cb_report_copyright, pattern="^report_copyright$"))
    app.add_handler(CallbackQueryHandler(cb_support_start, pattern="^support_start$"))
    app.add_handler(CallbackQueryHandler(cb_adm_block_link, pattern="^adm_block_link:"))
    app.add_handler(CallbackQueryHandler(cb_adm_block_domain, pattern="^adm_block_domain:"))
    app.add_handler(CallbackQueryHandler(cb_adm_owner_contact_set, pattern="^adm_owner_contact_set$"))
    app.add_handler(CallbackQueryHandler(cb_adm_owner_contact_clear, pattern="^adm_owner_contact_clear$"))
    app.add_handler(CallbackQueryHandler(cb_adm_logger_channel_set, pattern="^adm_logger_channel_set$"))

    app.add_handler(CallbackQueryHandler(cb_get_caption, pattern="^get_caption$"))
    app.add_handler(CallbackQueryHandler(cb_download_another, pattern="^download_another$"))

    app.add_handler(CallbackQueryHandler(cb_ticket_close, pattern="^tk_close:"))
    app.add_handler(CallbackQueryHandler(cb_ticket_reopen, pattern="^tk_reopen:"))

    app.add_handler(CallbackQueryHandler(cb_gift_menu, pattern="^gift_menu$"))
    app.add_handler(CallbackQueryHandler(cb_gift_stars, pattern="^gift_stars$"))
    app.add_handler(CallbackQueryHandler(cb_gift_stars_custom, pattern="^gift_stars_custom$"))
    app.add_handler(CallbackQueryHandler(cb_gift_stars_amount, pattern="^gift_stars_amt:"))
    app.add_handler(CallbackQueryHandler(cb_gift_upi, pattern="^gift_upi$"))
    app.add_handler(CallbackQueryHandler(cb_gift_upi_paid, pattern="^gift_upi_paid:"))
    app.add_handler(PreCheckoutQueryHandler(cmd_precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, cmd_successful_payment))
    app.add_handler(CallbackQueryHandler(cb_nav, pattern="^nav:"))
    app.add_handler(CallbackQueryHandler(cb_toggle_menu_button, pattern="^tgl:"))
    app.add_handler(CallbackQueryHandler(cb_settings_toggle, pattern="^stgl:"))
    app.add_handler(CallbackQueryHandler(cb_styleset, pattern="^styleset:"))
    app.add_handler(CallbackQueryHandler(cb_noop, pattern="^noop$"))

    app.add_handler(CallbackQueryHandler(cb_adm_home, pattern="^adm_home$"))
    app.add_handler(CallbackQueryHandler(cb_adm_back, pattern="^adm_back$"))
    app.add_handler(CallbackQueryHandler(nav_tracked("adm_stats")(cb_adm_stats), pattern="^adm_stats$"))
    app.add_handler(CallbackQueryHandler(nav_tracked("adm_users")(cb_adm_users), pattern="^adm_users$"))
    app.add_handler(CallbackQueryHandler(cb_adm_users_list, pattern="^adm_users_list$"))
    app.add_handler(CallbackQueryHandler(cb_adm_users_msg, pattern="^adm_users_msg$"))
    app.add_handler(CallbackQueryHandler(nav_tracked("adm_broadcast")(cb_adm_broadcast), pattern="^adm_broadcast$"))
    app.add_handler(CallbackQueryHandler(cb_adm_bc_new, pattern="^adm_bc_new$"))
    app.add_handler(CallbackQueryHandler(cb_adm_bc_log, pattern="^adm_bc_log$"))

    app.add_handler(CallbackQueryHandler(nav_tracked("adm_menu_ui")(cb_adm_menu_ui), pattern="^adm_menu_ui$"))
    app.add_handler(CallbackQueryHandler(cb_adm_menu_edit, pattern="^adm_menu_edit:"))
    app.add_handler(CallbackQueryHandler(cb_adm_menu_txt, pattern="^adm_menu_txt:"))
    app.add_handler(CallbackQueryHandler(cb_adm_menu_style, pattern="^adm_menu_style:"))
    app.add_handler(CallbackQueryHandler(cb_adm_menu_parsemode, pattern="^adm_menu_parsemode:"))
    app.add_handler(CallbackQueryHandler(cb_adm_menu_img, pattern="^adm_menu_img:"))
    app.add_handler(CallbackQueryHandler(cb_adm_menu_rmimg, pattern="^adm_menu_rmimg:"))
    app.add_handler(CallbackQueryHandler(cb_adm_menu_autodel, pattern="^adm_menu_autodel:"))
    app.add_handler(CallbackQueryHandler(cb_adm_menu_btns, pattern="^adm_menu_btns:"))
    app.add_handler(CallbackQueryHandler(cb_adm_menu_trans, pattern="^adm_menu_trans:"))
    app.add_handler(CallbackQueryHandler(cb_adm_menu_trans_edit, pattern="^adm_menu_trans_edit:"))
    app.add_handler(CallbackQueryHandler(cb_setlang, pattern="^setlang:"))
    app.add_handler(CallbackQueryHandler(cb_adm_btn_add, pattern="^adm_btn_add:"))
    app.add_handler(CallbackQueryHandler(cb_adm_btn_del, pattern="^adm_btn_del:"))
    app.add_handler(CallbackQueryHandler(cb_adm_btn_style, pattern="^adm_btn_style:"))
    app.add_handler(CallbackQueryHandler(cb_btn_type_pick, pattern="^btntype:"))

    app.add_handler(CallbackQueryHandler(nav_tracked("adm_settings")(cb_adm_settings), pattern="^adm_settings$"))
    app.add_handler(CallbackQueryHandler(cb_adm_lang_manage, pattern="^adm_lang_manage$"))
    app.add_handler(CallbackQueryHandler(cb_adm_lang_add, pattern="^adm_lang_add$"))
    app.add_handler(CallbackQueryHandler(cb_adm_lang_add_do, pattern="^adm_lang_add_do:"))
    app.add_handler(CallbackQueryHandler(cb_adm_lang_remove, pattern="^adm_lang_remove$"))
    app.add_handler(CallbackQueryHandler(cb_adm_lang_remove_do, pattern="^adm_lang_remove_do:"))
    app.add_handler(CallbackQueryHandler(cb_adm_set_autodelete, pattern="^adm_set_autodelete$"))
    app.add_handler(CallbackQueryHandler(cb_adm_autoreply_list, pattern="^adm_autoreply_list$"))
    app.add_handler(CallbackQueryHandler(cb_adm_autoreply_add, pattern="^adm_autoreply_add$"))
    app.add_handler(CallbackQueryHandler(cb_adm_autoreply_del, pattern="^adm_autoreply_del$"))
    app.add_handler(CallbackQueryHandler(cb_adm_manage_admins, pattern="^adm_manage_admins$"))
    app.add_handler(CallbackQueryHandler(cb_adm_add_admin, pattern="^adm_add_admin$"))
    app.add_handler(CallbackQueryHandler(cb_adm_remove_admin, pattern="^adm_remove_admin$"))
    app.add_handler(CallbackQueryHandler(cb_adm_restore_info, pattern="^adm_restore_info$"))
    app.add_handler(CallbackQueryHandler(nav_tracked("adm_owner_contact")(cb_adm_owner_contact), pattern="^adm_owner_contact$"))
    app.add_handler(CallbackQueryHandler(nav_tracked("adm_logger_channel")(cb_adm_logger_channel), pattern="^adm_logger_channel$"))

    app.add_handler(CallbackQueryHandler(nav_tracked("adm_premium")(cb_adm_premium), pattern="^adm_premium$"))
    app.add_handler(CallbackQueryHandler(cb_adm_set_dailylimit, pattern="^adm_set_dailylimit$"))
    app.add_handler(CallbackQueryHandler(nav_tracked("adm_upi")(cb_adm_upi), pattern="^adm_upi$"))
    app.add_handler(CallbackQueryHandler(cb_adm_upi_set, pattern="^adm_upi_set$"))
    app.add_handler(CallbackQueryHandler(cb_adm_upi_clear, pattern="^adm_upi_clear$"))
    app.add_handler(CallbackQueryHandler(nav_tracked("adm_devsettings")(cb_adm_devsettings), pattern="^adm_devsettings$"))
    app.add_handler(CallbackQueryHandler(cb_adm_dev_id, pattern="^adm_dev_id$"))
    app.add_handler(CallbackQueryHandler(cb_adm_dev_link, pattern="^adm_dev_link$"))
    app.add_handler(CallbackQueryHandler(nav_tracked("adm_support_settings")(cb_adm_support_settings), pattern="^adm_support_settings$"))
    app.add_handler(CallbackQueryHandler(cb_adm_group_set, pattern="^adm_group_set$"))
    app.add_handler(CallbackQueryHandler(nav_tracked("adm_tickets")(cb_adm_tickets), pattern="^adm_tickets$"))

    app.add_handler(CallbackQueryHandler(nav_tracked("adm_danger")(cb_adm_danger), pattern="^adm_danger$"))
    app.add_handler(CallbackQueryHandler(cb_adm_clear_bclog, pattern="^adm_clear_bclog$"))
    app.add_handler(CallbackQueryHandler(cb_adm_delete_chat_msgs, pattern="^adm_delete_chat_msgs$"))
    app.add_handler(CallbackQueryHandler(cb_adm_reset_menus_confirm, pattern="^adm_reset_menus_confirm$"))
    app.add_handler(CallbackQueryHandler(cb_adm_reset_menus_do, pattern="^adm_reset_menus_do$"))
    app.add_handler(CallbackQueryHandler(cb_adm_reset_confirm, pattern="^adm_reset_confirm$"))
    app.add_handler(CallbackQueryHandler(cb_adm_reset_do, pattern="^adm_reset_do$"))
    app.add_handler(CallbackQueryHandler(cb_restore_confirm, pattern="^restore_confirm$"))
    app.add_handler(CallbackQueryHandler(cb_restore_cancel, pattern="^restore_cancel$"))

    app.add_handler(MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, handle_restore_upload))
    app.add_handler(MessageHandler((filters.PHOTO | filters.VIDEO) & filters.ChatType.PRIVATE, handle_admin_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.add_error_handler(global_error_handler)

    if app.job_queue is not None:
        if BACKUP_INTERVAL_HOURS > 0:
            app.job_queue.run_repeating(
                scheduled_backup_job, interval=timedelta(hours=BACKUP_INTERVAL_HOURS),
                first=timedelta(hours=BACKUP_INTERVAL_HOURS),
            )
        app.job_queue.run_repeating(inactive_reengage_job, interval=timedelta(hours=24), first=timedelta(hours=24))

    return app


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN env var is required.")
    if not OWNER_ID:
        log.warning("OWNER_ID is not set — owner-only commands will be unreachable.")
    load_data()
    app = build_app()
    log.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

# ------------------------------------------------------------------------
# Scope notes (what's implemented vs intentionally skipped):
#
# Implemented: dynamic menu engine + render_menu (#1,3), per-menu image
# with caption-length validation (#2), style/small-caps text helper (#1),
# custom per-menu buttons: menu/url/callback/toggle types (#4), colorful
# buttons with library-version fallback (#5), separate user/admin help
# (#6), global + per-menu auto-delete (#7), forward-lock toggle (#8),
# categorized admin panel incl. new Menu & UI category (#9), storage layer
# unchanged (#10), health dashboard (#11, simplified — no per-API-provider
# stats since the bot only calls Telegram + yt-dlp, no third-party APIs to
# track), CSV user export + full JSON database export (#12), recurring
# backup + restore-by-upload with confirm screen (#13), typing indicator +
# keyword auto-reply + rate limiting + inactive re-engagement (#15).
#
# Skipped for scope (say the word and I'll add any of these next):
# - PDF report with charts (#12) — CSV covers the data, PDF needs
#   reportlab/matplotlib and a fair bit more code.
# - Button reordering UI / multi-value edit-in-place (#4) — right now you
#   delete + re-add a button to change it; a dedicated "edit" flow is a
#   straightforward follow-up if you want it.
# - Encrypted backups, multi-language menus, menu version history (#14).
# - Premium custom-emoji support (#16) — needs the bot-owner Telegram
#   account to have Premium; flag if that's the case and I'll wire it up.
# ------------------------------------------------------------------------
