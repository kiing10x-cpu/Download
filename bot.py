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
import html
import shutil
import logging
import tempfile
from datetime import datetime, timedelta

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
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


def wrap_blockquote(text: str, expandable: bool = False) -> str:
    """Wrap plain text in Telegram's NATIVE <blockquote> HTML entity — the
    real quote-bar/background look (Bot API, not a fake HTML/CSS card, not
    an image). Requires parse_mode="HTML" on whatever send/edit call uses
    the result. Text is HTML-escaped first so any literal &, <, > in the
    admin's text can't break the tag or get swallowed."""
    escaped = html.escape(text, quote=False)
    tag = '<blockquote expandable="true">' if expandable else "<blockquote>"
    return f"{tag}{escaped}</blockquote>"


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
        "quote_style": True,
        "quote_expandable": False,
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
            {"label": to_small_caps("🏠 main menu"), "type": "menu", "value": "start", "row": 1, "style": "primary"}
        ],
        "auto_delete_seconds": None,
        "updated_by": None,
        "updated_at": None,
        "translations": {},
        "quote_style": True,
        "quote_expandable": False,
    },
    "reel_result": {
        "text": to_deco(to_small_caps("here's your reel")),
        "parse_mode": None,
        "image_file_id": None,
        "buttons": [
            {"label": to_small_caps("📝 get caption"), "type": "callback", "value": "get_caption", "row": 1, "style": "primary"},
            {"label": to_small_caps("🏠 main menu"), "type": "menu", "value": "start", "row": 1, "style": "primary"},
        ],
        "auto_delete_seconds": None,
        "updated_by": None,
        "updated_at": None,
        "translations": {},
        "quote_style": True,
        "quote_expandable": False,
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
        "quote_style": True,
        "quote_expandable": False,
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
    },
    "broadcast_log": [],
    "restore_log": [],
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


def touch_user(update: Update):
    user = update.effective_user
    if not user:
        return
    uid = str(user.id)
    now = datetime.utcnow().isoformat()
    users = BOT_DATA["users"]
    if uid not in users:
        users[uid] = {
            "name": user.full_name, "username": user.username,
            "joined": now, "last_active": now, "last_reengaged": None,
            "lang": None,
        }
    else:
        users[uid]["last_active"] = now
        users[uid]["name"] = user.full_name
    save_data()


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


def rendered_menu_text_length(menu: dict) -> int:
    """Length Telegram will actually see, including <blockquote> wrapping
    overhead when quote_style is on — used for the 1024-char caption checks."""
    return caption_length_for(menu, menu.get("text", ""))


def caption_length_for(menu: dict, candidate_text: str) -> int:
    """Effective rendered length of `candidate_text` if it were saved as this
    menu's text (accounts for blockquote wrap overhead when quote_style is on)."""
    if menu.get("quote_style"):
        return len(wrap_blockquote(candidate_text, expandable=menu.get("quote_expandable", False)))
    return len(candidate_text)


def menu_needs_caption_limit(menu: dict, menu_id: str) -> bool:
    """True if this menu is ever sent as a photo/video caption (1024-char
    Telegram limit) rather than a free-standing text message (4096 limit).
    reel_result is ALWAYS a video caption regardless of image_file_id."""
    return bool(menu.get("image_file_id")) or menu_id == "reel_result"


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

    # Native Telegram <blockquote> styling (real quote-bar look, not a fake
    # HTML/CSS card and not an image) — appears directly under the photo
    # when the menu has an image, or as the message body otherwise.
    if menu.get("quote_style"):
        text = wrap_blockquote(text, expandable=menu.get("quote_expandable", False))
        parse_mode = "HTML"

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
                    chat_id, photo=image, caption=text, parse_mode=parse_mode, reply_markup=kb
                )
            elif not image and has_photo:
                await existing_message.delete()
                sent_message = await context.bot.send_message(
                    chat_id, text=text, parse_mode=parse_mode, reply_markup=kb
                )
            else:
                await existing_message.edit_text(text=text, parse_mode=parse_mode, reply_markup=kb)
                sent_message = existing_message
        else:
            if image:
                sent_message = await context.bot.send_photo(
                    chat_id, photo=image, caption=text, parse_mode=parse_mode, reply_markup=kb
                )
            else:
                sent_message = await context.bot.send_message(
                    chat_id, text=text, parse_mode=parse_mode, reply_markup=kb
                )
    except Exception:
        log.exception("render_menu failed for %s, sending fresh", menu_id)
        try:
            if image:
                sent_message = await context.bot.send_photo(
                    chat_id, photo=image, caption=text, parse_mode=parse_mode, reply_markup=kb
                )
            else:
                sent_message = await context.bot.send_message(
                    chat_id, text=text, parse_mode=parse_mode, reply_markup=kb
                )
        except Exception:
            log.exception("render_menu completely failed for %s", menu_id)
            return

    seconds = menu.get("auto_delete_seconds")
    if seconds is None:
        seconds = BOT_DATA["settings"].get("global_auto_delete_seconds", 0)
    if sent_message:
        await schedule_delete(context, chat_id, sent_message.message_id, seconds)


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
        menu = BOT_DATA["menus"][menu_id]
        would_be_length = caption_length_for(menu, styled_text)
        if menu_needs_caption_limit(menu, menu_id) and would_be_length > 1024:
            await query.edit_message_text(
                f"⚠️ Ye menu ek caption ke roop mein bhejta hai, limit 1024 characters hai "
                f"(styled/quote-wrapped text {would_be_length} hoga). Chhota source text try karo."
            )
            return
        menu["text"] = styled_text
        menu["updated_by"] = update.effective_user.id
        menu["updated_at"] = datetime.utcnow().isoformat()
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

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user(update)
    if not check_rate_limit(update.effective_user.id):
        await update.message.reply_text("⏳ Thoda slow karo, bahut jaldi jaldi requests aa rahi hain.")
        return
    if BOT_DATA["settings"].get("maintenance") and not is_admin(update.effective_user.id):
        await update.message.reply_text("🛠️ Bot abhi maintenance mode mein hai.")
        return
    await render_menu(context, update.effective_chat.id, "start")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user(update)
    menu_id = "help_admin" if is_admin(update.effective_user.id) else "help_user"
    await render_menu(context, update.effective_chat.id, menu_id)


LANG_NAMES = {
    "en": "🇬🇧 English", "hi": "🇮🇳 हिन्दी", "es": "🇪🇸 Español",
    "fr": "🇫🇷 Français", "ar": "🇸🇦 العربية", "pt": "🇵🇹 Português",
    "id": "🇮🇩 Indonesia", "bn": "🇧🇩 বাংলা", "ur": "🇵🇰 اردو",
}


async def cmd_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user(update)
    langs = BOT_DATA["settings"].get("languages", [])
    if not langs:
        await update.message.reply_text("Abhi koi extra language configure nahi hui hai.")
        return
    rows = [[styled_button("✨ Default (Hinglish)", callback_data="setlang:default")]]
    for code in langs:
        rows.append([styled_button(LANG_NAMES.get(code, code), callback_data=f"setlang:{code}")])
    await update.message.reply_text("🌐 Apni language choose karo:", reply_markup=InlineKeyboardMarkup(rows))


async def cb_setlang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    code = query.data.split(":", 1)[1]
    uid = str(update.effective_user.id)
    if uid in BOT_DATA["users"]:
        BOT_DATA["users"][uid]["lang"] = None if code == "default" else code
        save_data()
    await render_menu(context, query.message.chat_id, "start", existing_message=query.message)


# ----------------------------------------------------------------------------
# Reel download
# ----------------------------------------------------------------------------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user(update)
    user_id = update.effective_user.id
    awaiting = context.user_data.get("awaiting")

    if awaiting and is_admin(user_id):
        await handle_admin_text_input(update, context, awaiting)
        return

    if not check_rate_limit(user_id):
        await update.message.reply_text("⏳ Thoda slow karo, bahut jaldi jaldi requests aa rahi hain.")
        return

    text = update.message.text or ""
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
        await update.message.reply_text("🛠️ Bot abhi maintenance mode mein hai.")
        return

    url = match.group(1)
    status_msg = await update.message.reply_text("⏳ Download ho raha hai, best quality mein...")

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
            return fp, ig_caption

    file_path = None
    try:
        try:
            file_path, ig_caption = run_download(use_merge=FFMPEG_AVAILABLE)
        except Exception as e:
            # Self-heal: if a merge was attempted and ffmpeg turned out to be
            # the problem, retry once with a no-merge (progressive) format.
            if "ffmpeg" in str(e).lower():
                log.warning("Merge failed (ffmpeg issue), retrying with progressive format.")
                file_path, ig_caption = run_download(use_merge=False)
            else:
                raise

        uid = str(update.effective_user.id)
        lang = BOT_DATA["users"].get(uid, {}).get("lang")
        menu = BOT_DATA["menus"]["reel_result"]
        translation = menu.get("translations", {}).get(lang) if lang else None
        result_caption = (translation or {}).get("text") or menu.get("text", "")
        buttons = (translation or {}).get("buttons") or menu.get("buttons", [])
        kb = build_keyboard_from_buttons(buttons, "reel_result")

        result_parse_mode = menu.get("parse_mode") or None
        if menu.get("quote_style"):
            result_caption = wrap_blockquote(result_caption, expandable=menu.get("quote_expandable", False))
            result_parse_mode = "HTML"

        with open(file_path, "rb") as vid:
            sent = await update.message.reply_video(
                video=vid, caption=result_caption, parse_mode=result_parse_mode, reply_markup=kb
            )

        # Cache the real Instagram caption so the "Get Caption" button under
        # THIS specific video can show it, keyed to this exact message.
        _caption_cache[(sent.chat_id, sent.message_id)] = ig_caption
        if len(_caption_cache) > CAPTION_CACHE_MAX:
            _caption_cache.pop(next(iter(_caption_cache)))

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


# ----------------------------------------------------------------------------
# Admin panel — top level (#9 categorized, functional dispatcher — not a
# content menu, since these are actions, not editable copy)
# ----------------------------------------------------------------------------

def admin_panel_keyboard():
    return InlineKeyboardMarkup(
        [
            [styled_button("📊 Stats & Activity", callback_data="adm_stats", style="primary"),
             styled_button("👥 Users & Groups", callback_data="adm_users", style="primary")],
            [styled_button("📢 Broadcast", callback_data="adm_broadcast", style="primary"),
             styled_button("🎨 Menu & UI", callback_data="adm_menu_ui", style="primary")],
            [styled_button("⚙️ Settings & Admins", callback_data="adm_settings", style="primary"),
             styled_button("🛑 Danger Zone", callback_data="adm_danger", style="danger")],
        ]
    )


def back_row(cb="adm_home", label="🔙 Back to Panel"):
    return [styled_button(label, callback_data=cb)]


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("🛠️ Admin Panel", reply_markup=admin_panel_keyboard())


async def cb_adm_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("🛠️ Admin Panel", reply_markup=admin_panel_keyboard())


# ---- Stats & Activity -------------------------------------------------------

async def cb_adm_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
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
        f"⏱ Uptime: {human_uptime()}\n"
        f"💾 Memory: {mem if mem is not None else 'n/a'} MB\n"
        f"🗄 Storage backend: {backend}\n"
    )
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([back_row()]))


# ---- Users & Groups ----------------------------------------------------------

async def cb_adm_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kb = InlineKeyboardMarkup(
        [
            [styled_button("📋 List Users (last 20)", callback_data="adm_users_list")],
            [styled_button("✉️ Message a User", callback_data="adm_users_msg")],
            back_row(),
        ]
    )
    await query.edit_message_text("👥 Users & Groups", reply_markup=kb)


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

async def cb_adm_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
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
        ]
    )
    await query.edit_message_text("📢 Broadcast", reply_markup=kb)


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
    protect = BOT_DATA["settings"].get("protect_broadcasts", True)
    sent = 0
    failed = 0
    for uid in list(BOT_DATA["users"].keys()):
        try:
            copied = await context.bot.copy_message(
                chat_id=int(uid), from_chat_id=msg.chat_id, message_id=msg.message_id,
                protect_content=protect,
            )
            await schedule_delete(context, int(uid), copied.message_id, BOT_DATA["settings"].get("global_auto_delete_seconds", 0))
            sent += 1
        except Exception:
            failed += 1
    BOT_DATA["broadcast_log"].append(
        {"by": update.effective_user.id, "at": datetime.utcnow().isoformat(), "recipients": sent}
    )
    save_data()
    await update.message.reply_text(
        f"✅ Broadcast bhej diya.\nSent: {sent} | Failed: {failed}\nForward-lock: {'ON' if protect else 'OFF'}"
    )


# ---- Menu & UI (#1, #2, #4, #7 controls) -------------------------------------

async def cb_adm_menu_ui(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    rows = [[styled_button(f"📝 {mid}", callback_data=f"adm_menu_edit:{mid}")] for mid in BOT_DATA["menus"]]
    rows.append(back_row())
    await query.edit_message_text("🎨 Menu & UI — kaun sa menu edit karna hai?", reply_markup=InlineKeyboardMarkup(rows))


async def cb_adm_menu_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    menu_id = query.data.split(":", 1)[1]
    menu = BOT_DATA["menus"][menu_id]
    parse_mode_label = menu.get("parse_mode") or "OFF (raw text)"
    override = menu.get("auto_delete_seconds")
    override_label = f"{override}s" if override is not None else "uses global"
    quote_on = bool(menu.get("quote_style"))
    expand_on = bool(menu.get("quote_expandable"))
    kb = InlineKeyboardMarkup(
        [
            [styled_button("✏️ Edit Text", callback_data=f"adm_menu_txt:{menu_id}")],
            [styled_button("🅰️ Style Text", callback_data=f"adm_menu_style:{menu_id}")],
            [styled_button(f"🔤 Parse Mode: {parse_mode_label}", callback_data=f"adm_menu_parsemode:{menu_id}")],
            [styled_button(f"💬 Native Quote Block: {'ON' if quote_on else 'OFF'}",
                           callback_data=f"adm_menu_quote:{menu_id}", style="success" if quote_on else "danger")],
            [styled_button(f"📖 Expandable Quote: {'ON' if expand_on else 'OFF'}",
                           callback_data=f"adm_menu_quote_exp:{menu_id}", style="success" if expand_on else "danger")],
            [styled_button("🖼️ Set Image", callback_data=f"adm_menu_img:{menu_id}"),
             styled_button("🗑️ Remove Image", callback_data=f"adm_menu_rmimg:{menu_id}")],
            [styled_button("🔘 Manage Buttons", callback_data=f"adm_menu_btns:{menu_id}")],
            [styled_button(f"⏱ Auto-Delete: {override_label}", callback_data=f"adm_menu_autodel:{menu_id}")],
            [styled_button("🌐 Translations", callback_data=f"adm_menu_trans:{menu_id}")],
            [styled_button("🔙 Back", callback_data="adm_menu_ui")],
        ]
    )
    note = "\n\n💬 Native Quote Block ON forces HTML parse mode (needed for the quote to render) — the Parse Mode button above is ignored while it's ON." if quote_on else ""
    await query.edit_message_text(f"📝 Editing: {menu_id}{note}", reply_markup=kb)


async def cb_adm_menu_quote_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    menu_id = query.data.split(":", 1)[1]
    menu = BOT_DATA["menus"][menu_id]
    menu["quote_style"] = not bool(menu.get("quote_style"))
    save_data()
    await cb_adm_menu_edit(update, context)


async def cb_adm_menu_quote_exp_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    menu_id = query.data.split(":", 1)[1]
    menu = BOT_DATA["menus"][menu_id]
    menu["quote_expandable"] = not bool(menu.get("quote_expandable"))
    save_data()
    await cb_adm_menu_edit(update, context)



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

async def cb_adm_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
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
            back_row(),
        ]
    )
    await query.edit_message_text("⚙️ Settings & Admins", reply_markup=kb)


async def cb_settings_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    _, key, return_to = query.data.split(":", 2)
    BOT_DATA["settings"][key] = not bool(BOT_DATA["settings"].get(key, False))
    save_data()
    if return_to == "adm_settings":
        await cb_adm_settings(update, context)
    elif return_to == "adm_broadcast":
        await cb_adm_broadcast(update, context)


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

async def cb_adm_danger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kb = InlineKeyboardMarkup(
        [
            [styled_button("🧹 Clear Broadcast Log", callback_data="adm_clear_bclog", style="danger")],
            [styled_button("🔄 Reset Menus to Default", callback_data="adm_reset_menus_confirm", style="danger")],
            [styled_button("❌ Reset ALL Bot Data", callback_data="adm_reset_confirm", style="danger")],
            back_row(),
        ]
    )
    await query.edit_message_text("🛑 Danger Zone\n(Ye actions destructive hain.)", reply_markup=kb)


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
        would_be_length = caption_length_for(menu, text)
        if menu_needs_caption_limit(menu, menu_id) and would_be_length > 1024:
            await update.message.reply_text(
                f"⚠️ Ye menu ek caption ke roop mein bhejta hai (image/video ke saath), limit 1024 characters hai "
                f"(quote-wrap ke saath tumhara text {would_be_length} hoga). Chhota karo."
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
        would_be_length = caption_length_for(menu, text)
        if menu_needs_caption_limit(menu, menu_id) and would_be_length > 1024:
            await update.message.reply_text(
                f"⚠️ Ye menu ek caption ke roop mein bhejta hai, limit 1024 characters hai "
                f"(quote-wrap ke saath ye translation {would_be_length} hoga). Chhota karo."
            )
            return
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
        if menu_needs_caption_limit(menu, menu_id) and rendered_menu_text_length(menu) > 1024:
            await update.message.reply_text(
                "⚠️ Is menu ka text (quote-wrap ke saath) 1024 characters se lamba hai, image caption mein fit nahi hoga. "
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


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """CSV export of users. (PDF/chart report skipped for scope — ask if needed.)"""
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

    app.add_handler(CallbackQueryHandler(cb_get_caption, pattern="^get_caption$"))
    app.add_handler(CallbackQueryHandler(cb_nav, pattern="^nav:"))
    app.add_handler(CallbackQueryHandler(cb_toggle_menu_button, pattern="^tgl:"))
    app.add_handler(CallbackQueryHandler(cb_settings_toggle, pattern="^stgl:"))
    app.add_handler(CallbackQueryHandler(cb_styleset, pattern="^styleset:"))
    app.add_handler(CallbackQueryHandler(cb_noop, pattern="^noop$"))

    app.add_handler(CallbackQueryHandler(cb_adm_home, pattern="^adm_home$"))
    app.add_handler(CallbackQueryHandler(cb_adm_stats, pattern="^adm_stats$"))
    app.add_handler(CallbackQueryHandler(cb_adm_users, pattern="^adm_users$"))
    app.add_handler(CallbackQueryHandler(cb_adm_users_list, pattern="^adm_users_list$"))
    app.add_handler(CallbackQueryHandler(cb_adm_users_msg, pattern="^adm_users_msg$"))
    app.add_handler(CallbackQueryHandler(cb_adm_broadcast, pattern="^adm_broadcast$"))
    app.add_handler(CallbackQueryHandler(cb_adm_bc_new, pattern="^adm_bc_new$"))
    app.add_handler(CallbackQueryHandler(cb_adm_bc_log, pattern="^adm_bc_log$"))

    app.add_handler(CallbackQueryHandler(cb_adm_menu_ui, pattern="^adm_menu_ui$"))
    app.add_handler(CallbackQueryHandler(cb_adm_menu_edit, pattern="^adm_menu_edit:"))
    app.add_handler(CallbackQueryHandler(cb_adm_menu_txt, pattern="^adm_menu_txt:"))
    app.add_handler(CallbackQueryHandler(cb_adm_menu_style, pattern="^adm_menu_style:"))
    app.add_handler(CallbackQueryHandler(cb_adm_menu_parsemode, pattern="^adm_menu_parsemode:"))
    app.add_handler(CallbackQueryHandler(cb_adm_menu_quote_toggle, pattern="^adm_menu_quote:"))
    app.add_handler(CallbackQueryHandler(cb_adm_menu_quote_exp_toggle, pattern="^adm_menu_quote_exp:"))
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

    app.add_handler(CallbackQueryHandler(cb_adm_settings, pattern="^adm_settings$"))
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

    app.add_handler(CallbackQueryHandler(cb_adm_danger, pattern="^adm_danger$"))
    app.add_handler(CallbackQueryHandler(cb_adm_clear_bclog, pattern="^adm_clear_bclog$"))
    app.add_handler(CallbackQueryHandler(cb_adm_reset_menus_confirm, pattern="^adm_reset_menus_confirm$"))
    app.add_handler(CallbackQueryHandler(cb_adm_reset_menus_do, pattern="^adm_reset_menus_do$"))
    app.add_handler(CallbackQueryHandler(cb_adm_reset_confirm, pattern="^adm_reset_confirm$"))
    app.add_handler(CallbackQueryHandler(cb_adm_reset_do, pattern="^adm_reset_do$"))
    app.add_handler(CallbackQueryHandler(cb_restore_confirm, pattern="^restore_confirm$"))
    app.add_handler(CallbackQueryHandler(cb_restore_cancel, pattern="^restore_cancel$"))

    app.add_handler(MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, handle_restore_upload))
    app.add_handler(MessageHandler((filters.PHOTO | filters.VIDEO) & filters.ChatType.PRIVATE, handle_admin_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

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
