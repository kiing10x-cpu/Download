"""
Instagram Reel Downloader + Dynamic Premium Bot
------------------------------------------------
Core idea: every menu (start, help, etc.) is a DB record — text, image,
buttons, auto-delete — edited live from the admin panel, never hardcoded.
See README section at the bottom of this file for what's implemented vs
intentionally skipped for scope.

Setup:
  pip install -r requirements.txt
  pip install "qrcode[pil]"   # optional but recommended — local branded
                               # payment QR codes instead of a remote URL
  export BOT_TOKEN="123:ABC"
  export OWNER_ID="123456789"
  # optional:
  export MONGO_URI="mongodb+srv://..."
  export BACKUP_INTERVAL_HOURS="12"
  python3 bot.py
"""

import os
import io
import re
import json
import csv
import time
import shutil
import asyncio
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
    ChatMemberHandler,
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

# ----------------------------------------------------------------------------
# Local branded QR generation (replaces the old api.qrserver.com URL, which
# gave a plain black-on-white square and depended on a third-party service
# being reachable, separately from the bot itself). Same graceful-fallback
# pattern as ffmpeg above: works best with `qrcode[pil]` installed, degrades
# to a plain local QR if only `qrcode` is present, and falls all the way
# back to the old remote-URL QR only if `qrcode` isn't installed at all.
# ----------------------------------------------------------------------------
QRCODE_AVAILABLE = False
QRCODE_STYLED_AVAILABLE = False
try:
    import qrcode
    from qrcode.image.styledpil import StyledPilImage
    from qrcode.image.styles.moduledrawers import RoundedModuleDrawer
    from qrcode.image.styles.colormasks import SolidFillColorMask

    QRCODE_AVAILABLE = True
    QRCODE_STYLED_AVAILABLE = True
except ImportError:
    try:
        import qrcode

        QRCODE_AVAILABLE = True
    except ImportError:
        pass

try:
    from PIL import Image, ImageDraw, ImageFont, ImageOps

    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

if QRCODE_AVAILABLE and PIL_AVAILABLE:
    log.info(
        "QR generation: local %s QR enabled.",
        "styled (rounded + branded)" if QRCODE_STYLED_AVAILABLE else "plain",
    )
else:
    log.warning(
        "qrcode/Pillow not found — payment QR codes will fall back to the "
        "remote api.qrserver.com URL. Run `pip install \"qrcode[pil]\"` for "
        "nicer, fully local QR codes that don't depend on a third party."
    )

UPI_QR_BRAND_COLOR = (0, 135, 90)  # UPI-style green (kept as a fallback tint)

# Dark neon-card look (matches the requested reference design): near-black
# card, purple -> cyan gradient border, white rounded panel holding the
# actual black-on-white QR (max contrast = most reliably scannable), and a
# circular center logo with a soft colored ring.
QR_CARD_BG = (10, 10, 14)
QR_GRADIENT_A = (147, 51, 234)   # purple
QR_GRADIENT_B = (56, 189, 248)   # cyan
QR_TEXT_LIGHT = (235, 235, 245)
QR_TEXT_MUTED = (150, 150, 165)

# Keep the logo comfortably inside the ~30% recovery budget of
# ERROR_CORRECT_H so the code stays scannable even after we cover the
# center with a photo. 20% of the QR's own width (not the outer card) is a
# safe, well-tested ratio.
QR_LOGO_RATIO = 0.20


def _diagonal_gradient(size, c1, c2):
    """A simple top-left -> bottom-right gradient image, built with row/col
    interpolation (fast enough for a one-off card, no numpy dependency)."""
    w, h = size
    grad = Image.new("RGB", size)
    max_t = (w - 1) + (h - 1) or 1
    row_cache = {}
    pixels = grad.load()
    for y in range(h):
        for x in range(w):
            key = x + y
            rgb = row_cache.get(key)
            if rgb is None:
                t = key / max_t
                rgb = (
                    int(c1[0] + (c2[0] - c1[0]) * t),
                    int(c1[1] + (c2[1] - c1[1]) * t),
                    int(c1[2] + (c2[2] - c1[2]) * t),
                )
                row_cache[key] = rgb
            pixels[x, y] = rgb
    return grad


def _paste_center_logo(qr_img: "Image.Image", logo_bytes: bytes):
    """Paste a circular avatar in the middle of the QR with a white buffer
    ring underneath it, sized so the code is still reliably scannable."""
    logo = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")
    logo_size = int(qr_img.width * QR_LOGO_RATIO)
    logo = ImageOps.fit(logo, (logo_size, logo_size), Image.LANCZOS)

    mask = Image.new("L", (logo_size, logo_size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, logo_size, logo_size), fill=255)

    # white buffer ring so no QR module directly under the logo's edge is
    # left half-covered/ambiguous to a scanner
    ring_size = int(logo_size * 1.22)
    ring_mask = Image.new("L", (ring_size, ring_size), 0)
    ImageDraw.Draw(ring_mask).ellipse((0, 0, ring_size, ring_size), fill=255)

    qr_rgba = qr_img.convert("RGBA")
    rx = (qr_rgba.width - ring_size) // 2
    ry = (qr_rgba.height - ring_size) // 2
    white_ring = Image.new("RGBA", (ring_size, ring_size), (255, 255, 255, 255))
    qr_rgba.paste(white_ring, (rx, ry), ring_mask)

    lx = (qr_rgba.width - logo_size) // 2
    ly = (qr_rgba.height - logo_size) // 2
    qr_rgba.paste(logo, (lx, ly), mask)
    return qr_rgba.convert("RGB")


def generate_branded_qr(data: str, amount=None, caption: str = "Scan with any UPI app", logo_bytes: bytes = None):
    """Build the dark, gradient-bordered branded QR card (amount + caption
    baked into the image), optionally with a circular logo (e.g. the paying
    user's Telegram profile photo) in the center. Returns an in-memory PNG,
    or None if qrcode/Pillow aren't installed so callers can fall back to
    the old remote-URL QR."""
    if not (QRCODE_AVAILABLE and PIL_AVAILABLE):
        return None
    try:
        # ERROR_CORRECT_H = up to ~30% of the code can be damaged/covered
        # and it still scans — required here since the center gets covered
        # by the logo. Plain black-on-white modules (not colored/rounded)
        # keep contrast at its safest maximum for real-world UPI scanners.
        qr = qrcode.QRCode(
            error_correction=qrcode.constants.ERROR_CORRECT_H,
            box_size=10,
            border=3,
        )
        qr.add_data(data)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

        if logo_bytes:
            try:
                qr_img = _paste_center_logo(qr_img, logo_bytes)
            except Exception:
                log.exception("Failed to paste center logo onto QR — continuing without it")

        pad = 36
        panel_pad = 20  # white rounded panel margin around the raw QR
        header_h = 56 if amount is not None else 0
        footer_h = 40
        border_w = 6
        radius = 40

        panel_w = qr_img.width + panel_pad * 2
        panel_h = qr_img.height + panel_pad * 2
        canvas_w = panel_w + pad * 2
        canvas_h = panel_h + pad * 2 + header_h + footer_h

        canvas = Image.new("RGB", (canvas_w, canvas_h), QR_CARD_BG)
        draw = ImageDraw.Draw(canvas)

        # gradient border (purple -> cyan), drawn as a stroke via a mask so
        # it only affects the outline, not the whole card
        border_mask = Image.new("L", (canvas_w, canvas_h), 0)
        ImageDraw.Draw(border_mask).rounded_rectangle(
            [0, 0, canvas_w - 1, canvas_h - 1], radius=radius, outline=255, width=border_w
        )
        gradient = _diagonal_gradient((canvas_w, canvas_h), QR_GRADIENT_A, QR_GRADIENT_B)
        canvas.paste(gradient, (0, 0), border_mask)

        try:
            font_big = ImageFont.truetype("DejaVuSans-Bold.ttf", 30)
            font_small = ImageFont.truetype("DejaVuSans.ttf", 16)
        except Exception:
            font_big = ImageFont.load_default()
            font_small = ImageFont.load_default()

        y = pad // 2 + 6
        if amount is not None:
            amt_text = f"₹{amount}"
            w = draw.textlength(amt_text, font=font_big)
            draw.text(((canvas_w - w) / 2, y), amt_text, fill=QR_TEXT_LIGHT, font=font_big)
            y += header_h

        # white rounded panel behind the QR — maximum contrast for scanning,
        # matches the reference card's "white square in a dark frame" look
        panel_x, panel_y = pad, y
        draw.rounded_rectangle(
            [panel_x, panel_y, panel_x + panel_w - 1, panel_y + panel_h - 1],
            radius=24, fill=(255, 255, 255),
        )
        canvas.paste(qr_img, (panel_x + panel_pad, panel_y + panel_pad))
        y = panel_y + panel_h + 14

        w = draw.textlength(caption, font=font_small)
        draw.text(((canvas_w - w) / 2, y), caption, fill=QR_TEXT_MUTED, font=font_small)

        buf = io.BytesIO()
        canvas.save(buf, format="PNG")
        buf.seek(0)
        buf.name = "payment_qr.png"
        return buf
    except Exception:
        log.exception("Local QR generation failed, falling back to remote QR service")
        return None


async def fetch_user_avatar_bytes(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Fetch the user's current Telegram profile photo (highest available
    resolution) as raw bytes, for use as the QR's center logo. Returns None
    if the user has no profile photo or it can't be fetched — callers must
    treat that as "no logo" and continue, never as an error."""
    try:
        photos = await context.bot.get_user_profile_photos(user_id, limit=1)
        if not photos or not photos.photos:
            return None
        file_id = photos.photos[0][-1].file_id  # last = largest size available
        tg_file = await context.bot.get_file(file_id)
        data = await tg_file.download_as_bytearray()
        return bytes(data)
    except Exception:
        log.exception("Could not fetch Telegram avatar for user %s, QR will use no logo", user_id)
        return None


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

# Wrapped in to_small_caps() right here (not just at render time) so the
# `if text == RKB_DOWNLOAD:` string-matching in handle_text() still lines up
# with what the reply-keyboard button actually sends back — to_small_caps()
# is idempotent (re-applying it to already-styled text is a safe no-op), so
# this can't get out of sync with styled_kb_button()'s own wrapping below.
RKB_DOWNLOAD = to_small_caps("⬇️ Download reel")
RKB_USAGE = to_small_caps("📊 My usage")
RKB_GIFT = to_small_caps("🎁 Send a gift")
RKB_LANGUAGE = to_small_caps("🌐 Language")
RKB_DEVELOPER = to_small_caps("👨‍💻 Developer")
RKB_HOWTO = to_small_caps("📘 How to use")
RKB_SUPPORT = to_small_caps("🎧 Support")


def main_reply_keyboard() -> ReplyKeyboardMarkup:
    """v2 Section 1 — persistent bottom keyboard, alongside the existing
    inline /start menu (doesn't replace it). Colors via Bot API 9.4 `style`
    (needs PTB 22.7+, already pinned in requirements.txt)."""
    return ReplyKeyboardMarkup(
        [
            [styled_kb_button(RKB_DOWNLOAD, style="success")],
            [styled_kb_button(RKB_USAGE, style="primary"), styled_kb_button(RKB_GIFT, style="primary")],
            [styled_kb_button(RKB_LANGUAGE, style="primary"), styled_kb_button(RKB_DEVELOPER, style="primary")],
            [styled_kb_button(RKB_HOWTO, style="danger"), styled_kb_button(RKB_SUPPORT, style="danger")],
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

try:
    KeyboardButton(text="probe", style="primary")
    SUPPORTS_KB_BUTTON_STYLE = True
except TypeError:
    SUPPORTS_KB_BUTTON_STYLE = False


def styled_button(text, callback_data=None, url=None, style=None):
    """Every inline button in the bot goes through here (dynamic menu
    buttons, admin panel, gift/ticket/force-join flows, etc.) — so applying
    small-caps once, centrally, gives every button in the bot the small-caps
    look without having to touch 100+ individual call sites. Safe to call
    on text that's already small-caps (to_small_caps is idempotent)."""
    kwargs = {}
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if url is not None:
        kwargs["url"] = url
    if style and SUPPORTS_BUTTON_STYLE:
        kwargs["style"] = style
    return InlineKeyboardButton(to_small_caps(str(text)), **kwargs)


def styled_kb_button(text, style=None):
    """Same small-caps treatment as styled_button(), for the persistent
    bottom reply-keyboard buttons."""
    text = to_small_caps(str(text))
    if style and SUPPORTS_KB_BUTTON_STYLE:
        return KeyboardButton(text, style=style)
    return KeyboardButton(text)


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
        "force_join_channel": None,   # v3 §7 — @channelusername or -100id; None = disabled
        "send_as_document": False,    # v3 §8 — send reels as document instead of video
        "document_mode_threshold_mb": 45,  # auto-switch to document above this size
        "premium_plans": [],  # v4 — admin-defined plans: {id, name, days, price_inr, price_stars, enabled}
        "detailed_join_alerts": True,  # new-user/group-start full details -> admin DMs + logger
        "user_activity_dm": True,      # every reel-link a user sends -> owner DM (misuse monitoring)
    },
    "broadcast_log": [],
    "activity_log": [],         # ring buffer: {time, user_id, name, username, chat_type, url}
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
    "next_plan_id": 1,            # v4 — admin-defined premium plans
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


def is_premium_active(uid: str) -> bool:
    """BUGFIX #1/#3 — a user counts as premium only while plan != Free AND
    (no expiry set, or expiry is in the future)."""
    u = BOT_DATA["users"].get(uid, {})
    if u.get("plan", "Free") == "Free":
        return False
    exp = u.get("plan_expires_at")
    if not exp:
        return True
    try:
        return datetime.fromisoformat(exp) > datetime.utcnow()
    except Exception:
        return True


def grant_premium(uid: str, days: int = 30):
    """BUGFIX #3 — used by both Stars payments and admin-confirmed UPI orders
    so a successful gift actually upgrades the user's plan."""
    u = BOT_DATA["users"].setdefault(uid, {})
    u["plan"] = "Premium"
    u["plan_expires_at"] = (datetime.utcnow() + timedelta(days=days)).isoformat()
    save_data()


def check_daily_limit(uid: str) -> bool:
    """BUGFIX #1 — was defined implicitly (limit shown in /usage) but never
    actually enforced before a download. Premium users are unlimited."""
    if is_premium_active(uid):
        return True
    u = BOT_DATA["users"].get(uid, {})
    today = datetime.utcnow().strftime("%Y-%m-%d")
    today_count = u.get("downloads_today", 0) if u.get("downloads_today_date") == today else 0
    limit = BOT_DATA["settings"].get("daily_limit", 20)
    return today_count < limit


async def cm_track_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """v3 §6 — populate BOT_DATA['groups'] (was always empty, so the
    'Groups' stat in admin panel never reflected reality)."""
    cmu = update.my_chat_member
    if not cmu or cmu.chat.type not in ("group", "supergroup"):
        return
    gid = str(cmu.chat.id)
    new_status = cmu.new_chat_member.status
    if new_status in ("member", "administrator"):
        BOT_DATA["groups"][gid] = {
            "title": cmu.chat.title, "added_at": datetime.utcnow().isoformat(),
        }
    elif new_status in ("left", "kicked"):
        BOT_DATA["groups"].pop(gid, None)
    save_data()


async def is_force_join_ok(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """v3 §7 — if admin set a force-join channel, block non-members.

    BUGFIX — this used to swallow EVERY exception and silently fail OPEN
    (treat the user as verified) with zero logging anywhere. In practice
    that meant a bare username typed without '@', or the bot simply not
    being an admin in that channel, made force-join look "set" in the
    panel while never actually blocking a single person — with no way to
    tell why. We still fail open on error (a broken force-join shouldn't
    brick the whole bot for every user), but now the real reason is logged
    to the Activity Log so it's actually diagnosable."""
    channel = BOT_DATA["settings"].get("force_join_channel")
    if not channel or is_admin(user_id):
        return True
    try:
        member = await context.bot.get_chat_member(chat_id=channel, user_id=user_id)
        return member.status not in ("left", "kicked")
    except Exception as e:
        log.warning("Force-join check failed (channel=%s, user=%s): %s", channel, user_id, e)
        entries = BOT_DATA.setdefault("error_log", [])
        entries.append({
            "time": datetime.utcnow().isoformat(),
            "error": f"force_join check failed (channel={channel}): {e}",
        })
        if len(entries) > 200:
            del entries[: len(entries) - 200]
        return True  # fail-open so a misconfigured channel doesn't brick the bot


async def resolve_force_join_link(context: ContextTypes.DEFAULT_TYPE, channel) -> str | None:
    """BUGFIX — build a link that actually opens. Private channels are set
    by numeric ID (-100...), which has NO public https://t.me/<id> page —
    the old code built exactly that broken link, so the '📢 Join Channel'
    button led nowhere for any admin who configured force-join with a
    numeric ID (the documented, suggested way to do it for private
    channels). Now we resolve a real invite link via the Bot API."""
    if not channel:
        return None
    ch = str(channel)
    if ch.startswith("http"):
        return ch
    if ch.lstrip("-").isdigit():
        try:
            chat = await context.bot.get_chat(int(ch))
            if chat.username:
                return f"https://t.me/{chat.username}"
            if getattr(chat, "invite_link", None):
                return chat.invite_link
            return await context.bot.export_chat_invite_link(int(ch))
        except Exception as e:
            log.warning("Force-join: could not resolve invite link for %s: %s", ch, e)
            return None
    return f"https://t.me/{ch.lstrip('@')}"


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


async def dm_all_admins(context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None):
    """Send a message to every admin's private chat (owner + BOT_DATA['admins']).
    One admin having blocked the bot / never opened a DM must never stop the
    others from getting it, so each send is isolated."""
    targets = set(BOT_DATA.get("admins", []))
    if OWNER_ID:
        targets.add(OWNER_ID)
    for admin_id in targets:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text, reply_markup=reply_markup)
        except Exception:
            log.warning("Could not DM admin %s (bot blocked / never started a DM)", admin_id)


def build_join_details(update: Update, is_new: bool) -> str:
    """Full detail card for a /start — new user OR bot started inside a
    group — so admins get the complete picture in one glance."""
    user = update.effective_user
    chat = update.effective_chat
    lines = [
        "🆕 " + ("New User Started Bot" if is_new else "Bot Started In Group") + "",
        "",
        f"👤 Name: {user.full_name}",
        f"🔗 Username: @{user.username}" if user.username else "🔗 Username: (none)",
        f"🆔 User ID: {user.id}",
        f"🌐 Language: {user.language_code or 'unknown'}",
        f"⭐ Telegram Premium: {'Yes' if getattr(user, 'is_premium', False) else 'No'}",
        f"💬 Chat type: {chat.type}",
    ]
    if chat.type in ("group", "supergroup"):
        lines.append(f"👨‍👩‍👧 Group: {chat.title}")
        lines.append(f"🆔 Group ID: {chat.id}")
    lines.append(f"🕒 Time (UTC): {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}")
    return "\n".join(lines)


async def notify_admins_new_start(context: ContextTypes.DEFAULT_TYPE, update: Update, is_new: bool):
    """New user starts the bot, OR the bot is (re-)started inside a group —
    full details go to every admin's DM, and to the logger group if one is
    configured, per spec."""
    if not BOT_DATA["settings"].get("detailed_join_alerts", True):
        return
    is_group = update.effective_chat.type in ("group", "supergroup")
    if not (is_new or is_group):
        return
    text = build_join_details(update, is_new)
    await dm_all_admins(context, text)
    await log_event(context, text)


async def log_user_activity(context: ContextTypes.DEFAULT_TYPE, update: Update, url: str):
    """Anti-misuse monitoring: record what a user pastes into the bot and
    surface it live to the owner's DM (+ logger group), with a one-tap Ban
    button — this is a check/monitoring tool only, not automatic action."""
    user = update.effective_user
    chat = update.effective_chat
    entry = {
        "time": datetime.utcnow().isoformat(),
        "user_id": user.id,
        "name": user.full_name,
        "username": user.username,
        "chat_type": chat.type,
        "url": url,
    }
    buf = BOT_DATA.setdefault("activity_log", [])
    buf.append(entry)
    if len(buf) > 300:
        del buf[: len(buf) - 300]
    save_data()

    if not BOT_DATA["settings"].get("user_activity_dm", True):
        return
    uname = f"@{user.username}" if user.username else "(no username)"
    text = (
        "🕵️ Live Activity\n\n"
        f"👤 {user.full_name} {uname}\n"
        f"🆔 {user.id}\n"
        f"🕒 {entry['time'][11:19]} UTC\n"
        f"🔗 {url}"
    )
    await dm_all_admins(context, text)
    await log_event(context, text)


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
            if owner_id_str.isdigit():
                # Same tg://user?id= reliability issue as the developer
                # button — resolve the real @username via getChat when we can.
                url = f"tg://user?id={owner_id_str}"
                try:
                    chat = await context.bot.get_chat(int(owner_id_str))
                    if chat.username:
                        url = f"https://t.me/{chat.username}"
                except Exception:
                    pass
            else:
                url = f"https://t.me/{owner_id_str.lstrip('@')}"
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


def remember_panel_message(context: ContextTypes.DEFAULT_TYPE, query, screen_key: str):
    """BUGFIX (buttons feel 'dead') — when an admin taps 'Set X', the panel
    message stays on screen unchanged while the bot waits for a text reply.
    Once the value is saved, that same panel used to just sit there stale —
    the only feedback was a small extra confirmation message further down
    the chat, easy to miss, and it never said which of many possible
    settings screens it belonged to. We remember exactly which panel
    message triggered this input, so we can edit THAT message in place
    once the value is saved — the admin sees the screen itself flip to the
    new value immediately, not just a text line."""
    context.user_data["panel_refresh"] = {
        "chat_id": query.message.chat_id,
        "message_id": query.message.message_id,
        "screen": screen_key,
    }


async def refresh_panel_after_save(context: ContextTypes.DEFAULT_TYPE, screen_key: str, build_fn) -> bool:
    """Edits the original admin-panel screen (remembered via
    remember_panel_message) in place to reflect a just-saved value.
    Returns True if it succeeded, so callers can still send a plain
    confirmation as a fallback if the panel message is gone."""
    info = context.user_data.get("panel_refresh")
    if not info or info.get("screen") != screen_key:
        return False
    context.user_data.pop("panel_refresh", None)
    text, kb = build_fn()
    try:
        await context.bot.edit_message_text(
            chat_id=info["chat_id"], message_id=info["message_id"], text=text, reply_markup=kb,
        )
        return True
    except Exception:
        return False


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
        await update.message.reply_text("⏳ " + to_small_caps("slow down, too many requests too fast."))
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
    save_data()
    # New user, or bot (re)started inside a group — full detail card to
    # every admin's DM + the logger group (if configured).
    await notify_admins_new_start(context, update, is_new)

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

    # BUGFIX #2 (part 2) — non-text media that isn't part of an active ticket
    # or awaited-input flow has nothing to do here; don't fall through to the
    # "not a valid reel link" text reply for a bare photo/video.
    if not text and not context.user_data.get("awaiting"):
        return

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
        await update.message.reply_text("⏳ " + to_small_caps("slow down, too many requests too fast."))
        return

    match = INSTAGRAM_URL_RE.search(text)
    if match and not is_admin(user_id) and not check_daily_limit(uid):
        limit = BOT_DATA["settings"].get("daily_limit", 20)
        kb = InlineKeyboardMarkup([[styled_button(to_small_caps("🚀 upgrade for more"), callback_data="gift_menu", style="success")]])
        await update.message.reply_text(
            "🚫 " + to_small_caps(f"daily limit reached ({limit}/{limit}). try again tomorrow or upgrade."),
            reply_markup=kb,
        )
        return

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

    if not await is_force_join_ok(context, user_id):
        channel = BOT_DATA["settings"].get("force_join_channel")
        # BUGFIX — numeric channel IDs used to build a dead https://t.me/-100...
        # link; resolve a real, clickable invite link instead.
        ch_link = await resolve_force_join_link(context, channel)
        kb_rows = []
        if ch_link:
            kb_rows.append([InlineKeyboardButton(to_small_caps("📢 join channel"), url=ch_link)])
        kb_rows.append([styled_button(to_small_caps("✅ i've joined"), callback_data="check_force_join", style="success")])
        text = "🔒 " + to_small_caps("please join our channel first to use this bot.")
        if not ch_link:
            text += "\n⚠️ " + to_small_caps("couldn't get a join link — please contact an admin.")
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb_rows))
        return

    url = match.group(1)

    # Anti-misuse monitoring: every reel link a user pastes is logged and,
    # by default, forwarded live to admin DMs (+ logger group) with a
    # one-tap ban button — this is a check-only feed, no automatic action.
    if not is_admin(user_id):
        await log_user_activity(context, update, url)

    if is_link_blocked(url) and not is_admin(user_id):
        await update.message.reply_text("🚫 " + to_small_caps("this link/domain has been blocked by admin."))
        return

    status_msg = await update.message.reply_text(STR["processing"])

    # v3 §8 — real download progress (fed by yt-dlp's progress_hooks from the
    # worker thread) instead of just a canned 3-stage loop.
    _progress = {"pct": 0, "stage": "fetching"}

    def _progress_hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            _progress["pct"] = int(done / total * 100) if total else 0
            _progress["stage"] = "fetching"
        elif d.get("status") == "finished":
            _progress["pct"] = 100
            _progress["stage"] = "optimizing"

    async def _animate_status():
        try:
            while True:
                await asyncio.sleep(2)
                pct = _progress["pct"]
                filled = pct // 10
                bar = "▓" * filled + "░" * (10 - filled)
                stage_label = to_small_caps(_progress["stage"])
                try:
                    await status_msg.edit_text(
                        to_small_caps("⏳ processing your reel...") + f"\n📥 {stage_label}\n{bar} {pct}%"
                    )
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass

    anim_task = asyncio.create_task(_animate_status())
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
            "progress_hooks": [_progress_hook],
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
            # BUGFIX #4 — run_download() is a blocking (sync) yt-dlp call; it
            # was being awaited directly, which froze the whole bot's event
            # loop (all users) during every single download. Runs in a
            # thread now so the loop — and the animation above — keep going.
            file_path, ig_caption, ig_uploader = await asyncio.to_thread(run_download, FFMPEG_AVAILABLE)
        except Exception as e:
            # Self-heal: if a merge was attempted and ffmpeg turned out to be
            # the problem, retry once with a no-merge (progressive) format.
            if "ffmpeg" in str(e).lower():
                log.warning("Merge failed (ffmpeg issue), retrying with progressive format.")
                file_path, ig_caption, ig_uploader = await asyncio.to_thread(run_download, False)
            else:
                raise

        # BUGFIX #5 — Telegram bots can't upload files over 50MB; previously
        # a big reel would silently hang/fail with a raw exception. Check
        # size upfront and give a clear message instead of attempting upload.
        MAX_UPLOAD_BYTES = 50 * 1024 * 1024
        file_size = os.path.getsize(file_path) if file_path and os.path.exists(file_path) else 0
        if file_size > MAX_UPLOAD_BYTES:
            anim_task.cancel()
            mb = file_size / (1024 * 1024)
            await status_msg.edit_text(
                "❌ " + to_small_caps(f"this reel is too large to send ({mb:.1f}mb, limit 50mb). try a shorter reel.")
            )
            if os.path.exists(file_path):
                os.remove(file_path)
            return

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

        anim_task.cancel()
        protect = bool(BOT_DATA["settings"].get("lock_all_content", False))
        # v3 §8 — send as document when admin forces it, or file is close to
        # the 50MB cap (documents preserve quality better near the limit).
        threshold = BOT_DATA["settings"].get("document_mode_threshold_mb", 45) * 1024 * 1024
        as_document = BOT_DATA["settings"].get("send_as_document", False) or file_size > threshold
        with open(file_path, "rb") as vid:
            if as_document:
                sent = await update.message.reply_document(
                    document=vid, caption=result_caption, parse_mode=parse_mode, reply_markup=kb, protect_content=protect
                )
            else:
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
        anim_task.cancel()
        log.exception("Download failed")
        # BUGFIX — `e` was interpolated into an HTML-parsed message unescaped.
        # Any "<" ">" "&" in a yt-dlp error (common in URLs) broke Telegram's
        # HTML parser, edit_text raised, and the user was left staring at a
        # frozen "processing..." message forever with no visible error.
        import html as _html
        safe_err = _html.escape(str(e))[:500]
        try:
            await status_msg.edit_text(
                "❌ " + to_small_caps("download failed. the link may be private, deleted, or instagram rate-limited us.")
                + f"\n\n<code>{safe_err}</code>",
                parse_mode="HTML",
            )
        except Exception:
            await status_msg.edit_text(
                "❌ " + to_small_caps("download failed. the link may be private, deleted, or instagram rate-limited us.")
            )
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
        await query.message.reply_text("ℹ️ " + to_small_caps("no caption found for this post (or cache expired)."))
        return
    # Telegram message limit is 4096 chars — split if needed.
    for i in range(0, len(caption), 4000):
        await query.message.reply_text(caption[i:i + 4000])


async def cb_check_force_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    ok = await is_force_join_ok(context, update.effective_user.id)
    if ok:
        await query.answer(to_small_caps("✅ verified! send your reel link again."), show_alert=True)
        try:
            await query.message.delete()
        except Exception:
            pass
    else:
        await query.answer(to_small_caps("❌ still not joined."), show_alert=True)


async def cb_download_another(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("🔗 " + to_small_caps("paste your next instagram reel link."))


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
    # v4 — admin-added premium plans now list directly under Usage (not just
    # buried behind a generic "upgrade" button), so a newly-added plan is
    # visible to every user right away.
    s = BOT_DATA["settings"]
    plans = [p for p in s.get("premium_plans", []) if p.get("enabled")] if s.get("premium_enabled") else []
    if plans:
        text += "\n\n💎 " + to_small_caps("available plans") + "\n"
        for p in plans:
            price_bits = []
            if p.get("price_inr"):
                price_bits.append(f"₹{p['price_inr']}")
            if p.get("price_stars"):
                price_bits.append(f"{p['price_stars']}⭐")
            text += f"• {p['name']} — {' / '.join(price_bits)} — {p.get('days', 30)}{to_small_caps('d')}\n"
            kb_rows.append([styled_button(f"💎 {p['name']}", callback_data=f"gift_plan:{p['id']}", style="success")])
    elif s.get("premium_enabled"):
        kb_rows.append([styled_button(to_small_caps("🚀 upgrade for more"), callback_data="gift_menu", style="success")])
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    except Exception:
        pass
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb_rows) if kb_rows else None)


# ----------------------------------------------------------------------------
# v2 §5 — Developer button
# ----------------------------------------------------------------------------

def _normalize_username_link(value: str) -> str:
    value = value.strip()
    if value.startswith("http://") or value.startswith("https://") or value.startswith("tg://"):
        return value
    return f"https://t.me/{value.lstrip('@')}"


async def resolve_developer_url(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    link = BOT_DATA["settings"].get("developer_link")
    if link:
        return _normalize_username_link(link)
    dev_id = BOT_DATA["settings"].get("developer_id")
    if not dev_id:
        return None
    # tg://user?id=... only opens if the tapping user's Telegram client already
    # has that account cached (shared group, contact, etc.) — it silently does
    # nothing otherwise, which is the "click nahi khulta" bug. Resolving the
    # real @username via getChat and linking to t.me/username always works.
    try:
        chat = await context.bot.get_chat(dev_id)
        if chat.username:
            return f"https://t.me/{chat.username}"
    except Exception:
        pass
    return f"tg://user?id={dev_id}"


async def show_developer_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = await resolve_developer_url(context)
    if not url:
        await update.message.reply_text(to_small_caps("developer contact not set up yet."))
        return
    kb = InlineKeyboardMarkup([[styled_button("👨‍💻 " + to_small_caps("message developer"), url=url)]])
    await update.message.reply_text(to_small_caps("tap below to message the developer:"), reply_markup=kb)


# ----------------------------------------------------------------------------
# v2 §4 — Gift flow (Telegram Stars + UPI)
# ----------------------------------------------------------------------------

async def show_gift_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """v4 — pure voluntary support/tip flow. This is separate from Premium
    Plans (those live under Usage now) — this is just 'send a gift to
    support the bot', any amount, no plan attached."""
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
    await query.message.reply_text("✏️ " + to_small_caps("enter a numeric star amount (e.g. 150)."))


def find_premium_plan(pid: str):
    for p in BOT_DATA["settings"].get("premium_plans", []):
        if p["id"] == pid:
            return p
    return None


async def cb_gift_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User tapped a specific admin-defined plan in the gift menu — show only
    the payment methods the admin actually priced for this plan."""
    query = update.callback_query
    await query.answer()
    pid = query.data.split(":", 1)[1]
    plan = find_premium_plan(pid)
    if not plan or not plan.get("enabled"):
        await query.message.reply_text("⚠️ " + to_small_caps("this plan is no longer available."))
        return
    kb_rows = []
    if plan.get("price_stars"):
        kb_rows.append([styled_button(f"⭐ Pay {plan['price_stars']} Stars", callback_data=f"gift_plan_stars:{pid}", style="success")])
    if plan.get("price_inr") and BOT_DATA["settings"].get("upi_id"):
        kb_rows.append([styled_button(f"💳 Pay ₹{plan['price_inr']} via UPI", callback_data=f"gift_plan_upi:{pid}", style="primary")])
    if not kb_rows:
        await query.message.reply_text("⚠️ " + to_small_caps("no payment method available for this plan right now."))
        return
    await query.message.reply_text(
        f"💎 {plan['name']} — {plan.get('days', 30)} days\nChoose how to pay:",
        reply_markup=InlineKeyboardMarkup(kb_rows),
    )


async def cb_gift_plan_stars(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pid = query.data.split(":", 1)[1]
    plan = find_premium_plan(pid)
    if not plan or not plan.get("enabled") or not plan.get("price_stars"):
        await query.message.reply_text("⚠️ " + to_small_caps("this plan is no longer available."))
        return
    await send_stars_invoice(context, query.message.chat_id, plan["price_stars"], plan_id=pid, plan_name=plan["name"])


async def cb_gift_plan_upi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pid = query.data.split(":", 1)[1]
    plan = find_premium_plan(pid)
    if not plan or not plan.get("enabled") or not plan.get("price_inr"):
        await query.message.reply_text("⚠️ " + to_small_caps("this plan is no longer available."))
        return
    await start_upi_order(update, context, plan["price_inr"], plan_id=pid)


async def send_stars_invoice(context: ContextTypes.DEFAULT_TYPE, chat_id: int, amount: int, plan_id: str = None, plan_name: str = None):
    title = f"{plan_name} — Premium ⭐" if plan_name else "Gift the developer ⭐"
    desc = f"Unlock {plan_name}." if plan_name else f"Send {amount} Telegram Stars as a gift."
    await context.bot.send_invoice(
        chat_id=chat_id,
        title=title,
        description=desc,
        payload=f"stars_gift:{amount}:{chat_id}:{int(time.time())}:{plan_id or ''}",
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
    uid = str(update.effective_user.id)
    # v4 — a star payment for a specific admin-defined plan grants THAT plan's
    # day count; a generic/free-amount star gift keeps the old flat setting.
    plan_id = None
    parts = (sp.invoice_payload or "").split(":")
    if len(parts) >= 5 and parts[4]:
        plan_id = parts[4]
    plan = find_premium_plan(plan_id) if plan_id else None
    if plan:
        days = plan.get("days", 30)
        grant_premium(uid, days)
        text = (
            "✅ " + to_small_caps("payment successful!") + "\n\n"
            f"💎 {to_small_caps('plan')}: {plan['name']}\n"
            f"⭐ {to_small_caps('paid')}: {sp.total_amount} {to_small_caps('stars')}\n"
            f"⏳ {to_small_caps('premium unlocked for')} {days} {to_small_caps('days')}"
        )
        log_line = f"⭐ Plan purchased — {sp.total_amount} stars from {update.effective_user.id} ({plan['name']}), premium granted"
    else:
        # v4 — this is a voluntary support gift, not a plan purchase, so the
        # thank-you reads like a thank-you (not an invoice receipt), and it
        # no longer mixes plan-shaped wording in.
        days = BOT_DATA["settings"].get("premium_days_per_star_gift", 30)
        grant_premium(uid, days)
        text = (
            "🎉 " + to_small_caps("thank you so much for the support!") + "\n\n"
            f"💫 {to_small_caps('you sent')}: {sp.total_amount} ⭐\n"
            f"🎁 {to_small_caps('as a small thank-you, premium has been added for')} {days} {to_small_caps('days')}\n\n"
            + to_small_caps("it really means a lot — thank you! ❤️")
        )
        log_line = f"⭐ Gift received — {sp.total_amount} stars from {update.effective_user.id}, premium granted"
    await update.message.reply_text(text)
    await log_event(context, log_line)


async def cb_gift_upi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not BOT_DATA["settings"].get("upi_id"):
        await query.message.reply_text("⚠️ " + to_small_caps("upi is not configured yet."))
        return
    context.user_data["awaiting"] = "gift_upi_amount"
    await query.message.reply_text("💳 " + to_small_caps("enter amount (₹):"))


async def start_upi_order(update: Update, context: ContextTypes.DEFAULT_TYPE, amount: int, plan_id: str = None):
    upi_id = BOT_DATA["settings"].get("upi_id")
    oid = str(BOT_DATA["next_gift_id"])
    BOT_DATA["next_gift_id"] += 1
    expires_at = time.time() + 600  # 10 minutes
    order = {
        "id": oid, "user_id": update.effective_user.id, "amount": amount,
        "expires_at": expires_at, "status": "pending", "plan_id": plan_id,
    }
    BOT_DATA["gift_orders"][oid] = order
    save_data()
    upi_uri = f"upi://pay?pa={upi_id}&am={amount}&cu=INR&tn=Gift%20Order%20{oid}"
    # BUGFIX/UPGRADE — was a plain black-square QR from a third-party URL
    # (api.qrserver.com). Now generated locally: dark gradient-bordered
    # card, fixed amount + caption baked in, and the paying user's own
    # Telegram profile photo as the center logo. Falls back to the old
    # remote URL automatically if qrcode/Pillow aren't installed.
    avatar_bytes = await fetch_user_avatar_bytes(context, update.effective_user.id)
    qr_photo = generate_branded_qr(
        upi_uri, amount=amount, caption=to_small_caps("scan with any upi app"),
        logo_bytes=avatar_bytes,
    )
    if qr_photo is None:
        qr_photo = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={quote(upi_uri)}"
    kb = InlineKeyboardMarkup([[styled_button("✅ I've Paid", callback_data=f"gift_upi_paid:{oid}", style="success")]])
    msg = await context.bot.send_photo(
        chat_id=update.effective_chat.id, photo=qr_photo,
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
        # Auto-delete the QR photo from the chat on expiry (10 min), rather
        # than leaving a dead/expired QR sitting there. A small follow-up
        # notice replaces it so the user still has a way to retry.
        deleted = False
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            deleted = True
        except Exception:
            log.exception("Could not delete expired QR message %s/%s, falling back to caption edit", chat_id, message_id)
        kb = InlineKeyboardMarkup([[styled_button("🔁 Generate New QR", callback_data="gift_upi", style="primary")]])
        if deleted:
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="❌ " + to_small_caps("qr expired and was removed. generate a new one:"),
                    reply_markup=kb,
                )
            except Exception:
                pass
        else:
            try:
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
        await query.message.reply_text("❌ " + to_small_caps("qr expired, generate a new one via 🎁 send a gift."))
        return
    order["status"] = "claimed_pending_verify"
    save_data()
    await query.message.reply_text("✅ " + to_small_caps("marked as paid — an admin will verify shortly."))
    targets = BOT_DATA.get("admins", [])
    plan = find_premium_plan(order.get("plan_id")) if order.get("plan_id") else None
    kind = f"Plan purchase ({plan['name']})" if plan else "Support gift"
    admin_kb = InlineKeyboardMarkup([[
        styled_button("✅ Confirm & Upgrade", callback_data=f"gift_upi_confirm:{oid}", style="success"),
    ]])
    for target in targets:
        try:
            await context.bot.send_message(
                target,
                f"💳 UPI order #{oid} — {kind} — ₹{order['amount']} — user {order['user_id']} claims paid.",
                reply_markup=admin_kb,
            )
        except Exception:
            pass


async def cb_gift_upi_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """BUGFIX #3 — admin taps this to actually verify + upgrade the user;
    previously a UPI 'gift' never upgraded anyone even after being marked paid."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    oid = query.data.split(":", 1)[1]
    order = BOT_DATA["gift_orders"].get(oid)
    if not order or order["status"] == "paid":
        await query.answer("Already handled or not found.", show_alert=True)
        return
    order["status"] = "paid"
    save_data()
    uid = str(order["user_id"])
    # v4 — an order tied to a specific admin-defined plan grants THAT plan's
    # day count and reads like a purchase receipt; a generic/free-amount
    # UPI gift is a voluntary support tip, so it gets a proper thank-you
    # instead of plan-shaped wording.
    plan = find_premium_plan(order.get("plan_id")) if order.get("plan_id") else None
    if plan:
        days = plan.get("days", 30)
        user_text = (
            "✅ " + to_small_caps("payment confirmed!") + "\n\n"
            f"💎 {to_small_caps('plan')}: {plan['name']}\n"
            f"💳 {to_small_caps('paid')}: ₹{order['amount']}\n"
            f"⏳ {to_small_caps('premium unlocked for')} {days} {to_small_caps('days')}"
        )
        log_line = f"💳 UPI order #{oid} confirmed by admin {update.effective_user.id} ({plan['name']}), premium granted"
    else:
        days = BOT_DATA["settings"].get("premium_days_per_upi_gift", 30)
        user_text = (
            "🎉 " + to_small_caps("thank you so much for the support!") + "\n\n"
            f"💫 {to_small_caps('you sent')}: ₹{order['amount']}\n"
            f"🎁 {to_small_caps('as a small thank-you, premium has been added for')} {days} {to_small_caps('days')}\n\n"
            + to_small_caps("it really means a lot — thank you! ❤️")
        )
        log_line = f"💳 UPI order #{oid} confirmed by admin {update.effective_user.id}, premium granted"
    grant_premium(uid, days)
    try:
        await context.bot.send_message(order["user_id"], user_text)
    except Exception:
        pass
    try:
        await query.edit_message_reply_markup(None)
        await query.message.reply_text(f"✅ Order #{oid} confirmed, user upgraded.")
    except Exception:
        pass
    await log_event(context, log_line)


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
    await query.message.reply_text("🆘 " + to_small_caps("support") + "\n\n" + to_small_caps("describe your issue and we'll forward it to the team."))


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
        await update.message.reply_text("✅ " + to_small_caps("your message has been sent to support, we'll get back to you soon."))

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
            await update.message.reply_text("⚠️ " + to_small_caps("please send a valid star number."))
            return
        await send_stars_invoice(context, update.effective_chat.id, int(text))

    elif awaiting == "gift_upi_amount":
        context.user_data.pop("awaiting", None)
        if not text.isdigit() or int(text) <= 0:
            await update.message.reply_text("⚠️ " + to_small_caps("please send a valid ₹ amount."))
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
            [styled_button("💎 Premium Plans", callback_data="adm_premium"),
             styled_button("💳 UPI Settings", callback_data="adm_upi")],
            [styled_button("👨‍💻 Developer Settings", callback_data="adm_devsettings"),
             styled_button("🎧 Support Settings", callback_data="adm_support_settings")],
            [styled_button("🎫 Tickets", callback_data="adm_tickets"),
             styled_button("📊 Bot Stats", callback_data="adm_stats")],
            [styled_button("🌐 Language Settings", callback_data="adm_lang_manage"),
             styled_button("📢 Broadcast", callback_data="adm_broadcast")],
            [styled_button("👥 Users & Groups", callback_data="adm_users"),
             styled_button("🎨 Menu & UI", callback_data="adm_menu_ui")],
            [styled_button("⚙️ Settings & Admins", callback_data="adm_settings"),
             styled_button("🛑 Danger Zone", callback_data="adm_danger")],
            [styled_button("📋 Activity Log", callback_data="adm_activity"),
             styled_button("🧪 Self-Test", callback_data="adm_selftest")],
            [styled_button("🕵️ Live User Feed", callback_data="adm_live")],
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


# ---- v3 §10 — activity log + self-test ---------------------------------------

async def cb_adm_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    entries = BOT_DATA.get("error_log", [])[-15:]
    if not entries:
        body = "✅ " + to_small_caps("no recent errors logged.")
    else:
        lines = [f"• {e.get('time', '?')} — {str(e.get('error', e))[:100]}" for e in entries]
        body = "📋 " + to_small_caps("last 15 log entries") + "\n\n" + "\n".join(lines)
    await query.edit_message_text(body, reply_markup=InlineKeyboardMarkup([back_row("adm_home")]))


async def cb_adm_selftest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    await query.edit_message_text("🧪 " + to_small_caps("running self-test..."))
    results = []
    results.append(("Bot token", "✅ OK" if BOT_TOKEN else "❌ missing"))
    try:
        me = await context.bot.get_me()
        results.append(("Telegram API", f"✅ OK (@{me.username})"))
    except Exception as e:
        results.append(("Telegram API", f"❌ {e}"))
    results.append(("ffmpeg", "✅ found" if FFMPEG_AVAILABLE else "⚠️ not found (merge downloads may fail)"))
    try:
        import yt_dlp as _yd
        results.append(("yt-dlp", f"✅ v{_yd.version.__version__}"))
    except Exception as e:
        results.append(("yt-dlp", f"❌ {e}"))
    results.append(("Download dir", "✅ writable" if os.access(DOWNLOAD_DIR, os.W_OK) else "❌ not writable"))
    body = "🧪 " + to_small_caps("self-test results") + "\n\n" + "\n".join(f"{k}: {v}" for k, v in results)
    await query.edit_message_text(body, reply_markup=InlineKeyboardMarkup([back_row("adm_home")]))


# ---- Live User Feed (anti-misuse monitoring) ---------------------------------

async def _render_adm_live(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    col = get_mongo_collection()
    backend = "MongoDB ✅ (synced)" if col is not None else "Local (in-memory/JSON, no Mongo connected)"
    entries = BOT_DATA.get("activity_log", [])[-15:][::-1]
    lines = [f"🕵️ Live User Feed\n🗄 Backend: {backend}\n👥 Total tracked users: {len(BOT_DATA['users'])}\n"]
    if not entries:
        lines.append("Abhi tak koi activity record nahi hui.")
    else:
        for e in entries:
            uname = f"@{e['username']}" if e.get("username") else "(no username)"
            lines.append(f"• {e.get('time', '?')[11:19]} — {e.get('name')} {uname} [{e.get('user_id')}]\n  ↳ {e.get('url')}")
    body = "\n".join(lines)
    s = BOT_DATA["settings"]
    kb = InlineKeyboardMarkup(
        [
            [styled_button("🚫 Ban / Unban User (by ID)", callback_data="adm_quickban", style="danger")],
            [styled_button(
                f"📡 Feed To Admin DM: {'ON' if s.get('user_activity_dm', True) else 'OFF'}",
                callback_data="stgl:user_activity_dm:adm_live",
            )],
            [styled_button(
                f"🆕 Detailed Join Alerts: {'ON' if s.get('detailed_join_alerts', True) else 'OFF'}",
                callback_data="stgl:detailed_join_alerts:adm_live",
            )],
            back_row("adm_home"),
        ]
    )
    await query.edit_message_text(body, reply_markup=kb)


async def cb_adm_live(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    await _render_adm_live(update, context)


async def cb_adm_quickban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ban/Unban a user by ID, entered from the admin panel — this is the
    only place a ban actually happens; the Live Feed is view-only."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    context.user_data["awaiting"] = "adm_ban_unban_userid"
    await query.message.reply_text(
        "🚫 User ki numeric ID bhejo — agar already banned hai to unban ho jayega, "
        "warna ban ho jayega."
    )


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
            [styled_button("👨‍👩‍👧 List Groups", callback_data="adm_groups_list")],
            [styled_button("✉️ Message a User", callback_data="adm_users_msg")],
            back_row(),
            home_row(),
        ]
    )
    await query.edit_message_text("👥 Users & Groups", reply_markup=kb)


async def cb_adm_groups_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    groups = list(BOT_DATA["groups"].items())
    if not groups:
        text = "Bot abhi kisi group mein nahi hai."
    else:
        lines = [f"👨‍👩‍👧 Groups ({len(groups)})\n"]
        for gid, info in groups:
            lines.append(f"• {info.get('title', '(no title)')} — {gid}")
        text = "\n".join(lines)
    kb = InlineKeyboardMarkup([[styled_button("🔙 Back", callback_data="adm_users")]])
    await query.edit_message_text(text, reply_markup=kb)


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
            [styled_button("📢 New Broadcast", callback_data="adm_bc_new")],
            [styled_button(
                f"🔐 Forward-Lock: {'ON' if protect else 'OFF'}",
                callback_data="stgl:protect_broadcasts:adm_broadcast",
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
            styled_button("❌", callback_data=f"adm_btn_del:{menu_id}:{i}"),
        ])
    rows.append([styled_button("➕ Add Button", callback_data=f"adm_btn_add:{menu_id}")])
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
            styled_button("❌", callback_data=f"adm_btn_del:{menu_id}:{i}"),
        ])
    rows.append([styled_button("➕ Add Button", callback_data=f"adm_btn_add:{menu_id}")])
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
            )],
            [styled_button(f"⏱ Global Auto-Delete: {s.get('global_auto_delete_seconds', 0)}s", callback_data="adm_set_autodelete")],
            [styled_button(
                f"🅰️ Small-Caps Buttons: {'ON' if s.get('small_caps_buttons_default') else 'OFF'}",
                callback_data="stgl:small_caps_buttons_default:adm_settings",
            )],
            [styled_button("💬 Auto-Replies", callback_data="adm_autoreply_list")],
            [styled_button("🌐 Manage Languages", callback_data="adm_lang_manage")],
            [styled_button("👤 Manage Admins", callback_data="adm_manage_admins")],
            [styled_button("📥 Restore Backup", callback_data="adm_restore_info")],
            [styled_button(
                f"🔐 Lock All Forwarding: {'ON' if s.get('lock_all_content') else 'OFF'}",
                callback_data="stgl:lock_all_content:adm_settings",
            )],
            [styled_button("👑 Owner/Developer Contact", callback_data="adm_owner_contact")],
            [styled_button("📋 Logger Channel", callback_data="adm_logger_channel")],
            [styled_button(f"📢 Force-Join: {s.get('force_join_channel') or 'OFF'}", callback_data="adm_force_join")],
            [styled_button(
                f"📄 Send As Document: {'ON' if s.get('send_as_document') else 'OFF (auto near limit)'}",
                callback_data="stgl:send_as_document:adm_settings",
            )],
            back_row(),
            home_row(),
        ]
    )
    await query.edit_message_text("⚙️ Settings & Admins", reply_markup=kb)


async def cb_adm_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _render_adm_settings(update, context)


# ---- Owner/Developer credit button (#10) --------------------------------------

def _build_adm_owner_contact_view():
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
            [styled_button("✏️ Set Contact", callback_data="adm_owner_contact_set")],
            [styled_button("❌ Clear", callback_data="adm_owner_contact_clear")],
            back_row(),
        ]
    )
    return text, kb


async def _render_adm_owner_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    text, kb = _build_adm_owner_contact_view()
    await query.edit_message_text(text, reply_markup=kb)


async def cb_adm_owner_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _render_adm_owner_contact(update, context)


async def cb_adm_owner_contact_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    remember_panel_message(context, query, "owner_contact")
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

def _build_adm_logger_channel_view():
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
            )],
            back_row(),
        ]
    )
    return text, kb


async def _render_adm_logger_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    text, kb = _build_adm_logger_channel_view()
    await query.edit_message_text(text, reply_markup=kb)


async def cb_adm_logger_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _render_adm_logger_channel(update, context)


async def cb_adm_logger_channel_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    remember_panel_message(context, query, "logger_channel")
    context.user_data["awaiting"] = "logger_channel_id"
    await query.message.reply_text(
        "Forward any message from the target channel here (bot must be an "
        "admin there), or just type its numeric ID (looks like -100xxxxxxxxxx)."
    )


# ---- v3 §7 — Force-join channel ------------------------------------------------

def _build_adm_force_join_view():
    s = BOT_DATA["settings"]
    channel = s.get("force_join_channel")
    text = (
        "📢 Force-Join Channel\n\n"
        f"Channel: {channel or '(not set — force-join disabled)'}\n\n"
        "When set, users must be a member of this channel before they can "
        "download reels. Bot must be an admin of the channel to check membership.\n\n"
        "⚠️ Note: you and other bot admins ALWAYS bypass this check, by design — "
        "so testing with your own admin account will never show the block screen. "
        "Test with a normal (non-admin) account, or use 🧪 Test Now below."
    )
    kb_rows = [
        [styled_button("✏️ Set Channel", callback_data="adm_force_join_set")],
    ]
    if channel:
        kb_rows.append([styled_button("🧪 Test Now", callback_data="adm_force_join_test")])
    kb_rows.append([styled_button("❌ Disable", callback_data="adm_force_join_clear")])
    kb_rows.append(back_row())
    return text, InlineKeyboardMarkup(kb_rows)


async def _render_adm_force_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    text, kb = _build_adm_force_join_view()
    await query.edit_message_text(text, reply_markup=kb)


async def cb_adm_force_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _render_adm_force_join(update, context)


async def cb_adm_force_join_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    remember_panel_message(context, query, "force_join")
    context.user_data["awaiting"] = "force_join_channel"
    await query.message.reply_text(
        "Type the channel username (like @mychannel) or numeric ID (-100xxxxxxxxxx). "
        "Bot must already be an admin in that channel."
    )


async def cb_adm_force_join_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    BOT_DATA["settings"]["force_join_channel"] = None
    save_data()
    await _render_adm_force_join(update, context)


async def cb_adm_force_join_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Live diagnostic — actually calls the Bot API right now and shows the
    exact result/error, instead of the admin having to guess why nobody is
    getting blocked. This is the #1 real-world cause of 'force-join doesn't
    work': the bot silently isn't an admin in the target channel, or the
    channel string is wrong — and that used to only get logged, never shown."""
    query = update.callback_query
    await query.answer()
    channel = BOT_DATA["settings"].get("force_join_channel")
    if not channel:
        await query.message.reply_text("⚠️ No force-join channel is set.")
        return
    lines = [f"🧪 Testing force-join channel: {channel}\n"]
    try:
        me = await context.bot.get_me()
        chat = await context.bot.get_chat(channel)
        lines.append(f"✅ Bot can see the channel: {chat.title or chat.id}")
        member = await context.bot.get_chat_member(chat_id=channel, user_id=me.id)
        if member.status in ("administrator", "creator"):
            lines.append("✅ Bot IS an admin there — membership checks will work.")
        else:
            lines.append(
                "❌ Bot is a MEMBER but NOT an admin there — get_chat_member calls for "
                "other users will fail and force-join will silently fail OPEN "
                "(let everyone through). Make the bot an admin in this channel."
            )
    except Exception as e:
        lines.append(
            f"❌ Bot could NOT access this channel at all ({e}).\n"
            "This is almost always the reason force-join 'doesn't work' — the bot "
            "must be added to the channel as an ADMIN first. Double-check the "
            "username/ID too."
        )
    await query.message.reply_text("\n".join(lines))


async def cb_settings_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """BUGFIX — this used to hardcode only 3 valid `return_to` screens, so any
    toggle wired to a 4th screen (e.g. adm_premium) flipped the setting in the
    DB but never refreshed the message — the button looked completely dead.
    Now it looks up ANY registered admin screen via SCREEN_RENDERERS, so every
    current and future toggle button actually re-renders."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    _, key, return_to = query.data.split(":", 2)
    BOT_DATA["settings"][key] = not bool(BOT_DATA["settings"].get(key, False))
    save_data()
    renderer = SCREEN_RENDERERS.get(return_to)
    if renderer is not None:
        await renderer(update, context)
    else:
        # Still give feedback instead of doing nothing if a screen is missing.
        await query.answer(to_small_caps("✅ updated."), show_alert=False)


async def cb_adm_lang_manage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    langs = BOT_DATA["settings"].get("languages", [])
    text = "🌐 Enabled Languages\n\n" + ("\n".join(f"• {LANG_NAMES.get(c, c)}" for c in langs) if langs else "Koi nahi — sirf Default (Hinglish).")
    kb = InlineKeyboardMarkup(
        [
            [styled_button("➕ Add Language", callback_data="adm_lang_add")],
            [styled_button("➖ Remove Language", callback_data="adm_lang_remove")],
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
    # BUGFIX — `A + B or C` always evaluated truthy (header alone is
    # non-empty), so the "no auto-replies" fallback text never showed.
    lines = ["💬 Auto-Replies\n"] + (
        [f"• `{k}` → {v[:30]}" for k, v in replies.items()] or ["Koi auto-reply set nahi hai."]
    )
    kb = InlineKeyboardMarkup(
        [
            [styled_button("➕ Add", callback_data="adm_autoreply_add")],
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
            [styled_button("➕ Add Admin", callback_data="adm_add_admin")],
            [styled_button("➖ Remove Admin", callback_data="adm_remove_admin")],
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
            [styled_button("🧹 Clear Broadcast Log", callback_data="adm_clear_bclog")],
            [styled_button("🧹 Delete All Bot Messages In This Chat", callback_data="adm_delete_chat_msgs")],
            [styled_button("🔄 Reset Menus to Default", callback_data="adm_reset_menus_confirm")],
            [styled_button("❌ Reset ALL Bot Data", callback_data="adm_reset_confirm")],
            back_row(),
            home_row(),
        ]
    )
    await query.edit_message_text("🛑 Danger Zone\n(Ye actions destructive hain.)", reply_markup=kb)


# ---- v2 §8 new admin screens: Premium / UPI / Developer / Support / Tickets --

def _build_adm_premium_view():
    s = BOT_DATA["settings"]
    plans = s.get("premium_plans", [])
    lines = [
        f"💎 Premium Plans\n\nMaster switch: {'ON' if s.get('premium_enabled') else 'OFF'}",
        f"Daily free limit: {s.get('daily_limit', 20)}",
        "",
    ]
    kb_rows = [
        [styled_button(f"🔀 Master Switch: {'ON' if s.get('premium_enabled') else 'OFF'}",
                        callback_data="stgl:premium_enabled:adm_premium")],
        [styled_button("✏️ Set Daily Limit", callback_data="adm_set_dailylimit")],
    ]
    if not plans:
        lines.append("No plans yet — tap ➕ Add Plan below.")
    else:
        lines.append("Your plans (tap a plan's row buttons to toggle/delete):")
        for p in plans:
            state = "🟢 ON" if p.get("enabled") else "🔴 OFF"
            price_bits = []
            if p.get("price_inr"):
                price_bits.append(f"₹{p['price_inr']}")
            if p.get("price_stars"):
                price_bits.append(f"{p['price_stars']}⭐")
            price_str = " / ".join(price_bits) if price_bits else "(no price set)"
            lines.append(f"• {p['name']} — {price_str} — {p.get('days', 30)}d — {state}")
            kb_rows.append([
                styled_button(f"{'🔴 Turn Off' if p.get('enabled') else '🟢 Turn On'} · {p['name']}",
                              callback_data=f"adm_plan_toggle:{p['id']}"),
                styled_button("🗑", callback_data=f"adm_plan_del:{p['id']}"),
            ])
    kb_rows.append([styled_button("➕ Add Plan", callback_data="adm_plan_add")])
    kb_rows.append(back_row())
    kb_rows.append(home_row())
    text = "\n".join(lines) + (
        "\n\nWhen master switch is ON and a plan is toggled ON, that plan shows up "
        "immediately to every user in the 🎁 gift/upgrade menu."
    )
    return text, InlineKeyboardMarkup(kb_rows)


async def _render_adm_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    text, kb = _build_adm_premium_view()
    await query.edit_message_text(text, reply_markup=kb)


async def cb_adm_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _render_adm_premium(update, context)


# ---- Premium plan CRUD (add / toggle / delete) ---------------------------------

async def cb_adm_plan_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    remember_panel_message(context, query, "premium")
    context.user_data["new_plan"] = {}
    context.user_data["awaiting"] = "plan_step_name"
    await query.message.reply_text(
        "➕ New plan — step 1/4\nPlan ka naam bhejo (e.g. 'Monthly', 'Weekly Pro')."
    )


async def cb_adm_plan_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pid = query.data.split(":", 1)[1]
    for p in BOT_DATA["settings"].get("premium_plans", []):
        if p["id"] == pid:
            p["enabled"] = not p.get("enabled")
            save_data()
            await log_event(context, f"💎 Plan '{p['name']}' toggled {'ON' if p['enabled'] else 'OFF'} by {update.effective_user.id}")
            break
    await _render_adm_premium(update, context)


async def cb_adm_plan_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pid = query.data.split(":", 1)[1]
    plans = BOT_DATA["settings"].get("premium_plans", [])
    BOT_DATA["settings"]["premium_plans"] = [p for p in plans if p["id"] != pid]
    save_data()
    await _render_adm_premium(update, context)


async def cb_adm_set_dailylimit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    remember_panel_message(context, query, "premium")
    context.user_data["awaiting"] = "daily_limit"
    await query.message.reply_text("Naya daily free-download limit (number) bhejo.")


def _build_adm_upi_view():
    upi = BOT_DATA["settings"].get("upi_id")
    kb = InlineKeyboardMarkup([
        [styled_button("✏️ Set UPI ID", callback_data="adm_upi_set")],
        [styled_button("❌ Clear", callback_data="adm_upi_clear")],
        back_row(), home_row(),
    ])
    return f"💳 UPI Settings\n\nCurrent: {upi or '(not set)'}", kb


async def _render_adm_upi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    text, kb = _build_adm_upi_view()
    await query.edit_message_text(text, reply_markup=kb)


async def cb_adm_upi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _render_adm_upi(update, context)


async def cb_adm_upi_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    remember_panel_message(context, query, "upi")
    context.user_data["awaiting"] = "upi_id"
    await query.message.reply_text("UPI ID bhejo (e.g. name@bank).")


async def cb_adm_upi_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    BOT_DATA["settings"]["upi_id"] = None
    save_data()
    await query.edit_message_text("✅ UPI ID cleared.", reply_markup=InlineKeyboardMarkup([back_row()]))


def _build_adm_devsettings_view():
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
    return text, kb


async def _render_adm_devsettings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    text, kb = _build_adm_devsettings_view()
    await query.edit_message_text(text, reply_markup=kb)


async def cb_adm_devsettings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _render_adm_devsettings(update, context)


async def cb_adm_dev_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    remember_panel_message(context, query, "devsettings")
    context.user_data["awaiting"] = "developer_id"
    await query.message.reply_text("Developer ki numeric user ID bhejo.")


async def cb_adm_dev_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    remember_panel_message(context, query, "devsettings")
    context.user_data["awaiting"] = "developer_link"
    await query.message.reply_text("t.me/username link bhejo (ya 'clear' likh do hatane ke liye).")


def _build_adm_support_settings_view():
    gid = BOT_DATA["settings"].get("admin_group_id")
    kb = InlineKeyboardMarkup([
        [styled_button("✏️ Set Ticket Group", callback_data="adm_group_set")],
        back_row(), home_row(),
    ])
    return f"🎧 Support Settings\n\nTicket group: {gid or '(not set — falls back to admin DMs)'}", kb


async def _render_adm_support_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    text, kb = _build_adm_support_settings_view()
    await query.edit_message_text(text, reply_markup=kb)


async def cb_adm_support_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _render_adm_support_settings(update, context)


async def cb_adm_group_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    remember_panel_message(context, query, "support_settings")
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
        [[styled_button("⚠️ Haan, reset karo", callback_data="adm_reset_menus_do"),
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
        [[styled_button("⚠️ Haan, sab reset karo", callback_data="adm_reset_do"),
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
        refreshed = await refresh_panel_after_save(context, "owner_contact", _build_adm_owner_contact_view)
        await update.message.reply_text(f"✅ Owner/Developer contact set: {label} → {text}" + ("" if refreshed else "\n(panel screen above may be stale — reopen it to confirm)"))

    elif awaiting == "force_join_channel":
        context.user_data.pop("awaiting", None)
        channel = text.strip()
        # BUGFIX — a bare username typed without '@' (e.g. "mychannel"
        # instead of "@mychannel") makes get_chat_member reject the call
        # outright; that exception used to be swallowed silently, so
        # force-join looked "configured" but never blocked anyone.
        if channel and not channel.startswith("http") and not channel.lstrip("-").isdigit() and not channel.startswith("@"):
            channel = "@" + channel
        BOT_DATA["settings"]["force_join_channel"] = channel
        save_data()
        # BUGFIX — verify RIGHT NOW instead of letting the admin find out
        # days later that nobody was ever actually being blocked.
        warning = ""
        try:
            me = await context.bot.get_me()
            member = await context.bot.get_chat_member(chat_id=channel, user_id=me.id)
            if member.status not in ("administrator", "creator"):
                warning = (
                    "\n\n⚠️ " + to_small_caps("bot is in this channel but is NOT an admin there — "
                    "membership checks may fail. make the bot an admin in this channel.")
                )
        except Exception as e:
            warning = (
                f"\n\n⚠️ " + to_small_caps("could not verify this channel")
                + f" ({e}). " + to_small_caps(
                    "force-join will fail open (let everyone through) until this is fixed — "
                    "double-check the username/id and make sure the bot is an admin there."
                )
            )
        refreshed = await refresh_panel_after_save(context, "force_join", lambda: _build_adm_force_join_view())
        await update.message.reply_text(f"✅ Force-join channel set: {channel}{warning}" + ("" if refreshed else "\n(reopen the force-join panel to confirm)"))

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
        refreshed = await refresh_panel_after_save(context, "logger_channel", _build_adm_logger_channel_view)
        await update.message.reply_text(f"✅ Logger channel set to {chat_id} and enabled." + ("" if refreshed else "\n(reopen the logger panel to confirm)"))

    elif awaiting == "daily_limit":
        context.user_data.pop("awaiting", None)
        if not text.isdigit():
            await update.message.reply_text("Valid number bhejo.")
            return
        BOT_DATA["settings"]["daily_limit"] = int(text)
        save_data()
        refreshed = await refresh_panel_after_save(context, "premium", _build_adm_premium_view)
        await update.message.reply_text(f"✅ Daily limit set to {text}." + ("" if refreshed else "\n(reopen the premium panel to confirm)"))

    elif awaiting == "upi_id":
        context.user_data.pop("awaiting", None)
        BOT_DATA["settings"]["upi_id"] = text
        save_data()
        refreshed = await refresh_panel_after_save(context, "upi", _build_adm_upi_view)
        await update.message.reply_text(f"✅ UPI ID set: {text}" + ("" if refreshed else "\n(reopen the UPI panel to confirm)"))

    elif awaiting == "developer_id":
        context.user_data.pop("awaiting", None)
        if not text.isdigit():
            await update.message.reply_text("Valid numeric ID bhejo.")
            return
        BOT_DATA["settings"]["developer_id"] = int(text)
        save_data()
        note = ""
        try:
            chat = await context.bot.get_chat(int(text))
            if not chat.username:
                note = "\n⚠️ Ye account ka koi @username nahi hai — button kabhi-kabhi khulega nahi. 'Set Link Override' se @username bhejna better hai."
        except Exception:
            note = "\n⚠️ Bot is ID tak abhi pahuch nahi paaya (developer ne bot ko kabhi message nahi kiya) — button reliably khulega nahi jab tak 'Set Link Override' se @username na do."
        refreshed = await refresh_panel_after_save(context, "devsettings", _build_adm_devsettings_view)
        await update.message.reply_text(f"✅ Developer ID set: {text}{note}" + ("" if refreshed else "\n(reopen the developer panel to confirm)"))

    elif awaiting == "developer_link":
        context.user_data.pop("awaiting", None)
        BOT_DATA["settings"]["developer_link"] = None if text.lower() == "clear" else text
        save_data()
        refreshed = await refresh_panel_after_save(context, "devsettings", _build_adm_devsettings_view)
        await update.message.reply_text("✅ Developer link updated." + ("" if refreshed else "\n(reopen the developer panel to confirm)"))

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
        refreshed = await refresh_panel_after_save(context, "support_settings", _build_adm_support_settings_view)
        await update.message.reply_text(f"✅ Ticket group set to {chat_id}." + ("" if refreshed else "\n(reopen the support panel to confirm)"))

    elif awaiting == "plan_step_name":
        name = text.strip()
        if not name:
            await update.message.reply_text("Khaali naam nahi chalega. Plan ka naam bhejo.")
            return
        context.user_data.setdefault("new_plan", {})["name"] = name
        context.user_data["awaiting"] = "plan_step_days"
        await update.message.reply_text("Step 2/4 — Plan kitne din chalega? (e.g. 30)")

    elif awaiting == "plan_step_days":
        if not text.strip().isdigit() or int(text.strip()) <= 0:
            await update.message.reply_text("Valid number bhejo (e.g. 30).")
            return
        context.user_data["new_plan"]["days"] = int(text.strip())
        context.user_data["awaiting"] = "plan_step_inr"
        await update.message.reply_text(
            "Step 3/4 — ₹ (INR) price bhejo (UPI ke liye). "
            "Agar UPI se ye plan nahi bechna to '0' bhejo."
        )

    elif awaiting == "plan_step_inr":
        cleaned = text.strip().replace("₹", "")
        if not cleaned.isdigit():
            await update.message.reply_text("Valid number bhejo (0 bhi chalega agar UPI price nahi rakhna).")
            return
        context.user_data["new_plan"]["price_inr"] = int(cleaned)
        context.user_data["awaiting"] = "plan_step_stars"
        await update.message.reply_text(
            "Step 4/4 — ⭐ Telegram Stars price bhejo. "
            "Agar Stars se ye plan nahi bechna to '0' bhejo."
        )

    elif awaiting == "plan_step_stars":
        context.user_data.pop("awaiting", None)
        cleaned = text.strip()
        if not cleaned.isdigit():
            await update.message.reply_text("Valid number bhejo (0 bhi chalega).")
            context.user_data["awaiting"] = "plan_step_stars"
            return
        draft = context.user_data.pop("new_plan", {})
        draft["price_stars"] = int(cleaned)
        if not draft.get("price_inr") and not draft.get("price_stars"):
            await update.message.reply_text(
                "⚠️ Plan cancel — kam se kam ek price (₹ ya ⭐) zaroor set karo. "
                "➕ Add Plan se dobara try karo."
            )
        else:
            pid = str(BOT_DATA.get("next_plan_id", 1))
            BOT_DATA["next_plan_id"] = BOT_DATA.get("next_plan_id", 1) + 1
            plan = {
                "id": pid,
                "name": draft.get("name", "Plan"),
                "days": draft.get("days", 30),
                "price_inr": draft.get("price_inr", 0),
                "price_stars": draft.get("price_stars", 0),
                "enabled": True,
            }
            BOT_DATA["settings"].setdefault("premium_plans", []).append(plan)
            save_data()
            await log_event(context, f"💎 New plan added: {plan['name']} by {update.effective_user.id}")
            price_bits = []
            if plan["price_inr"]:
                price_bits.append(f"₹{plan['price_inr']}")
            if plan["price_stars"]:
                price_bits.append(f"{plan['price_stars']}⭐")
            refreshed = await refresh_panel_after_save(context, "premium", _build_adm_premium_view)
            await update.message.reply_text(
                f"✅ Plan added: {plan['name']} — {' / '.join(price_bits)} — {plan['days']}d — turned ON.\n"
                "It's now visible to users in the 🎁 gift menu."
                + ("" if refreshed else "\n(reopen the premium panel to confirm)")
            )

    elif awaiting == "adm_ban_unban_userid":
        context.user_data.pop("awaiting", None)
        if not text.isdigit():
            await update.message.reply_text("Valid numeric user ID bhejo.")
            return
        uid_int = int(text)
        if uid_int in BOT_DATA["blocked"]:
            BOT_DATA["blocked"].remove(uid_int)
            save_data()
            await update.message.reply_text(f"✅ User {uid_int} unbanned.")
            await log_event(context, f"✅ Admin unbanned user {uid_int} (via panel)")
        else:
            BOT_DATA["blocked"].append(uid_int)
            save_data()
            await update.message.reply_text(f"🚫 User {uid_int} banned.")
            await log_event(context, f"🚫 Admin banned user {uid_int} (via panel)")

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
        "adm_live": _render_adm_live,
        "adm_broadcast": _render_adm_broadcast,
        "adm_menu_ui": _render_adm_menu_ui,
        "adm_settings": _render_adm_settings,
        "adm_danger": _render_adm_danger,
        "adm_owner_contact": _render_adm_owner_contact,
        "adm_logger_channel": _render_adm_logger_channel,
        "adm_force_join": _render_adm_force_join,
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
    app.add_handler(CallbackQueryHandler(nav_tracked("adm_force_join")(cb_adm_force_join), pattern="^adm_force_join$"))
    app.add_handler(CallbackQueryHandler(cb_adm_force_join_set, pattern="^adm_force_join_set$"))
    app.add_handler(CallbackQueryHandler(cb_adm_force_join_clear, pattern="^adm_force_join_clear$"))
    app.add_handler(CallbackQueryHandler(cb_adm_force_join_test, pattern="^adm_force_join_test$"))

    app.add_handler(CallbackQueryHandler(cb_get_caption, pattern="^get_caption$"))
    app.add_handler(CallbackQueryHandler(cb_download_another, pattern="^download_another$"))
    app.add_handler(CallbackQueryHandler(cb_check_force_join, pattern="^check_force_join$"))
    app.add_handler(ChatMemberHandler(cm_track_groups, ChatMemberHandler.MY_CHAT_MEMBER))

    app.add_handler(CallbackQueryHandler(cb_ticket_close, pattern="^tk_close:"))
    app.add_handler(CallbackQueryHandler(cb_ticket_reopen, pattern="^tk_reopen:"))

    app.add_handler(CallbackQueryHandler(cb_gift_menu, pattern="^gift_menu$"))
    app.add_handler(CallbackQueryHandler(cb_gift_plan, pattern="^gift_plan:"))
    app.add_handler(CallbackQueryHandler(cb_gift_plan_stars, pattern="^gift_plan_stars:"))
    app.add_handler(CallbackQueryHandler(cb_gift_plan_upi, pattern="^gift_plan_upi:"))
    app.add_handler(CallbackQueryHandler(cb_gift_stars, pattern="^gift_stars$"))
    app.add_handler(CallbackQueryHandler(cb_gift_stars_custom, pattern="^gift_stars_custom$"))
    app.add_handler(CallbackQueryHandler(cb_gift_stars_amount, pattern="^gift_stars_amt:"))
    app.add_handler(CallbackQueryHandler(cb_gift_upi, pattern="^gift_upi$"))
    app.add_handler(CallbackQueryHandler(cb_gift_upi_paid, pattern="^gift_upi_paid:"))
    app.add_handler(CallbackQueryHandler(cb_gift_upi_confirm, pattern="^gift_upi_confirm:"))
    app.add_handler(PreCheckoutQueryHandler(cmd_precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, cmd_successful_payment))
    app.add_handler(CallbackQueryHandler(cb_nav, pattern="^nav:"))
    app.add_handler(CallbackQueryHandler(cb_toggle_menu_button, pattern="^tgl:"))
    app.add_handler(CallbackQueryHandler(cb_settings_toggle, pattern="^stgl:"))
    app.add_handler(CallbackQueryHandler(cb_styleset, pattern="^styleset:"))
    app.add_handler(CallbackQueryHandler(cb_noop, pattern="^noop$"))

    app.add_handler(CallbackQueryHandler(cb_adm_home, pattern="^adm_home$"))
    app.add_handler(CallbackQueryHandler(cb_adm_activity, pattern="^adm_activity$"))
    app.add_handler(CallbackQueryHandler(cb_adm_selftest, pattern="^adm_selftest$"))
    app.add_handler(CallbackQueryHandler(cb_adm_back, pattern="^adm_back$"))
    app.add_handler(CallbackQueryHandler(nav_tracked("adm_stats")(cb_adm_stats), pattern="^adm_stats$"))
    app.add_handler(CallbackQueryHandler(nav_tracked("adm_users")(cb_adm_users), pattern="^adm_users$"))
    app.add_handler(CallbackQueryHandler(cb_adm_users_list, pattern="^adm_users_list$"))
    app.add_handler(CallbackQueryHandler(cb_adm_groups_list, pattern="^adm_groups_list$"))
    app.add_handler(CallbackQueryHandler(cb_adm_users_msg, pattern="^adm_users_msg$"))
    app.add_handler(CallbackQueryHandler(nav_tracked("adm_live")(cb_adm_live), pattern="^adm_live$"))
    app.add_handler(CallbackQueryHandler(cb_adm_quickban, pattern="^adm_quickban$"))
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
    app.add_handler(CallbackQueryHandler(cb_adm_plan_add, pattern="^adm_plan_add$"))
    app.add_handler(CallbackQueryHandler(cb_adm_plan_toggle, pattern="^adm_plan_toggle:"))
    app.add_handler(CallbackQueryHandler(cb_adm_plan_del, pattern="^adm_plan_del:"))
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
    # BUGFIX #2 — was filters.TEXT only, so photo/video messages (ticket
    # replies from users, or admins replying with media) never reached
    # handle_text and therefore never got forwarded into the ticket thread.
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.ALL
             | filters.VOICE | filters.AUDIO | filters.ANIMATION | filters.Sticker.ALL)
            & ~filters.COMMAND,
            handle_text,
        )
    )

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
#
# ------------------------------------------------------------------------
# Code review pass — fixes applied:
#
# 1. Admin Panel "Premium ON/OFF" button did nothing visible — the generic
#    toggle handler only knew how to refresh 3 hardcoded screens and
#    "adm_premium" wasn't one of them. Toggle now looks up ANY registered
#    screen, so this and every future toggle button actually re-renders.
# 2. Force-join was silently non-functional in common setups:
#      - ANY error checking membership (bad channel format, bot not admin
#        there, etc.) was swallowed with zero logging, so it looked
#        "configured" while never blocking a single user. Now logged to
#        Activity Log so it's actually diagnosable.
#      - Bare usernames typed without "@" are now auto-normalized on save.
#      - Setting the channel now immediately verifies the bot can see it
#        and is an admin there, warning the admin right away if not.
#      - The "📢 Join Channel" button used to build a dead
#        https://t.me/-100xxxxxxxxxx link for numeric channel IDs (the
#        suggested format for private channels) — now resolves a real,
#        clickable invite link via the Bot API.
# 3. Payment QR codes were a plain black-and-white square from a
#    third-party URL (api.qrserver.com). Now generated locally: rounded
#    modules, on-brand color, amount + "scan with any UPI app" caption
#    baked into the card. Falls back to the old remote URL automatically
#    if `qrcode`/Pillow aren't installed — see setup notes at the top.
# 4. Admin Panel buttons are now colorless/neutral by design (colors were
#    intentionally removed from every admin screen per request).
# 5. Every button in the bot — admin panel and user-facing — is now
#    small-caps by default, enforced centrally in styled_button() /
#    styled_kb_button() so it can't drift out of sync screen-by-screen.
# 6. Auto-Replies list: an operator-precedence bug (`list_a + list_b or
#    fallback`) meant the "no auto-replies yet" message could never
#    actually display, since the concatenated list was always truthy.
# 7. A download-failure message interpolated the raw exception text into
#    an HTML-parsed message unescaped; any "<", ">", or "&" in a yt-dlp
#    error (common in URLs) broke Telegram's parser and could leave the
#    user staring at a frozen "processing..." message. Now escaped, with
#    a plain-text fallback if the edit still somehow fails.
# ------------------------------------------------------------------------
