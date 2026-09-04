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
  pip install matplotlib reportlab   # optional — enables 📊 /exportpdf
  pip install cryptography           # optional — enables encrypted backups
  export BOT_TOKEN="123:ABC"
  export OWNER_ID="123456789"
  # optional:
  export MONGO_URI="mongodb+srv://..."
  export BACKUP_INTERVAL_HOURS="12"
  python3 bot.py
"""

import os
import io
import html
import re
import json
import csv
import time
import shutil
import subprocess
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
    CopyTextButton,
    MessageEntity,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ApplicationHandlerStop,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    ChatJoinRequestHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)
from telegram.error import Forbidden, BadRequest, RetryAfter, TelegramError

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
PLUGIN_DIR = "plugins"
MAX_LOCAL_BACKUPS = 10
BACKUP_KEY_FILE = "backup.key"  # local Fernet key — never put this in the repo/git

os.makedirs(BACKUP_DIR, exist_ok=True)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(PLUGIN_DIR, exist_ok=True)

# ffmpeg is only needed when yt-dlp has to MERGE separate video+audio streams.
# Most Instagram reels are already a single muxed file, so we don't strictly
# need it — but when a merge is required and ffmpeg is missing, downloads
# used to crash. We now auto-detect ffmpeg (system install, or the portable
# binary from the `imageio-ffmpeg` package) and gracefully fall back to a
# no-merge format if neither is available.
FFMPEG_PATH = shutil.which("ffmpeg")
FFPROBE_PATH = shutil.which("ffprobe")
if not FFMPEG_PATH:
    try:
        import imageio_ffmpeg

        FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        FFMPEG_PATH = None
FFMPEG_AVAILABLE = bool(FFMPEG_PATH)
# FIX — "audio extraction failed... unable to obtain file audio codec with
# ffprobe": the imageio-ffmpeg package (our fallback when no system ffmpeg
# is installed) ships ONLY an ffmpeg binary — no ffprobe. yt-dlp's
# FFmpegExtractAudio postprocessor was pointed at that binary's folder via
# ffmpeg_location and silently assumed ffprobe lived right next to it, so
# every audio extraction on a server without a real system install failed
# with exactly that ffprobe error. We now track ffprobe's availability
# separately and do audio extraction ourselves via a direct ffmpeg call
# (see cb_get_audio) instead of relying on yt-dlp's postprocessor, so a
# missing ffprobe no longer breaks the Audio button.
FFPROBE_AVAILABLE = bool(FFPROBE_PATH)


def _ytdlp_extract_with_retry(opts: dict, url: str, download: bool = True):
    """Run yt-dlp's extract_info with one automatic retry.

    FIX — "No video formats found!" / "Unable to extract ..." errors from
    the Instagram extractor are very often caused by yt-dlp's own on-disk
    extractor cache holding a stale response from before Instagram changed
    something on their end — the exact same symptom yt-dlp's own docs tell
    users to fix with `yt-dlp --rm-cache-dir`. Previously a transient hit
    like this permanently failed the download with no retry. Now: on the
    known-transient error signatures, the cache is cleared once and the
    extraction is retried before giving up for real. This does NOT paper
    over a genuinely private/deleted/invalid link — those still fail
    immediately, since retrying can't help those.
    """
    transient_markers = (
        "no video formats found",
        "requested format is not available",
        "unable to extract",
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            return ydl.extract_info(url, download=download)
        except Exception as e:
            msg = str(e).lower()
            if not any(m in msg for m in transient_markers):
                raise
            log.warning("yt-dlp extraction hit a possibly-stale-cache error, clearing cache and retrying once: %s", e)
            try:
                ydl.cache.remove()
            except Exception:
                pass
    # Retry with a fresh YoutubeDL instance so the cleared cache actually takes effect.
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=download)

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

# ----------------------------------------------------------------------------
# PDF report (charts) + encrypted backup — same graceful-fallback pattern as
# qrcode/Pillow above. Neither is a hard requirement to run the bot; the
# admin panel just tells you what to `pip install` if a feature is missing.
# ----------------------------------------------------------------------------
PDF_REPORT_AVAILABLE = False
try:
    import matplotlib
    matplotlib.use("Agg")  # headless — no display server on a bot host
    import matplotlib.pyplot as plt
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib import colors as rl_colors
    from reportlab.platypus import (
        SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image as RLImage,
    )
    from reportlab.lib.styles import getSampleStyleSheet

    PDF_REPORT_AVAILABLE = True
except ImportError:
    log.warning(
        "matplotlib/reportlab not found — 📊 PDF Report will be unavailable. "
        "Run `pip install matplotlib reportlab` to enable it."
    )

BACKUP_ENCRYPTION_AVAILABLE = False
try:
    from cryptography.fernet import Fernet, InvalidToken

    BACKUP_ENCRYPTION_AVAILABLE = True
except ImportError:
    log.warning(
        "`cryptography` not found — backups will be saved unencrypted. "
        "Run `pip install cryptography` to enable encrypted backups."
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


def _default_center_logo(size: int) -> "Image.Image":
    """Vector-drawn circular fallback logo (gradient ring + simple bolt
    icon), used whenever no user avatar is available — e.g. the user has
    no Telegram profile photo, get_user_profile_photos fails, or
    logo_bytes is None. This is drawn with plain PIL shapes, not text, so
    it never depends on a font being present and the QR center is never
    left blank/plain."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    grad = _diagonal_gradient((size, size), QR_GRADIENT_A, QR_GRADIENT_B).convert("RGBA")
    ring_mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(ring_mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    img.paste(grad, (0, 0), ring_mask)

    inner_pad = max(2, int(size * 0.09))
    d = ImageDraw.Draw(img)
    d.ellipse(
        (inner_pad, inner_pad, size - 1 - inner_pad, size - 1 - inner_pad),
        fill=QR_CARD_BG + (255,),
    )

    # simple bolt icon, pure vector — no font/glyph dependency at all
    cx, cy = size / 2, size / 2
    s = size * 0.20
    points = [
        (cx + s * 0.15, cy - s), (cx - s * 0.55, cy + s * 0.15), (cx - s * 0.05, cy + s * 0.15),
        (cx - s * 0.15, cy + s), (cx + s * 0.55, cy - s * 0.15), (cx + s * 0.05, cy - s * 0.15),
    ]
    d.polygon(points, fill=QR_TEXT_LIGHT + (255,))
    return img


def _paste_center_logo(qr_img: "Image.Image", logo_bytes: bytes = None):
    """Paste a circular avatar (or the vector fallback logo, if no avatar
    bytes were given/loadable) in the middle of the QR with a white buffer
    ring underneath it, sized so the code is still reliably scannable."""
    logo_size = int(qr_img.width * QR_LOGO_RATIO)
    if logo_bytes:
        try:
            logo = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")
            logo = ImageOps.fit(logo, (logo_size, logo_size), Image.LANCZOS)
        except Exception:
            log.exception("Could not decode avatar bytes for QR center logo — using fallback logo")
            logo = _default_center_logo(logo_size)
    else:
        logo = _default_center_logo(logo_size)

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


_QR_FONT_CACHE = {}


def _find_unicode_font(bold: bool):
    """Search common install locations for a DejaVu Sans TTF that can
    render the glyphs the QR card needs (₹, and the small-caps Unicode
    phonetic-extension letters used throughout the bot's UI).

    ImageFont.truetype("DejaVuSans-Bold.ttf") — a bare filename — only
    resolves when that file happens to sit in the current working
    directory or a couple of PIL-internal dirs. It does NOT search the
    system's actual font directories (Pillow has no fontconfig
    integration), so on most servers/containers this silently raises and
    the caller falls back to PIL's tiny built-in bitmap font, which can't
    render ₹ or small-caps at all — hence the ▯▯▯▯ boxes. Returns a real
    path to a working font, or None if nothing usable was found."""
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    candidates = [
        name,
        f"/usr/share/fonts/truetype/dejavu/{name}",
        f"/usr/share/fonts/dejavu/{name}",
        f"/usr/share/fonts/truetype/ttf-dejavu/{name}",
        f"/usr/share/fonts/TTF/{name}",
        f"/usr/local/share/fonts/{name}",
        f"/Library/Fonts/{name}",
        os.path.expanduser(f"~/.fonts/{name}"),
        f"C:\\Windows\\Fonts\\{name}",
    ]
    # matplotlib bundles DejaVu Sans and is present in a lot of environments
    # even when the OS-level font packages aren't installed — cheap extra
    # chance at a real unicode-capable font before giving up.
    try:
        import matplotlib
        candidates.insert(1, os.path.join(matplotlib.get_data_path(), "fonts", "ttf", name))
    except Exception:
        pass

    for path in candidates:
        try:
            ImageFont.truetype(path, 10)
            return path
        except Exception:
            continue
    return None


def _load_qr_fonts():
    """Load (and cache) the fonts used on the QR card. Returns
    (font_big, font_small, unicode_ok). unicode_ok is False only when no
    real TTF could be found anywhere and we had to fall back to PIL's
    default bitmap font — callers must then ASCII-ify any ₹/small-caps
    text before drawing it, or it renders as ▯▯▯▯ boxes."""
    if _QR_FONT_CACHE:
        c = _QR_FONT_CACHE
        return c["big"], c["small"], c["unicode_ok"]

    bold_path = _find_unicode_font(bold=True)
    reg_path = _find_unicode_font(bold=False)
    try:
        if not (bold_path and reg_path):
            raise OSError("no unicode-capable TTF found on this system")
        font_big = ImageFont.truetype(bold_path, 30)
        font_small = ImageFont.truetype(reg_path, 16)
        unicode_ok = True
    except Exception:
        font_big = ImageFont.load_default()
        font_small = ImageFont.load_default()
        unicode_ok = False
        log.warning(
            "No DejaVu/unicode-capable TTF found on this system — QR card "
            "text (₹ amount, small-caps caption) will render as plain "
            "ASCII via PIL's default bitmap font instead of showing "
            "▯▯▯▯ boxes. Install the 'fonts-dejavu-core' package (or any "
            "TTF with those glyphs) for the real symbols."
        )

    _QR_FONT_CACHE.update(big=font_big, small=font_small, unicode_ok=unicode_ok)
    return font_big, font_small, unicode_ok


def _ascii_safe(text: str) -> str:
    """Best-effort plain-ASCII rendering of QR card text, used only when
    _load_qr_fonts() couldn't find a real unicode-capable font. Undoes the
    bot's small-caps styling (ᴀʙᴄ.. -> abc..) and swaps ₹ for 'Rs.' so the
    card shows readable text instead of ▯▯▯▯ tofu boxes."""
    reverse = {v: k for k, v in SMALL_CAPS_MAP.items()}
    out = "".join(reverse.get(ch, ch) for ch in text)
    return out.replace("₹", "Rs.")


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

        # Always paste a center logo — the real avatar when we have it,
        # otherwise the vector fallback baked into _paste_center_logo, so
        # the card never renders with a plain blank center.
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

        font_big, font_small, unicode_ok = _load_qr_fonts()
        safe_caption = caption if unicode_ok else _ascii_safe(caption)

        y = pad // 2 + 6
        if amount is not None:
            amt_text = f"₹{amount}" if unicode_ok else f"Rs.{amount}"
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

        w = draw.textlength(safe_caption, font=font_small)
        draw.text(((canvas_w - w) / 2, y), safe_caption, fill=QR_TEXT_MUTED, font=font_small)

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


def _is_private_chat(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type == "private")


def _message_mentions_this_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True when a message explicitly tags this bot. Useful in groups where
    privacy mode is disabled and we must never answer unrelated chatter."""
    try:
        username = (context.bot.username or "").lower()
    except Exception:
        username = ""
    text = (update.effective_message.text or "").lower() if update.effective_message else ""
    return bool(username and f"@{username}" in text)


def _safe_filename(value: str, fallback: str = "Instagram_Audio") -> str:
    value = re.sub(r'[\\/:*?"<>|]+', " ", str(value or ""))
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value[:80] or fallback)

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
    """Bot-wide house style: Initial Capital + Unicode small caps.

    Every normal user/admin UI string that passes through this helper now
    follows the same typography, e.g. ``Stats & Activity`` ->
    ``Sᴛᴀᴛꜱ & Aᴄᴛɪᴠɪᴛʏ``. This keeps old generated messages from drifting
    between plain lowercase and all-small-caps styles.
    """
    out = []
    word_start = True
    for ch in str(text):
        if ch.isalpha() and ch.isascii():
            if word_start:
                out.append(ch.upper())
                word_start = False
            else:
                out.append(SMALL_CAPS_MAP.get(ch.lower(), ch))
        else:
            out.append(ch)
            # Start a new styled word after whitespace/punctuation, but not
            # after an already-styled Unicode small-cap glyph.
            word_start = not (ch.isalnum() or ch in "'’")
    return "".join(out)


def to_title_small_caps(text: str) -> str:
    """Alias for the single bot-wide Initial-Capital small-caps house style."""
    return to_small_caps(text)


# ----------------------------------------------------------------------------
# v2 build prompt — centralized small-caps strings (Section 10)
# ----------------------------------------------------------------------------
STR = {
    "processing": to_small_caps("processing your reel...") + "\n📥 " + to_small_caps("fetching") + " • 🔄 "
                  + to_small_caps("optimizing") + " • ✅ " + to_small_caps("almost done"),
    "done": "✅ " + to_small_caps("your reel is ready!") + "\n🎬 " + to_small_caps("saved and sent below"),
    "usage_title": to_small_caps("usage overview"),
    "how_to_use": (
        "<blockquote>"
        "𝐇𝐎𝐖 𝐓𝐎 𝐔𝐒𝐄\n\n"
        "➤ Cᴏᴘʏ ᴀɴʏ Iɴsᴛᴀɢʀᴀᴍ Rᴇᴇʟ ʟɪɴᴋ\n\n"
        "➤ Pᴀsᴛᴇ ᴛʜᴇ ʟɪɴᴋ ʜᴇʀᴇ ɪɴ ᴄʜᴀᴛ\n\n"
        "➤ Wᴀɪᴛ ᴀ ғᴇᴡ sᴇᴄᴏɴᴅs\n\n"
        "➤ Gᴇᴛ ʏᴏᴜʀ Rᴇᴇʟ ᴅᴏᴡɴʟᴏᴀᴅᴇᴅ ɪɴsᴛᴀɴᴛʟʏ\n\n"
        "➤ 𝐍𝐎𝐓𝐄 : Oɴʟʏ Pᴜʙʟɪᴄ Iɴsᴛᴀɢʀᴀᴍ Rᴇᴇʟ ʟɪɴᴋs ᴀʀᴇ sᴜᴘᴘᴏʀᴛᴇᴅ"
        "</blockquote>"
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
RKB_DOWNLOAD = to_title_small_caps("Download Reel")
RKB_USAGE = to_title_small_caps("My Usage")
RKB_GIFT = to_title_small_caps("Send A Gift")
RKB_LANGUAGE = to_title_small_caps("Language")
RKB_DEVELOPER = to_title_small_caps("Developer")
RKB_HOWTO = to_title_small_caps("How To Use")
RKB_SUPPORT = to_title_small_caps("Support")
RKB_ADMINPANEL = to_title_small_caps("Admin Panel")


def main_reply_keyboard(is_admin_user: bool = False) -> ReplyKeyboardMarkup:
    """v2 Section 1 — persistent bottom keyboard, alongside the existing
    inline /start menu (doesn't replace it). Colors via Bot API 9.4 `style`
    (needs PTB 22.7+, already pinned in requirements.txt).
    v5 — an extra 🛠 Admin Panel row appears below How to Use/Support, but
    ONLY when this keyboard is built for an admin's own chat — every other
    user's keyboard is completely unchanged."""
    rows = [
        [styled_kb_button(RKB_DOWNLOAD, style="success")],
        [styled_kb_button(RKB_USAGE, style="primary"), styled_kb_button(RKB_GIFT, style="primary")],
        [styled_kb_button(RKB_LANGUAGE, style="primary"), styled_kb_button(RKB_DEVELOPER, style="primary")],
        [styled_kb_button(RKB_HOWTO, style="danger"), styled_kb_button(RKB_SUPPORT, style="danger")],
    ]
    if is_admin_user:
        rows.append([styled_kb_button(RKB_ADMINPANEL, style="primary")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)



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


# -----------------------------------------------------------------------------
# User-facing error messages
# -----------------------------------------------------------------------------
# Keep technical exceptions (yt-dlp / ffmpeg / Telegram / network details)
# in the server/admin logs only. Users should always receive short, clean
# messages in the same typography as the rest of the bot UI.
USER_ERR_WRONG_FORMAT = (
    "❌ I" + to_small_caps("nvalid ") + "L" + to_small_caps("ink") + "\n\n"
    + "T" + to_small_caps("he link you sent is not a valid ")
    + "I" + to_small_caps("nstagram ") + "R" + to_small_caps("eel link.") + "\n\n"
    + "P" + to_small_caps("lease send a valid reel link to continue.") + "\n"
    + "F" + to_small_caps("or help, contact ") + "S" + to_small_caps("upport.")
)

USER_ERR_NOT_AVAILABLE = (
    "<blockquote>❌ " + to_title_small_caps("Not Available") + "\n\n"
    + to_title_small_caps("This Reel Can't Be Downloaded Right Now.") + "\n"
    + to_title_small_caps("Please Try Again Later Or Contact Support.")
    + "</blockquote>"
)


USER_ERR_AUDIO_NOT_AVAILABLE = (
    "<blockquote>❌ " + to_title_small_caps("Audio Not Available") + "\n\n"
    + to_title_small_caps("This Reel Doesn't Have An Audio Track To Extract.") + "\n"
    + to_title_small_caps("Please Try Again Later Or Contact Support.")
    + "</blockquote>"
)


USER_ERR_GENERIC = (
    "❌ " + to_bold_sans("Something went wrong") + "\n\n"
    + to_small_caps("Please try again later or contact support.")
)


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
    return f"『 {text} 』"


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


def premium_button_text(text: str) -> str:
    """First alphabetic character of EVERY word uppercase, rest of that
    word in small caps — same house style as to_title_small_caps(), used
    as the single place every button (inline + reply-keyboard) gets its
    typography from. Idempotent, so pre-styled constants stay stable even
    after passing through this a second time at render."""
    return to_title_small_caps(str(text))


def styled_button(text, callback_data=None, url=None, style=None):
    """Central inline-button renderer with premium typography."""
    kwargs = {}
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if url is not None:
        kwargs["url"] = url
    if style and SUPPORTS_BUTTON_STYLE:
        kwargs["style"] = style
    return InlineKeyboardButton(premium_button_text(text), **kwargs)


# ---- Activity Log: categorization + plain-English fix hints -----------------
# Every entry logged to BOT_DATA["error_log"] carries a "kind" so the panel
# can show *what* went wrong, *why* it likely happened, and *how* to fix it —
# instead of a raw, undated exception string nobody but a developer could
# read.
ERROR_KIND_INFO = {
    "force_join": (
        "🔒 Force-Join Check",
        "Couldn't verify a user's membership in the force-join channel.",
        "Make sure the bot is an admin in that channel, and that the "
        "channel is set correctly (use @username, or the -100... id for "
        "private channels) in Settings & Admins → Force-Join.",
    ),
    "broadcast": (
        "📢 Broadcast Delivery",
        "A message failed to send during a broadcast.",
        "Usually harmless — the recipient blocked the bot or never started "
        "a DM. Check the Broadcast Log for the full breakdown.",
    ),
    "mongo": (
        "🗄 Database",
        "Couldn't reach or sync with MongoDB.",
        "Check MONGO_URI is correct and the database allows connections "
        "from this server's IP. The bot keeps working on local storage "
        "meanwhile, so nothing is lost.",
    ),
    "download": (
        "⬇️ Download",
        "A reel/audio download failed.",
        "Usually the link was private, deleted, or Instagram briefly "
        "rate-limited the server. Ask the user to retry in a minute.",
    ),
    "conflict": (
        "⚔️ Duplicate Bot Instance",
        "Telegram rejected polling because another process is already "
        "polling with this same BOT_TOKEN.",
        "Stop the other running copy of this bot (an old deployment, a "
        "second terminal, a duplicate server) — only one instance can poll "
        "at a time. This can't be fixed from inside this process, since "
        "the conflicting instance is the other one.",
    ),
    "unhandled": (
        "🐞 Unexpected Error",
        "Something failed outside the usual error handling.",
        "Check the message below for the exact exception — if it keeps "
        "repeating for the same action, that action likely has a bug.",
    ),
}


def log_error(kind: str, detail: str) -> None:
    """Central place every part of the bot reports a problem to. Keeps the
    Activity Log screen consistent (same field names, always categorized)
    instead of every call site hand-rolling its own dict shape."""
    entries = BOT_DATA.setdefault("error_log", [])
    next_id = BOT_DATA.get("error_log_next_id", 1)
    entries.append({
        "id": next_id,
        "time": datetime.utcnow().isoformat(),
        "kind": kind if kind in ERROR_KIND_INFO else "unhandled",
        "detail": str(detail)[:400],
    })
    BOT_DATA["error_log_next_id"] = next_id + 1
    if len(entries) > 200:
        del entries[: len(entries) - 200]


def _short_btn_label(label: str, limit: int = 22) -> str:
    """Trims a long button label in admin list screens (e.g. Manage
    Buttons) so the row stays readable on a phone instead of the button
    text overflowing/getting cut off by Telegram."""
    label = str(label)
    return label if len(label) <= limit else label[: limit - 1].rstrip() + "…"


def toggle_label(base: str, is_on: bool) -> str:
    """Consistent ON/OFF rendering for every toggle button in the admin
    panel — a green tick when ON, a red cross when OFF, instead of the bare
    'ON'/'OFF' text every toggle used to hand-roll separately."""
    return f"{base}: {'✅ ON' if is_on else '❌ OFF'}"


def styled_kb_button(text, style=None):
    """Reply-keyboard button renderer with premium typography."""
    text = premium_button_text(text)
    if style and SUPPORTS_KB_BUTTON_STYLE:
        return KeyboardButton(text, style=style)
    return KeyboardButton(text)


# ----------------------------------------------------------------------------
# Default data / menu records (#1, #2, #4, #7)
# ----------------------------------------------------------------------------

DEFAULT_MENUS = {
    "start": {
        "text": (
            "<blockquote>"
            "Hᴇʟʟᴏ, {username}!\n\n"
            "𝐖𝐄𝐋𝐂𝐎𝐌𝐄 ᴛᴏ {bot_link}\n\n"
            "Yᴏᴜʀ ᴘᴇʀsᴏɴᴀʟ Iɴsᴛᴀɢʀᴀᴍ Rᴇᴇʟ ᴀssɪsᴛᴀɴᴛ.\n\n"
            "𝐓𝐇𝐈𝐒 𝐁𝐎𝐓 𝐂𝐀𝐍\n\n"
            "➤ Dᴏᴡɴʟᴏᴀᴅ Iɴsᴛᴀɢʀᴀᴍ Rᴇᴇʟs\n\n"
            "➤ Gᴇᴛ Rᴇᴇʟ Cᴀᴘᴛɪᴏɴs\n\n"
            "➤ Gᴇᴛ Rᴇᴇʟ Aᴜᴅɪᴏ\n\n"
            "𝐖𝐇𝐘 𝐂𝐇𝐎𝐎𝐒𝐄 𝐔𝐒?\n\n"
            "➤ Nᴏ Wᴀᴛᴇʀᴍᴀᴋs\n\n"
            "➤ Hɪɢʜ-Qᴜᴀʟɪᴛʏ Rᴇᴇʟ Dᴏᴡɴʟᴏᴀᴅs\n\n"
            "➤ Fᴀsᴛ ᴀɴᴅ Sᴍᴏᴏᴛʜ Sᴇʀᴠɪᴄᴇ\n\n"
            "➤ Hᴀssʟᴇ-Fʀᴇᴇ ᴀɴᴅ Eᴀsʏ ᴛᴏ Uꜱᴇ\n\n"
            "➤ Sᴀᴠᴇs Tɪᴍᴇ ᴀɴᴅ Eғғᴏʀᴛ\n\n"
            "➤ Rᴇᴇʟ, Cᴀᴘᴛɪᴏɴ ᴀɴᴅ Aᴜᴅɪᴏ ɪɴ Oɴᴇ Pʟᴀᴄᴇ\n\n"
            "➤ Nᴏ Exᴛʀᴀ Tᴏᴏʟs ᴏʀ Cᴏᴍᴘʟɪᴄᴀᴛᴇᴅ Sᴛᴇᴘs"
            "</blockquote>"
        ),
        "parse_mode": "HTML",
        "image_file_id": None,
        "buttons": [],
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
        "buttons": [],
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
            {"label": to_small_caps("🎵 audio"), "type": "callback", "value": "get_audio", "row": 1, "style": "primary"},
        ],
        "auto_delete_seconds": None,
        "updated_by": None,
        "updated_at": None,
        "translations": {},
    },
    "disclaimer": {
        "text": (
            "<blockquote expandable>"
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
            "This service is provided \"as is\", without warranty of any kind, "
            "and may be modified, suspended, or discontinued at any time "
            "without prior notice. Continued use of this bot after any "
            "changes to these terms constitutes acceptance of the updated "
            "terms.\n\n"
            "Tap <b>I Agree &amp; Continue</b> to confirm you have read and "
            "accepted these terms."
            "</blockquote>"
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
            "ᴛʜᴇ ʙᴏᴛ ɪꜱ ᴛᴇᴍᴘᴏʀᴀʀɪʟʏ ᴏꜰꜰʟɪɴᴇ ꜰᴏʀ\n"
            "ꜱᴄʜᴇᴅᴜʟᴇᴅ ᴜᴘɢʀᴀᴅᴇꜱ ᴀɴᴅ ɪᴍᴘʀᴏᴠᴇᴍᴇɴᴛꜱ.\n\n"
            "ᴡᴇ'ʀᴇ ᴡᴏʀᴋɪɴɢ ᴛᴏ ᴍᴀᴋᴇ ᴛʜᴇ ʙᴏᴛ\n"
            "ꜰᴀꜱᴛᴇʀ, ꜱᴍᴏᴏᴛʜᴇʀ ᴀɴᴅ ʙᴇᴛᴛᴇʀ\n"
            "ꜰᴏʀ ᴇᴠᴇʀʏᴏɴᴇ.\n\n"
            "⏳ ᴡᴇ'ʟʟ ʙᴇ ʙᴀᴄᴋ ꜱʜᴏʀᴛʟʏ.\n\n"
            "ᴛʜᴀɴᴋ ʏᴏᴜ ꜰᴏʀ ʏᴏᴜʀ\n"
            "ᴘᴀᴛɪᴇɴᴄᴇ & ꜱᴜᴘᴘᴏʀᴛ."
        ),
        "parse_mode": None,
        "image_file_id": None,
        "buttons": [
            {"label": "🔔 " + to_small_caps("notify me"), "type": "callback", "value": "maint_notify_me", "row": 1, "style": "danger"}
        ],
        "auto_delete_seconds": None,
        "updated_by": None,
        "updated_at": None,
        "translations": {},
    },
    "bot_live": {
        "text": (
            "✅ 𝐁𝐎𝐓 𝐈𝐒 𝐋𝐈𝐕𝐄\n\n"
            "ᴍᴀɪɴᴛᴇɴᴀɴᴄᴇ ɪꜱ ᴄᴏᴍᴘʟᴇᴛᴇ — ᴛʜᴇ ʙᴏᴛ ɪꜱ ʙᴀᴄᴋ ᴜᴘ ᴀɴᴅ ʀᴜɴɴɪɴɢ ɴᴏʀᴍᴀʟʟʏ.\n\n"
            "🚀 ᴇᴠᴇʀʏᴛʜɪɴɢ ɪꜱ ʙᴀᴄᴋ ᴏɴʟɪɴᴇ ᴀɴᴅ ʀᴇᴀᴅʏ ᴛᴏ ᴜꜱᴇ.\n\n"
            "ᴛʜᴀɴᴋ ʏᴏᴜ ꜰᴏʀ ᴡᴀɪᴛɪɴɢ.\n\n"
            "ᴇɴᴊᴏʏ ᴛʜᴇ ɪᴍᴘʀᴏᴠᴇᴍᴇɴᴛꜱ. ✨"
        ),
        "parse_mode": None,
        "image_file_id": None,
        "buttons": [],
        "auto_delete_seconds": None,
        "updated_by": None,
        "updated_at": None,
        "translations": {},
    },
    "help_admin": {
        "text": (
            to_small_caps("❓ admin help") + "\n\n"
            + to_small_caps("📊 stats & activity — view the bot's live numbers") + "\n"
            + to_small_caps("👥 users & groups — list or message any user") + "\n"
            + to_small_caps("📢 broadcast — message everyone, with forward-lock") + "\n"
            + to_small_caps("🎨 menu & ui — edit any menu's text, image or buttons") + "\n"
            + to_small_caps("⚙️ settings & admins — welcome, admins, maintenance, languages") + "\n"
            + to_small_caps("🛑 danger zone — destructive, irreversible actions")
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
    "download": {
        "text": (
            "<blockquote>"
            "𝘞𝘢𝘯𝘵 𝘵𝘰 𝘥𝘰𝘸𝘯𝘭𝘰𝘢𝘥 𝘢 𝘙𝘦𝘦𝘭?\n\n"
            "➤ 𝘊𝘰𝘱𝘺 𝘵𝘩𝘦 𝘙𝘦𝘦𝘭 𝘭𝘪𝘯𝘬 𝘧𝘳𝘰𝘮 𝘐𝘯𝘴𝘵𝘢𝘨𝘳𝘢𝘮\n\n"
            "➤ 𝘗𝘢𝘴𝘵𝘦 𝘵𝘩𝘦 𝘭𝘪𝘯𝘬 𝘩𝘦𝘳𝘦\n\n"
            "𝘛𝘩𝘢𝘵'𝘴 𝘪𝘵 — 𝘐'𝘭𝘭 𝘥𝘰 𝘵𝘩𝘦 𝘳𝘦𝘴𝘵."
            "</blockquote>"
        ),
        "parse_mode": "HTML",
        "image_file_id": None,
        "buttons": [],
        "auto_delete_seconds": None,
        "updated_by": None,
        "updated_at": None,
        "translations": {},
    },
    "howto": {
        "text": (
            "<blockquote>"
            "𝐇𝐎𝐖 𝐓𝐎 𝐔𝐒𝐄\n\n"
            "➤ Cᴏᴘʏ ᴀɴʏ Iɴsᴛᴀɢʀᴀᴍ Rᴇᴇʟ ʟɪɴᴋ\n\n"
            "➤ Pᴀsᴛᴇ ᴛʜᴇ ʟɪɴᴋ ʜᴇʀᴇ ɪɴ ᴄʜᴀᴛ\n\n"
            "➤ Wᴀɪᴛ ᴀ ғᴇᴡ sᴇᴄᴏɴᴅs\n\n"
            "➤ Gᴇᴛ ʏᴏᴜʀ Rᴇᴇʟ ᴅᴏᴡɴʟᴏᴀᴅᴇᴅ ɪɴsᴛᴀɴᴛʟʏ\n\n"
            "➤ 𝐍𝐎𝐓𝐄 : Oɴʟʏ Pᴜʙʟɪᴄ Iɴsᴛᴀɢʀᴀᴍ Rᴇᴇʟ ʟɪɴᴋs ᴀʀᴇ sᴜᴘᴘᴏʀᴛᴇᴅ"
            "</blockquote>"
        ),
        "parse_mode": "HTML",
        "image_file_id": None,
        "buttons": [],
        "auto_delete_seconds": None,
        "updated_by": None,
        "updated_at": None,
        "translations": {},
    },
    # v10 — these 5 are new: the intro banner (and optional image) for each
    # of Send A Gift / Language / Developer / Support / Admin Panel is now
    # admin-editable from Menu & UI too, same as every other menu. Only the
    # BANNER text/image is stored here — the live functional buttons on
    # each screen (Stars/UPI, the language list, the developer contact
    # link, the actual open-a-ticket flow, the admin dashboard's own
    # buttons) stay code-driven and are appended after this banner, since
    # those carry real logic that can't be hand-typed as plain buttons.
    "gift": {
        "text": (
            "<blockquote>"
            "✨ 𝐒𝐔𝐏𝐏𝐎𝐑𝐓 𝐎𝐔𝐑 𝐁𝐎𝐓\n\n"
            "Tʜɪꜱ ꜱᴜᴘᴘᴏʀᴛ ɪꜱ ᴄᴏᴍᴘʟᴇᴛᴇʟʏ ᴏᴘᴛɪᴏɴᴀʟ.\n"
            "Wᴇ ɴᴇᴠᴇʀ ꜰᴏʀᴄᴇ ᴀɴʏᴏɴᴇ ᴛᴏ ꜱᴇɴᴅ ᴀ ᴘᴀʏᴍᴇɴᴛ.\n\n"
            "Iꜰ ʏᴏᴜ ᴇɴᴊᴏʏ ᴜꜱɪɴɢ ᴛʜᴇ ʙᴏᴛ ᴀɴᴅ ᴡᴀɴᴛ ᴛᴏ ꜱᴜᴘᴘᴏʀᴛ ɪᴛ, "
            "ʏᴏᴜ ᴄᴀɴ ᴄᴏɴᴛʀɪʙᴜᴛᴇ ᴀɴʏ ᴀᴍᴏᴜɴᴛ ʏᴏᴜ ᴘʀᴇꜰᴇʀ.\n\n"
            "Yᴏᴜʀ ꜱᴜᴘᴘᴏʀᴛ ʜᴇʟᴘꜱ ᴜꜱ ᴋᴇᴇᴘ ᴡᴏʀᴋɪɴɢ ᴏɴ ᴛʜᴇ ʙᴏᴛ, ɪᴍᴘʀᴏᴠɪɴɢ "
            "ᴇxɪꜱᴛɪɴɢ ꜰᴇᴀᴛᴜʀᴇꜱ, ᴀᴅᴅɪɴɢ ɴᴇᴡ ꜰᴜɴᴄᴛɪᴏɴꜱ ᴀɴᴅ ʙʀɪɴɢɪɴɢ ᴍᴏʀᴇ ᴜꜱᴇꜰᴜʟ ᴜᴘɢʀᴀᴅᴇꜱ.\n\n"
            "Wᴇ ꜱɪɴᴄᴇʀᴇʟʏ ᴀᴘᴘʀᴇᴄɪᴀᴛᴇ ᴇᴠᴇʀʏ ʙɪᴛ ᴏꜰ ꜱᴜᴘᴘᴏʀᴛ. ❤️"
            "</blockquote>"
        ),
        "parse_mode": "HTML",
        "image_file_id": None,
        "buttons": [],
        "auto_delete_seconds": None,
        "updated_by": None,
        "updated_at": None,
        "translations": {},
    },
    "language": {
        "text": "🌐 " + to_small_caps("choose your language:"),
        "parse_mode": None,
        "image_file_id": None,
        "buttons": [],
        "auto_delete_seconds": None,
        "updated_by": None,
        "updated_at": None,
        "translations": {},
    },
    "developer": {
        "text": to_small_caps("tap below to message the developer:"),
        "parse_mode": None,
        "image_file_id": None,
        "buttons": [],
        "auto_delete_seconds": None,
        "updated_by": None,
        "updated_at": None,
        "translations": {},
    },
    "support": {
        "text": (
            "<blockquote>"
            + to_title_small_caps("Please type your message below.") + "\n\n"
            + "🛠️ " + to_title_small_caps("Report a problem") + "\n"
            + "💡 " + to_title_small_caps("Share your feedback") + "\n"
            + "❓ " + to_title_small_caps("Ask a question") + "\n"
            + "💭 " + to_title_small_caps("Suggest a feature") + "\n\n"
            + to_title_small_caps("We'll review your message and get back to you as soon as possible.")
            + "</blockquote>"
        ),
        "parse_mode": "HTML",
        "image_file_id": None,
        "buttons": [],
        "auto_delete_seconds": None,
        "updated_by": None,
        "updated_at": None,
        "translations": {},
    },
    "admin": {
        "text": f"│ {to_title_small_caps('Admin Dashboard')} │",
        "parse_mode": None,
        "image_file_id": None,
        "buttons": [],
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
        "force_join_channel": None,   # legacy single force-join target
        "force_join_channels": [],   # [{"chat_id": @username/-100id or None, "link": https://...}]
        "force_join_request_verified": {}, # user_id -> [channel keys] accepted via join request
        "send_as_document": False,    # v3 §8 — send reels as document instead of video
        "document_mode_threshold_mb": 45,  # auto-switch to document above this size
        "premium_plans": [],  # v4 — admin-defined plans: {id, name, days, price_inr, price_stars, enabled}
        "detailed_join_alerts": True,  # new-user/group-start full details -> admin DMs + logger
        "user_activity_dm": True,      # every reel-link a user sends -> owner DM (misuse monitoring)
        "leaderboard_enabled": False,  # admin toggle — top-donor ranking shown inside Send Gift
        "share_enabled": True,         # admin toggle — "📤 Share" button under My Usage
        "share_url": None,             # link the Share button points to; falls back to the bot link
        "premium_emoji_enabled": False,   # #17 — greeting uses a Premium custom emoji
        "premium_emoji_id": None,         # custom_emoji_id captured from the admin's sample message
        "premium_emoji_char": "🌟",       # fallback glyph shown to non-Premium users automatically
        "activity_channel_id": None,      # #18 — dedicated channel for the Reel Delivered card
        "activity_channel_enabled": False,
    },
    "broadcast_log": [],
    "activity_log": [],         # ring buffer: {time, user_id, name, username, chat_type, url}
    "restore_log": [],
    "sent_messages": {},        # #5 — chat_id (str) -> [message_id, ...] ring buffer, last 200
    "copyright_reports": [],    # PDF #3 — DMCA-style user reports
    "blocked_links": [],        # PDF #3 — specific links blocked by admin
    "blocked_domains": [],      # PDF #3 — whole domains blocked by admin
    "error_log": [],            # #13 — capped ring buffer of recent errors
    "metrics": {"reels_downloaded": 0, "audio_gets": 0, "caption_gets": 0, "start_count": 0, "broadcasts_sent": 0},
    "tickets": {},               # v2 §6 — ticket_id(str) -> {...}
    "ticket_msg_map": {},        # v2 §6 — admin_group_message_id(str) -> ticket_id
    "support_msg_map": {},       # one-shot support admin-message-id(str) -> user_id(str)
    "support_requests": {},      # request_id(str) -> {user_id, chat_id, confirm_chat_id,
                                  #   confirm_message_id, admin_message_ids: [...], status, text, created_at}
    "support_admin_msg_map": {}, # one-shot support admin-message-id(str) -> request_id(str)
    "next_support_id": 100000,   # 6-digit request IDs, e.g. #100000, #100001, ...
    "next_ticket_id": 1,
    "panel_msg": {},              # v2 §11 — chat_id(str) -> last panel message_id
    "gift_orders": {},            # v2 §4 — order_id(str) -> {...} (UPI pending payments)
    "next_gift_id": 1,
    "next_plan_id": 1,            # v4 — admin-defined premium plans
    "donations": {},              # uid(str) -> {"name", "stars", "inr", "score"} — leaderboard source
    "maintenance_notified": [],   # chat_id(int) list — everyone shown the maintenance notice,
                                   # so we know exactly who to ping with BOT_LIVE_TEXT on toggle-off
    "maintenance_notice_msg": {},  # chat_id(str) -> message_id(int) of that chat's LATEST maintenance
                                    # notice — lets us delete the old one before sending a new one
                                    # (no more duplicate notices piling up), and delete it automatically
                                    # the moment maintenance is switched off.
}

# ----------------------------------------------------------------------------
# Storage layer (#10 — already existed, kept, menus/settings merge into it)
# ----------------------------------------------------------------------------

BOT_DATA = {}
_mongo_client = None
_mongo_collection = None
_mongo_last_error = None
_rate_state = {}  # in-memory only, not persisted
_caption_cache = {}  # (chat_id, message_id) -> {"caption": str, "url": str}, in-memory only
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


def get_or_create_backup_key() -> "bytes | None":
    """Local Fernet key used to encrypt backup files. Generated once and
    reused — losing this file means old encrypted backups can never be
    decrypted again, so it's kept next to bot_data.json, never inside the
    BACKUP_DIR (which the /export source-zip explicitly excludes anyway)."""
    if not BACKUP_ENCRYPTION_AVAILABLE:
        return None
    if os.path.exists(BACKUP_KEY_FILE):
        with open(BACKUP_KEY_FILE, "rb") as f:
            return f.read().strip()
    key = Fernet.generate_key()
    with open(BACKUP_KEY_FILE, "wb") as f:
        f.write(key)
    log.warning(
        "Generated a new backup encryption key at %s — this file is the "
        "ONLY way to decrypt existing .enc backups. Keep it safe and never "
        "commit it to git.", BACKUP_KEY_FILE,
    )
    return key


def encrypt_backup_bytes(raw: bytes) -> "tuple[bytes, bool]":
    """Returns (payload, was_encrypted). Falls back to plain bytes if the
    `cryptography` package isn't installed, so backups still work either way."""
    key = get_or_create_backup_key()
    if key is None:
        return raw, False
    return Fernet(key).encrypt(raw), True


def decrypt_backup_bytes(payload: bytes) -> bytes:
    """Reverses encrypt_backup_bytes. Raises InvalidToken if the key doesn't
    match, or ValueError if `cryptography` isn't installed but the payload
    is actually encrypted — callers should catch and show a clear message."""
    key = get_or_create_backup_key()
    if key is None:
        raise ValueError("cryptography package not installed — cannot decrypt.")
    return Fernet(key).decrypt(payload)


def make_backup_snapshot(reason: str = "scheduled") -> str:
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    raw = json.dumps(BOT_DATA, ensure_ascii=False, indent=2).encode("utf-8")
    json.loads(raw)  # integrity check before we ever touch disk/encryption
    payload, encrypted = encrypt_backup_bytes(raw)
    ext = "json.enc" if encrypted else "json"
    path = os.path.join(BACKUP_DIR, f"backup_{ts}_{reason}.{ext}")
    with open(path, "wb") as f:
        f.write(payload)
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
            "reels_count": 0, "audio_count": 0, "caption_count": 0,
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


def _force_join_targets():
    """Return normalized multi-force-join targets, while migrating legacy data."""
    settings = BOT_DATA["settings"]
    targets = settings.get("force_join_channels") or []
    if not targets and settings.get("force_join_channel"):
        legacy = settings["force_join_channel"]
        targets = [{"chat_id": legacy if not str(legacy).startswith("http") else None,
                    "link": legacy if str(legacy).startswith("http") else None}]
        settings["force_join_channels"] = targets
    normalized = []
    for item in targets:
        if isinstance(item, str):
            item = {"chat_id": item if not item.startswith("http") else None,
                    "link": item if item.startswith("http") else None}
        if isinstance(item, dict) and (item.get("chat_id") or item.get("link")):
            normalized.append(item)
    return normalized


async def is_force_join_ok(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Require membership in every configured force-join channel.

    Join requests are also accepted: when Telegram sends the bot a
    ChatJoinRequest update, that user is temporarily/explicitly marked as
    verified for that target, so they can start using the bot without waiting
    for a manual re-check.
    """
    targets = _force_join_targets()
    if not targets or is_admin(user_id):
        return True
    verified = BOT_DATA["settings"].get("force_join_request_verified", {}).get(str(user_id), [])
    for target in targets:
        chat_id = target.get("chat_id")
        key = str(chat_id or target.get("link"))
        if key in verified:
            continue
        if not chat_id:
            # Link-only targets cannot be queried by getChatMember. They can
            # still be verified through a join-request update.
            return False
        try:
            member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            if member.status in ("left", "kicked", "restricted"):
                return False
        except Exception as e:
            log.warning("Force-join check failed (target=%s, user=%s): %s", target, user_id, e)
            log_error("force_join", f"target={target}, user={user_id}: {e}")
            return False
    return True


async def resolve_force_join_link(context: ContextTypes.DEFAULT_TYPE, channel) -> str | None:
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


async def get_force_join_links(context):
    links = []
    for target in _force_join_targets():
        link = target.get("link") or await resolve_force_join_link(context, target.get("chat_id"))
        if link:
            links.append(link)
    return links


async def handle_force_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Accept a received join request as verification for force-join."""
    req = update.chat_join_request
    if not req:
        return
    uid = str(req.from_user.id)
    verified_map = BOT_DATA["settings"].setdefault("force_join_request_verified", {})
    keys = set(verified_map.get(uid, []))
    for target in _force_join_targets():
        chat_id = target.get("chat_id")
        if str(chat_id) == str(req.chat.id):
            keys.add(str(chat_id))
        # Match the actual invite link if Telegram exposes it.
        inv = getattr(req, "invite_link", None)
        if inv and target.get("link") and getattr(inv, "invite_link", None) == target.get("link"):
            keys.add(str(target.get("link")))
    if keys:
        verified_map[uid] = list(keys)
        save_data()


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


async def log_event(context: ContextTypes.DEFAULT_TYPE, text: str, parse_mode: str = None):
    """#14 — send a short line to the admin-configured logger channel, if any."""
    settings = BOT_DATA.get("settings", {})
    if not settings.get("logger_enabled") or not settings.get("logger_channel_id"):
        return
    try:
        await context.bot.send_message(chat_id=settings["logger_channel_id"], text=text, parse_mode=parse_mode)
    except Exception:
        log.exception("Failed to send to logger channel")


async def dm_all_admins(context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None, parse_mode: str = None):
    """Send a message to every admin's private chat (owner + BOT_DATA['admins']).
    One admin having blocked the bot / never opened a DM must never stop the
    others from getting it, so each send is isolated."""
    targets = set(BOT_DATA.get("admins", []))
    if OWNER_ID:
        targets.add(OWNER_ID)
    for admin_id in targets:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception:
            log.warning("Could not DM admin %s (bot blocked / never started a DM)", admin_id)


def clickable_user(user_obj) -> str:
    """HTML mention link to a user's Telegram profile — same pattern used by
    the Support flow, reused everywhere a user's name is shown to an admin
    (Live Activity, admin DMs, Support tickets, join alerts, etc.). Falls
    back to a plain @username link when a username exists (works even if
    the user has since blocked the bot), tg://user?id otherwise."""
    name = html.escape(user_obj.full_name or str(user_obj.id))
    if getattr(user_obj, "username", None):
        return f'<a href="https://t.me/{user_obj.username}">{name}</a>'
    return f'<a href="tg://user?id={user_obj.id}">{name}</a>'


_LANGUAGE_NAME_MAP = {
    "en": "English", "hi": "Hindi", "ur": "Urdu", "bn": "Bengali", "ta": "Tamil",
    "te": "Telugu", "mr": "Marathi", "gu": "Gujarati", "kn": "Kannada", "ml": "Malayalam",
    "pa": "Punjabi", "ar": "Arabic", "es": "Spanish", "fr": "French", "de": "German",
    "pt": "Portuguese", "ru": "Russian", "id": "Indonesian", "tr": "Turkish", "zh": "Chinese",
    "ja": "Japanese", "ko": "Korean", "it": "Italian", "vi": "Vietnamese", "fa": "Persian",
}


def build_join_details(update: Update, is_new: bool) -> str:
    """Full detail card for a /start — new user OR bot started inside a
    group — so admins get the complete picture in one glance. HTML: Name
    is a clickable link straight to the user's profile."""
    user = update.effective_user
    chat = update.effective_chat
    lbl = to_title_small_caps
    username_display = f"@{user.username}" if user.username else "Not Set"
    lang_code = (user.language_code or "").lower()
    lang_display = _LANGUAGE_NAME_MAP.get(lang_code, user.language_code or "Unknown")

    lines = [
        "🆕 " + lbl("New User Started Bot" if is_new else "Bot Started In Group"),
        "",
        _CARD_SEP,
        "",
        "👤 " + lbl("User Information"),
        "",
        f"{lbl('Name')} : {clickable_user(user)}",
        f"{lbl('Username')} : {html.escape(username_display)}",
        f"{lbl('User Id')} : {user.id}",
    ]

    if chat.type in ("group", "supergroup"):
        lines += [
            "",
            "👨‍👩‍👧 " + lbl("Group Details"),
            "",
            f"{lbl('Group')} : {html.escape(chat.title or '')}",
            f"{lbl('Group Id')} : {chat.id}",
        ]
    else:
        lines += [
            "",
            "🌐 " + lbl("Account Details"),
            "",
            f"{lbl('Language')} : {lbl(lang_display)}",
            f"{lbl('Chat Type')} : {lbl(chat.type.capitalize())}",
            f"{lbl('Telegram Premium')} : {lbl('Yes') if getattr(user, 'is_premium', False) else lbl('No')}",
        ]

    lines += [
        "",
        "🕒 " + lbl("Started At"),
        "",
        now_ist_str("%d %B %Y • %H:%M:%S") + " IST",
        "",
        _CARD_SEP,
    ]
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
    await dm_all_admins(context, text, parse_mode="HTML")
    await log_event(context, text, parse_mode="HTML")


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

    # NOTE: this used to also DM admins a plain-text "Live Activity" ping
    # right here, at request time. That has been superseded by the richer
    # Reel Delivered-style card (see build_reel_delivered_card /
    # send_reel_delivered_card) which now goes to admin DM once the reel is
    # actually delivered — same "📡 Feed To Admin DM" toggle, one consistent
    # format instead of two different-looking messages for the same event.
    # The activity_log entry above (used by the Live User Feed / quick-ban
    # screen) is still recorded immediately, so nothing about the
    # anti-misuse monitoring itself is lost.


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


IST_OFFSET = timedelta(hours=5, minutes=30)


def to_ist(dt: datetime) -> datetime:
    """All timestamps are stored in UTC internally (unchanged, so old data
    and any external tooling stays correct) — this only converts for
    on-screen display, since admins kept asking why times looked wrong."""
    return dt + IST_OFFSET


def now_ist_str(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return to_ist(datetime.utcnow()).strftime(fmt)


def iso_to_ist_str(iso_str: str, fmt: str = "%d %b %Y, %H:%M") -> str:
    """Safely convert a stored UTC ISO timestamp to an IST display string.
    Falls back to the raw stored value if it can't be parsed, rather than
    ever raising."""
    if not iso_str:
        return "?"
    try:
        return to_ist(datetime.fromisoformat(iso_str)).strftime(fmt)
    except Exception:
        return str(iso_str)


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


def human_uptime_full() -> str:
    """Same as human_uptime() but with seconds, in the small-caps
    'Xᴅᴀʏs, Yʜ:Zᴍ:Ws' style used by the /ping quote block."""
    secs = int(time.time() - START_TIME)
    d, secs = divmod(secs, 86400)
    h, secs = divmod(secs, 3600)
    m, s = divmod(secs, 60)
    if d:
        return f"{d}ᴅᴀʏs, {h}ʜ:{m:02d}ᴍ:{s:02d}s"
    return f"{h}ʜ:{m:02d}ᴍ:{s:02d}s"


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


def get_cpu_percent():
    """System-wide CPU usage %, or None if psutil isn't installed."""
    try:
        import psutil

        return psutil.cpu_percent(interval=0.3)
    except Exception:
        return None


def get_ram_percent():
    """System-wide RAM usage %, or None if psutil isn't installed."""
    try:
        import psutil

        return psutil.virtual_memory().percent
    except Exception:
        return None


def get_disk_percent():
    """Disk usage % of the filesystem the bot runs on, or None if psutil
    isn't installed."""
    try:
        import psutil

        return psutil.disk_usage(os.getcwd()).percent
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
                    styled_button(toggle_label(b["label"], current), callback_data=f"tgl:{b['value']}:{menu_id}", style=st)
                )
            elif btype == "callback":
                row_widgets.append(styled_button(b["label"], callback_data=b["value"], style=style))
            else:
                continue
        if row_widgets:
            kb_rows.append(row_widgets)
    return InlineKeyboardMarkup(kb_rows) if kb_rows else None


_me_cache = {"me": None, "at": 0.0}


async def _cached_get_me(context: ContextTypes.DEFAULT_TYPE):
    """get_me() never changes mid-run, but render_menu() used to call it on
    every single welcome-screen render — an avoidable network round-trip on
    the hottest path in the bot, and one more place a transient network
    hiccup could make the bot's name/link silently fail to show. Cached for
    10 minutes; refreshed automatically after that or if it's never been
    fetched yet."""
    now = time.monotonic()
    if _me_cache["me"] is None or (now - _me_cache["at"]) > 600:
        _me_cache["me"] = await context.bot.get_me()
        _me_cache["at"] = now
    return _me_cache["me"]


async def render_menu(context: ContextTypes.DEFAULT_TYPE, chat_id: int, menu_id: str, existing_message=None, lang: str = None):
    await _clear_ephemeral(context, chat_id)
    menu = BOT_DATA["menus"].get(menu_id)
    if not menu:
        await context.bot.send_message(chat_id, to_small_caps(f"⚠️ menu '{menu_id}' not found."))
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

    # Welcome-screen personalization — supports {username}/{bot_name} and
    # legacy {first_name}/{bot_link} placeholders for the start menu.
    if menu_id == "start":
        if "{username}" in text or "{first_name}" in text:
            stored_name = BOT_DATA["users"].get(str(chat_id), {}).get("name") or ""
            first_name = stored_name.split(" ")[0] if stored_name else "there"
            user_display = html.escape(first_name)
            # Make the user's displayed name open their Telegram profile.
            username_link = f'<a href="tg://user?id={int(chat_id)}">{user_display}</a>'
            text = text.replace("{username}", username_link)
            text = text.replace("{first_name}", user_display)
        if "{bot_name}" in text or "{bot_link}" in text:
            bot_name = "our bot"
            bot_link = "our bot"
            try:
                me = await _cached_get_me(context)
                display_name = html.escape(me.first_name or "our bot")
                bot_name = display_name
                # tg://user?id=<id> (not an https://t.me/<username> link) —
                # same trick already used above for {username}. A t.me/
                # link jumps straight into the chat; this ID-based deep
                # link opens the bot's profile card first instead.
                bot_link = f'<a href="tg://user?id={me.id}">{display_name}</a>'
            except Exception as e:
                log_error("bot_link_resolve", f"render_menu couldn't resolve bot name/link: {e}")
            text = text.replace("{bot_name}", bot_name)
            text = text.replace("{bot_link}", bot_link)

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


async def refresh_panel_after_save(context: ContextTypes.DEFAULT_TYPE, screen_key: str, build_fn, parse_mode=None) -> bool:
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
            chat_id=info["chat_id"], message_id=info["message_id"], text=text, reply_markup=kb, parse_mode=parse_mode,
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

# The welcome screen's live placeholders — {first_name} and {bot_link} (the
# bot's own clickable name/username in the welcome text). BUG FIX: every
# STYLE_OPTIONS function remaps plain a-z/A-Z letters (small caps, bold,
# fullwidth, ...), so styling text that contains these tokens used to
# mangle the literal word "bot_link" inside the braces into unicode
# look-alike letters. render_menu()'s exact `text.replace("{bot_link}", ...)`
# could then never find the token again, so the bot's name/link in the
# welcome message silently stopped being clickable the moment an admin
# styled the welcome text even once. Fixed by shielding these tokens with
# private-use sentinel characters (untouched by every style function) before
# styling, then restoring the real placeholder text afterwards.
_WELCOME_PLACEHOLDERS = ["{first_name}", "{bot_link}", "{username}", "{bot_name}"]


def apply_style_preserving_placeholders(func, text: str) -> str:
    protected = text
    markers = {}
    for i, token in enumerate(_WELCOME_PLACEHOLDERS):
        if token in protected:
            marker = f"\ue000{i}\ue001"
            markers[marker] = token
            protected = protected.replace(token, marker)
    styled = func(protected)
    for marker, token in markers.items():
        styled = styled.replace(marker, token)
    return styled


async def send_style_preview(context, chat_id, source_text):
    rows = []
    for i, (label, func) in enumerate(STYLE_OPTIONS):
        preview = apply_style_preserving_placeholders(func, source_text)
        display = preview if len(preview) <= 30 else preview[:27] + "..."
        rows.append([styled_button(display, callback_data=f"styleset:{i}")])
    await context.bot.send_message(chat_id, to_small_caps("🅰️ choose a style:"), reply_markup=InlineKeyboardMarkup(rows))


async def _replace_rkb_screen(context: ContextTypes.DEFAULT_TYPE, chat_id: int, key: str, text: str, reply_markup=None, parse_mode=None):
    """Delete the previous message shown for this reply-keyboard screen (if
    any) before sending the new one — for EVERY persistent bottom button
    (📊 My Usage, 🎁 Send A Gift, 👨‍💻 Developer, 📘 How To Use, 🎧 Support,
    ⬇️ Download Reel, 🌐 Language), not just one of them. Without this,
    tapping the same button over and over just stacked a fresh bot reply
    under the last one every time, click after click. Uses the same
    persisted panel_msg store as /start and /admin, so it survives a bot
    restart, not just context.user_data."""
    msg = await context.bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
    # All persistent reply-keyboard actions share one panel slot. This means
    # the user's own button message remains in chat, while only the latest
    # bot-side screen is replaced on every button tap.
    await track_and_refresh_panel(context, chat_id, "rkb_latest", msg)
    return msg



async def cb_styleset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    idx = int(query.data.split(":", 1)[1])
    src = context.user_data.pop("style_source_text", None)
    target = context.user_data.pop("style_target", None)
    if src is None or target is None:
        await query.edit_message_text(to_small_caps("session expired — please try again."))
        return
    label, func = STYLE_OPTIONS[idx]
    styled_text = apply_style_preserving_placeholders(func, src)

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


# ----------------------------------------------------------------------------
# FIX — "chat ekdum clear rakhna hai": the Send-Gift/Stars support flow (pick
# amount -> pay) used to leave a trail of "choose an amount" / invoice
# messages sitting in the chat forever. These are now tracked per-user and
# swept away the moment the person opens any other menu (Start, My Usage,
# Gift menu, Admin Panel) — same "disappears on next menu" behaviour asked
# for, without needing a bot restart or persistent duplicate messages.
# ----------------------------------------------------------------------------

def _track_ephemeral(context: ContextTypes.DEFAULT_TYPE, message) -> None:
    if message is None:
        return
    ids = context.user_data.setdefault("ephemeral_msg_ids", [])
    ids.append((message.chat_id, message.message_id))


async def _clear_ephemeral(context: ContextTypes.DEFAULT_TYPE, chat_id: int = None) -> None:
    ids = context.user_data.pop("ephemeral_msg_ids", [])
    for cid, mid in ids:
        if chat_id is not None and cid != chat_id:
            continue
        try:
            await context.bot.delete_message(chat_id=cid, message_id=mid)
        except Exception:
            pass


async def require_disclaimer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """PDF #1 — gate behind the disclaimer/agree flow only (no force-join
    check). Kept as a standalone building block for require_gate() below;
    most call sites should use require_gate() instead, which also enforces
    force-join. Returns True if the user may proceed; otherwise shows the
    disclaimer and returns False. Admins are exempt."""
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


async def show_force_join_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show one full-width-looking join button per required channel."""
    links = await get_force_join_links(context)
    kb_rows = []
    for i, link in enumerate(links, 1):
        label = "📢 JOIN CHANNEL" if len(links) == 1 else f"📢 JOIN CHANNEL {i}"
        kb_rows.append([InlineKeyboardButton(label, url=link)])
    kb_rows.append([styled_button("✅ I'VE JOINED / SENT REQUEST", callback_data="check_force_join", style="success")])
    text = "🔒 " + to_small_caps("please join all required channels to use this bot.")
    if not links:
        text += "\n⚠️ " + to_small_caps("no usable join link found — contact an admin.")
    chat_id = update.effective_chat.id
    if update.callback_query:
        try:
            await update.callback_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb_rows))
            return
        except Exception:
            pass
    await context.bot.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(kb_rows))


# NOTE: the maintenance notice and the "bot is live again" message are both
# fully admin-customisable — edit them anytime via 🎨 Menu & UI → maintenance
# / bot_live, or directly from Settings → 🔒 Maintenance → ✏️ Set New Message.
# The constant below only exists as a last-resort fallback if the "bot_live"
# menu entry is ever missing from storage.
_BOT_LIVE_FALLBACK = (
    "✅ 𝐁𝐎𝐓 𝐈𝐒 𝐋𝐈𝐕𝐄\n\n"
    "ᴍᴀɪɴᴛᴇɴᴀɴᴄᴇ ɪꜱ ᴄᴏᴍᴘʟᴇᴛᴇ — ᴛʜᴇ ʙᴏᴛ ɪꜱ ʙᴀᴄᴋ ᴜᴘ ᴀɴᴅ ʀᴜɴɴɪɴɢ ɴᴏʀᴍᴀʟʟʏ."
)


async def _send_typewriter(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str,
    reply_markup=None, parse_mode=None, delay: float = 0.25,
):
    """Reveals the message progressively, word by word, instead of dumping
    the full block instantly — a light animation just enough for a premium
    'someone is actually typing this' feel without dragging the user's wait
    time out. The button (if any) only appears on the final, complete
    message.

    parse_mode is applied ONLY on the final, complete frame — every
    intermediate reveal is sent as plain text. This matters when `text`
    contains HTML (e.g. a <blockquote> wrapper): parsing a half-revealed
    string as HTML would leave an unclosed tag and Telegram would reject
    the edit, so intermediate frames are deliberately unparsed and only the
    finished message renders styled.

    `delay` controls the pause between reveal steps — bump it slightly
    (e.g. for the Support confirmation) for a more deliberate, human-typed
    feel; the default stays snappy for shorter, lower-stakes messages."""
    words = text.split(" ")
    if len(words) <= 4:
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")
            await asyncio.sleep(max(delay, 0.5))
        except Exception:
            pass
        return await context.bot.send_message(
            chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode
        )

    try:
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    except Exception:
        pass

    steps = 5  # short and deliberate — just enough to feel alive, not slow
    chunk = max(1, -(-len(words) // steps))  # ceil division
    msg = None
    for i in range(chunk, len(words) + chunk, chunk):
        shown = " ".join(words[:i])
        is_last = i >= len(words)
        cursor = "" if is_last else " ▌"
        try:
            if msg is None:
                msg = await context.bot.send_message(chat_id=chat_id, text=shown + cursor)
            else:
                await msg.edit_text(
                    shown + cursor,
                    reply_markup=reply_markup if is_last else None,
                    parse_mode=parse_mode if is_last else None,
                )
        except Exception:
            pass
        if not is_last:
            try:
                await context.bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception:
                pass
            await asyncio.sleep(delay)
    if msg is None:
        msg = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
    return msg


async def send_maintenance_notice(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Show the maintenance message — typed out live, with the 🔔 Notify Me
    button attached. Just ONE message, nothing after it.

    FIX — this used to send a brand-new notice every single time a user
    tried anything while maintenance was on, so a user who kept tapping
    around ended up with a growing pile of identical "bot is offline"
    messages. Now, before sending a fresh one, we delete that chat's
    previous notice (if it's still around) — so at any moment there is at
    most ONE maintenance message sitting in the chat, old one gone, new
    one in its place."""
    notice_map = BOT_DATA.setdefault("maintenance_notice_msg", {})
    chat_key = str(chat_id)
    prev_msg_id = notice_map.get(chat_key)
    if prev_msg_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=prev_msg_id)
        except Exception:
            pass  # already gone / too old to delete — fine, we just move on
        notice_map.pop(chat_key, None)

    try:
        menu = BOT_DATA["menus"].get("maintenance", {})
        if menu.get("image_file_id"):
            # admin has attached an image via the generic Menu & UI editor —
            # animation doesn't apply there, send normally.
            first = await render_menu(context, chat_id, "maintenance")
        else:
            text = menu.get("text") or to_small_caps("maintenance is currently active.")
            buttons = menu.get("buttons") or []
            kb = build_keyboard_from_buttons(buttons, "maintenance") if buttons else None
            first = await _send_typewriter(context, chat_id, text, reply_markup=kb)

        # Remember every chat_id shown the notice, so that when maintenance
        # is switched off we know exactly who to notify (instead of nobody
        # finding out except by tapping something again) — and remember
        # THIS message's id specifically, so it can be auto-deleted the
        # moment maintenance goes off, or replaced next time this fires.
        notified = BOT_DATA.setdefault("maintenance_notified", [])
        if chat_id not in notified:
            notified.append(chat_id)
        if first is not None:
            notice_map[chat_key] = first.message_id
        save_data()
        return first
    except Exception:
        return None


async def broadcast_bot_live(context: ContextTypes.DEFAULT_TYPE):
    """Ping everyone who saw the maintenance notice, telling them the bot is
    back up — then clear the list so it doesn't grow forever / re-notify
    people on the next maintenance cycle.

    FIX — each person's old maintenance notice used to just sit there
    forever while the "bot is live" message was sent separately underneath
    it. Now their maintenance notice is deleted first, and the "bot is
    live" message takes its place — no leftover stale card."""
    live_menu = BOT_DATA["menus"].get("bot_live", {})
    live_text = live_menu.get("text") or _BOT_LIVE_FALLBACK
    chat_ids = BOT_DATA.get("maintenance_notified", [])
    notice_map = BOT_DATA.setdefault("maintenance_notice_msg", {})
    for chat_id in chat_ids:
        prev_msg_id = notice_map.pop(str(chat_id), None)
        if prev_msg_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=prev_msg_id)
            except Exception:
                pass
        try:
            await context.bot.send_message(chat_id=chat_id, text=live_text, parse_mode=live_menu.get("parse_mode"))
        except Exception:
            pass
    BOT_DATA["maintenance_notified"] = []
    notice_map.clear()
    save_data()


async def require_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """The single source of truth for 'is this user allowed to do anything
    yet'. Combines the disclaimer-acceptance gate AND the force-join gate —
    until BOTH are satisfied, nothing else in the bot should run: no
    command, no inline button, no reply-keyboard button. Admins are exempt
    from both. Returns True if the user may proceed; otherwise shows
    whichever screen is still pending (disclaimer takes priority over
    force-join, since there's no point sending someone to join a channel
    before they've even agreed to use the bot) and returns False."""
    user_obj = update.effective_user
    if not user_obj:
        return True
    if is_admin(user_obj.id):
        return True
    if not await require_disclaimer(update, context):
        return False
    if not await is_force_join_ok(context, user_obj.id):
        await show_force_join_prompt(update, context)
        return False
    return True


async def cb_global_button_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Registered in handler group -1, ahead of EVERY other handler — this
    is what makes 'no button works until agree + join' actually true,
    instead of each of 100+ individual callback handlers needing its own
    check (which is exactly how it stayed half-enforced before: some
    screens checked, most didn't). is_admin() inside require_gate() exempts
    admins as usual. The two callbacks that ARE the gate itself
    (agree_terms, check_force_join) must fall through untouched, or the
    user would have no way to ever pass the gate."""
    query = update.callback_query
    if not query:
        return
    data = query.data or ""
    if data in ("maint_notify_me", "maint_notify_me_done"):
        # This IS the maintenance screen's own button — it must always work
        # while maintenance is on, or tapping it would just re-trigger the
        # maintenance notice instead of confirming the opt-in.
        return
    if BOT_DATA["settings"].get("maintenance") and not is_admin(update.effective_user.id):
        await send_maintenance_notice(context, update.effective_chat.id)
        try:
            await query.answer()
        except Exception:
            pass
        raise ApplicationHandlerStop
    if data in ("agree_terms", "check_force_join"):
        return
    if not await require_gate(update, context):
        try:
            await query.answer()
        except Exception:
            pass
        raise ApplicationHandlerStop


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
            await context.bot.send_message(chat_id, "⠀", reply_markup=main_reply_keyboard(is_admin(int(uid))))
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

    # Group-specific /start: don't show the private onboarding/gate flow in
    # a group. Show a clean card with the group name as a link and who
    # started the bot, matching the bot's house typography.
    if not _is_private_chat(update):
        chat = update.effective_chat
        title = html.escape(chat.title or "This Group")
        if getattr(chat, "username", None):
            group_link = f"https://t.me/{chat.username}"
        elif str(chat.id).startswith("-100") and update.message:
            group_link = f"https://t.me/c/{str(chat.id)[4:]}/{update.message.message_id}"
        else:
            group_link = None
        group_name = f'<a href="{group_link}">{title}</a>' if group_link else title
        starter = update.effective_user
        starter_name = html.escape(starter.full_name or "User")
        starter_link = f'<a href="tg://user?id={starter.id}">{starter_name}</a>'
        body = (
            "<blockquote>🚀 " + to_title_small_caps("Bot Started In") + "\n\n"
            + "👥 " + group_name + "\n"
            + "👤 " + to_title_small_caps("Started By") + " : " + starter_link + "\n\n"
            + to_title_small_caps("Send An Instagram Reel Link To Download It.")
            + "</blockquote>"
        )
        await update.message.reply_text(body, parse_mode="HTML", disable_web_page_preview=True)
        await notify_admins_new_start(context, update, is_new)
        await delete_incoming(update)
        return

    # FIX — this notification used to fire only AFTER the rate-limit /
    # maintenance / disclaimer+force-join gate checks below all passed. Any
    # one of those returning early (extremely common: force-join is the
    # normal setup) meant the "New User Started Bot" detail card never went
    # out. And because touch_user() already saved the user on this very
    # first call, is_new would be False on every later /start from that
    # same person — so admins could end up NEVER being notified about a
    # new user at all. Fire it here, right where is_new is known, before
    # anything else has a chance to return early.
    await notify_admins_new_start(context, update, is_new)
    if not check_rate_limit(user_obj.id):
        await update.message.reply_text("⏳ " + to_small_caps("slow down, too many requests too fast."))
        await delete_incoming(update)
        return
    if BOT_DATA["settings"].get("maintenance") and not is_admin(user_obj.id):
        await send_maintenance_notice(context, update.effective_chat.id)
        await delete_incoming(update)
        return

    if not await require_gate(update, context):
        await delete_incoming(update)
        return

    BOT_DATA["metrics"]["start_count"] = BOT_DATA["metrics"].get("start_count", 0) + 1
    save_data()

    await send_premium_emoji_greeting(context.bot, update.effective_chat.id)
    sent = await show_post_onboarding(context, update.effective_chat.id, str(user_obj.id))
    await track_and_refresh_panel(context, update.effective_chat.id, "start", sent)
    await delete_incoming(update)


async def cb_maint_notify_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The 🔔 Notify Me button under the maintenance message. Tapping it:
    1) confirms the chat_id is on the notify list (it already is, but this
       makes the user's intent explicit), 2) turns the button itself from
       red → a blue, checked "Notified" state so it's visually obvious the
       tap registered, and 3) explains in plain words what happens next."""
    query = update.callback_query
    chat_id = update.effective_chat.id
    notified = BOT_DATA.setdefault("maintenance_notified", [])
    if chat_id not in notified:
        notified.append(chat_id)
        save_data()
    try:
        await query.answer(
            "🔔 " + to_small_caps("notifications on!") + "\n"
            + to_small_caps("we'll message you here the instant the bot is back — no need to check manually."),
            show_alert=True,
        )
    except Exception:
        pass
    try:
        confirmed_kb = InlineKeyboardMarkup(
            [[styled_button("✅ " + to_small_caps("you'll be notified"), callback_data="maint_notify_me_done", style="primary")]]
        )
        # Beyond the popup alert (which disappears in a couple seconds),
        # leave a permanent line inside the message itself so the
        # confirmation stays visible in the chat, not just flashed once.
        confirm_line = "\n\n🔔 " + to_small_caps("notification set — we'll message you the moment the bot is back online.")
        base_text = query.message.caption if query.message.photo else query.message.text
        base_text = base_text or ""
        new_text = base_text if confirm_line.strip() in base_text else base_text + confirm_line
        if query.message.photo:
            await query.edit_message_caption(caption=new_text, reply_markup=confirmed_kb)
        else:
            await query.edit_message_text(new_text, reply_markup=confirmed_kb)
    except Exception:
        pass


async def cb_maint_notify_me_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Button already shows the confirmed (blue ✅) state — a second tap
    just reassures the user, it doesn't need to do anything further."""
    try:
        await update.callback_query.answer(
            "✅ " + to_small_caps("you're all set — you'll be notified automatically."),
            show_alert=False,
        )
    except Exception:
        pass


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_obj = update.effective_user
    if is_blocked(user_obj.id) and not is_admin(user_obj.id):
        return
    touch_user(update)
    if not await require_gate(update, context):
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
    if not await require_gate(update, context):
        await delete_incoming(update)
        return
    langs = BOT_DATA["settings"].get("languages", [])
    menu = BOT_DATA["menus"].get("language", {})
    banner = menu.get("text") or DEFAULT_MENUS["language"]["text"]
    if not langs:
        await _replace_rkb_screen(
            context, update.effective_chat.id, "language",
            to_small_caps("no extra languages are configured yet."),
        )
        return
    await _replace_rkb_screen(
        context, update.effective_chat.id, "language",
        banner, reply_markup=build_language_keyboard(), parse_mode=menu.get("parse_mode"),
    )


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
    # Disclaimer accepted — now check the OTHER half of the gate before
    # letting the user any further in. If a force-join channel is set, they
    # see the join prompt right here instead of skipping straight to the
    # start menu.
    if not await is_force_join_ok(context, update.effective_user.id):
        await show_force_join_prompt(update, context)
        return
    await show_post_onboarding(context, query.message.chat_id, uid)


# ----------------------------------------------------------------------------
# Reel download
# ----------------------------------------------------------------------------

def build_reel_delivered_card(user_name: str, user_id, reel_number, original_reel_url: str, username: str = None) -> str:
    """HTML card used for both the Activity Channel AND the admin-DM Live
    Activity feed — same layout in both places so admins never see two
    different formats for the same event.

    Layout (per spec):
        Rᴇᴇʟ Nᴏ : #2      <- underlined (label + value)
        Sᴛᴀᴛᴜs : Dᴇʟɪᴠᴇʀᴇᴅ <- underlined (label + value)

        Uꜱᴇʀ : <clickable name>
        Uꜱᴇʀɴᴀᴍᴇ : Not Set / @username
        Uꜱᴇʀ ID : 123456

        Click Here To View Reel   (bare link, no visible URL)

    All dynamic values are HTML-escaped before insertion, including the
    URL used in the href attribute. The user's name is a clickable link
    straight to their Telegram profile (via @username if set, otherwise
    tg://user?id), same pattern as clickable_user() elsewhere in the bot.
    """
    safe_name = html.escape(str(user_name or "—"))
    safe_uid = html.escape(str(user_id))
    safe_no = html.escape(str(reel_number))
    safe_url = html.escape(original_reel_url or "", quote=True)
    username_display = f"@{username}" if username else "Not Set"

    if username:
        name_html = f'<a href="https://t.me/{html.escape(username, quote=True)}">{safe_name}</a>'
    else:
        name_html = f'<a href="tg://user?id={safe_uid}">{safe_name}</a>'

    reel_no_line = f"<u>{to_title_small_caps('Reel No')} : #{safe_no}</u>"
    status_line = f"<u>{to_title_small_caps('Status')} : {to_title_small_caps('Delivered')}</u>"

    return (
        f"{reel_no_line}\n"
        f"{status_line}\n\n"
        f"{to_title_small_caps('User')} : {name_html}\n"
        f"{to_title_small_caps('Username')} : {html.escape(username_display)}\n"
        f"{to_title_small_caps('User Id')} : {safe_uid}\n\n"
        f'<a href="{safe_url}">{to_title_small_caps("Click Here To View Reel")}</a>'
    )


async def send_reel_delivered_card(context, user_name: str, user_id, reel_number, original_reel_url: str, username: str = None):
    """Posts the reel-delivered card to two places:
      1. The Activity Channel, if configured/enabled (unchanged behavior).
      2. Every admin's DM — this is the new "Live Activity" feed, replacing
         the old plain-text ping, gated by the same "📡 Feed To Admin DM"
         toggle (adm_live > user_activity_dm) admins already know."""
    card = build_reel_delivered_card(user_name, user_id, reel_number, original_reel_url, username=username)

    s = BOT_DATA["settings"]
    if s.get("activity_channel_enabled") and s.get("activity_channel_id"):
        try:
            await context.bot.send_message(
                chat_id=s["activity_channel_id"],
                text=card,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            log.warning("Activity Channel reel-delivered post failed", exc_info=True)

    if s.get("user_activity_dm", True):
        notif_kb = InlineKeyboardMarkup([[styled_button("🔔 Notification Center", callback_data="adm_notifications")]])
        await dm_all_admins(context, card, reply_markup=notif_kb, parse_mode="HTML")
        await log_event(context, card, parse_mode="HTML")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # BUGFIX — root cause of the recurring "'NoneType' object has no
    # attribute 'reply_to_message'" crash in the Activity Log: this handler
    # is filter-matched against update.effective_message, which is also
    # populated for edited messages / channel posts — but update.message
    # itself is None for those. Every line below reads update.message
    # directly, so without this guard any edited message crashed here.
    if update.message is None:
        return
    user_obj = update.effective_user
    if is_blocked(user_obj.id) and not is_admin(user_obj.id):
        return  # silently ignored, per spec

    # Maintenance is a hard global lock for non-admins: no reply-keyboard
    # action, support flow, reel parsing, auto-reply or media flow can run.
    if BOT_DATA["settings"].get("maintenance") and not is_admin(user_obj.id):
        await send_maintenance_notice(context, update.effective_chat.id)
        return

    # v2 §6 — an admin replying (in the admin group / their DM) to a forwarded
    # ticket message routes straight back to that user, bypassing everything else.
    if update.message.reply_to_message and is_admin(user_obj.id):
        support_uid = BOT_DATA.get("support_msg_map", {}).get(str(update.message.reply_to_message.message_id))
        if support_uid:
            # v10 — two fixes requested: (1) the admin gets a clear
            # delivered/failed confirmation instead of silence, and (2) the
            # user's copy of the reply is prefixed with a quote of their own
            # original problem, so it's obvious which issue the reply is
            # about instead of a bare message showing up out of context.
            reply_rid = BOT_DATA.get("support_admin_msg_map", {}).get(
                str(update.message.reply_to_message.message_id)
            )
            problem_text = None
            if reply_rid:
                problem_text = BOT_DATA.get("support_requests", {}).get(reply_rid, {}).get("text")
            try:
                if problem_text:
                    tag = (
                        "<blockquote>"
                        + to_title_small_caps("Admin's Reply") + " ↩️" + "\n\n"
                        + f"«{html.escape(problem_text[:300])}»"
                        + "</blockquote>"
                    )
                    await context.bot.send_message(chat_id=int(support_uid), text=tag, parse_mode="HTML")
                await context.bot.copy_message(
                    chat_id=int(support_uid),
                    from_chat_id=update.effective_chat.id,
                    message_id=update.message.message_id,
                )
                await update.message.reply_text("✅ " + to_title_small_caps("Reply sent to user."))
            except Exception:
                await update.message.reply_text(
                    "⚠️ " + to_title_small_caps("Couldn't deliver reply — the user may have blocked the bot.")
                )
            return
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

    # Group safety: never answer normal group conversation. The bot only
    # reacts to an actual Instagram URL (with or without @BotUsername).
    # Private chats retain the normal helpful invalid-link feedback.
    if not _is_private_chat(update) and text:
        if not INSTAGRAM_URL_RE.search(text):
            return

    # BUGFIX #2 (part 2) — non-text media that isn't part of an active ticket
    # or awaited-input flow has nothing to do here; don't fall through to the
    # "not a valid reel link" text reply for a bare photo/video.
    if not text and not context.user_data.get("awaiting"):
        return

    # Gate check comes before EVERY reply-keyboard/button dispatch below —
    # this used to sit much further down (after the awaiting-input checks),
    # which meant a user who hadn't agreed to the disclaimer yet (or hadn't
    # joined the force-join channel) could still tap "📊 My Usage", "🎁 Send
    # a Gift", etc. and have them actually work. Nothing past this point
    # should run until require_gate() clears.
    if not await require_gate(update, context):
        return

    # v2 §1 — persistent reply-keyboard routing. Every branch here deletes
    # the user's own tapped-button message (it was piling up in the chat
    # right alongside the bot's replies) and routes its reply through the
    # same replace-in-place screen, so repeat taps on ANY of these buttons
    # — not just My Usage — leave only the most recent copy behind.
    # RKB_ADMINPANEL is excluded here — cmd_admin() replies to this exact
    # message first (reply_text) and only then deletes it itself, so
    # deleting it up-front would break that reply-to reference.
    _RKB_BUTTON_TEXTS = {
        RKB_DOWNLOAD, RKB_USAGE, RKB_GIFT, RKB_LANGUAGE,
        RKB_DEVELOPER, RKB_HOWTO, RKB_SUPPORT,
    }
    if text in _RKB_BUTTON_TEXTS:
        # This was previously missing — the comment above described this
        # behaviour but no code actually deleted the tapped message, so old
        # button-tap messages from the user kept piling up in the chat.
        await delete_incoming(update)
        # BUGFIX — tapping any bottom-keyboard button must cancel whatever
        # text-collection state was pending (e.g. Support's "awaiting":
        # "support_message"). Without this, closing Support and tapping a
        # different button (How To Use, My Usage, ...) left "awaiting"
        # stuck at "support_message", so the NEXT plain text the user typed
        # — completely unrelated to Support — silently got sent to Support
        # instead of being treated normally. Branches below that need their
        # own awaiting state (Support) set it again right after this, so
        # this is always safe.
        context.user_data.pop("awaiting", None)
    if text == RKB_DOWNLOAD:
        # Now a fully admin-editable menu (text/image/buttons) via
        # Menu & UI → "download", instead of a hardcoded string — same
        # single-slot panel behavior as every other reply-keyboard screen.
        sent = await render_menu(context, update.effective_chat.id, "download")
        await track_and_refresh_panel(context, update.effective_chat.id, "rkb_latest", sent)
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
        # Same treatment as Download Reel above — admin-editable via
        # Menu & UI → "howto".
        sent = await render_menu(context, update.effective_chat.id, "howto")
        await track_and_refresh_panel(context, update.effective_chat.id, "rkb_latest", sent)
        return
    if text == RKB_SUPPORT:
        await support_button_entry(update, context)
        return
    if text == RKB_ADMINPANEL and is_admin(user_id):
        await cmd_admin(update, context)
        return

    awaiting = context.user_data.get("awaiting")

    if awaiting == "premium_emoji_capture" and is_admin(user_id):
        await handle_premium_emoji_capture(update, context)
        return

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
        # Never spam groups for ordinary conversation. Auto-replies and the
        # invalid-link message are intentionally private-chat only.
        if not _is_private_chat(update):
            return
        low = text.lower()
        for phrase, reply in BOT_DATA["settings"].get("auto_replies", {}).items():
            if phrase in low:
                await update.message.reply_text(reply)
                return
        await update.message.reply_text(USER_ERR_WRONG_FORMAT)
        return

    # Maintenance is already enforced at the top of handle_text().

    # (disclaimer + force-join already verified by require_gate() above —
    # no need to re-check either one here)

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
        # FIX — "video+audio hamesha clear download ho": the old format string
        # ("bestvideo+bestaudio/best") had no format_sort, so yt-dlp could
        # pick mismatched/lower-quality video+audio pairs, or a codec combo
        # that plays back muted/glitchy on some devices. Now:
        #  1. The no-merge path explicitly requires a format that already
        #     has BOTH video and audio, so it can never silently pick a
        #     video-only stream and ship a muted reel.
        #  2. format_sort prefers resolution first, then mp4/h264/aac — the
        #     combo every Telegram client can always play cleanly.
        #  3. Audio is re-encoded to AAC on merge (video is left untouched
        #     via -c:v copy) so a rare opus/vorbis track from IG can't end
        #     up silent or unplayable inside Telegram's in-app player.
        opts = {
            "format": (
                "bestvideo*+bestaudio/best"
                if use_merge
                else "best[vcodec!=none][acodec!=none]/best"
            ),
            "format_sort": ["res", "ext:mp4:m4a", "vcodec:h264", "acodec:aac"],
            "outtmpl": out_template,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [_progress_hook],
        }
        if use_merge:
            opts["merge_output_format"] = "mp4"
            opts["postprocessor_args"] = {
                "ffmpeg_merger": ["-c:v", "copy", "-c:a", "aac", "-movflags", "+faststart"]
            }
            if FFMPEG_PATH:
                opts["ffmpeg_location"] = FFMPEG_PATH
        return opts

    def run_download(use_merge: bool):
        opts = build_ydl_opts(use_merge)
        try:
            info = _ytdlp_extract_with_retry(opts, url, download=True)
        except Exception as first_error:
            # Instagram occasionally exposes only one extractor-visible stream
            # for a reel. Retry once with yt-dlp's broadest format selector
            # instead of failing solely because our quality preference was too
            # strict. The real exception still reaches logs if this also fails.
            log.warning("Preferred Instagram format failed; retrying with plain best: %s", first_error)
            opts = {
                "format": "best/bestvideo+bestaudio",
                "outtmpl": out_template,
                "quiet": True,
                "no_warnings": True,
                "progress_hooks": [_progress_hook],
            }
            if FFMPEG_PATH:
                opts["ffmpeg_location"] = FFMPEG_PATH
            info = _ytdlp_extract_with_retry(opts, url, download=True)
        with yt_dlp.YoutubeDL(opts) as ydl:
            fp = ydl.prepare_filename(info)
        stem = fp.rsplit(".", 1)[0]
        for candidate in (fp, stem + ".mp4", stem + ".webm", stem + ".mkv"):
            if os.path.exists(candidate):
                fp = candidate
                break
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
        # The delivered reel gets a purpose-built action row.  The old
        # Caption callback is intentionally removed: short captions use
        # Telegram's native CopyTextButton, while long captions fall back
        # to a callback that sends the complete caption for normal copy.
        parse_mode = menu.get("parse_mode") or None
        kb = None

        # v2 §9 — native Telegram blockquote with extra reel info, HTML only.
        if parse_mode == "HTML":
            import html as _html
            # Keep the caption visibly quoted under the reel.  Telegram's
            # native copy button can copy up to 256 characters; longer
            # captions get a callback fallback below so nothing is lost.
            preview = ig_caption[:700] + ("…" if len(ig_caption) > 700 else "")
            bq = (
                "<blockquote expandable>"
                f"📋 {_html.escape(preview) or '(none)'}"
                "</blockquote>"
            )
            result_caption = f"{base_caption}\n\n{bq}"
        else:
            result_caption = base_caption

        # Compact reel controls: Caption + Audio stay together on the first
        # row, while the full-width Remove Buttons control sits underneath.
        # Labels use the bot's small-caps font so the controls match the rest
        # of the user-facing UI.
        if ig_caption:
            if len(ig_caption) <= 256:
                copy_btn = InlineKeyboardButton(
                    to_small_caps("📋 Caption"),
                    copy_text=CopyTextButton(text=ig_caption),
                )
            else:
                copy_btn = styled_button(
                    to_small_caps("📋 Caption"),
                    callback_data="copy_caption", style="primary"
                )
        else:
            copy_btn = None

        top_row = []
        if copy_btn is not None:
            top_row.append(copy_btn)
        top_row.append(styled_button(
            to_small_caps("🎵 Audio"), callback_data="get_audio", style="primary"
        ))
        kb_rows = [top_row, [styled_button(
            to_small_caps("❌ Remove Buttons"),
            callback_data="remove_reel", style="danger"
        )]]
        kb = InlineKeyboardMarkup(kb_rows)

        anim_task.cancel()
        protect = bool(BOT_DATA["settings"].get("lock_all_content", False))
        # v3 §8 — send as document when admin forces it, or file is close to
        # the 50MB cap (documents preserve quality better near the limit).
        threshold = BOT_DATA["settings"].get("document_mode_threshold_mb", 45) * 1024 * 1024
        as_document = BOT_DATA["settings"].get("send_as_document", False) or file_size > threshold
        # Tell the user the reel is ready immediately before delivering the
        # actual media. Keep this as a separate, clean message.
        try:
            await status_msg.edit_text(to_small_caps("Your reel is ready"))
        except Exception:
            pass

        with open(file_path, "rb") as vid:
            if as_document:
                sent = await update.message.reply_document(
                    document=vid, caption=result_caption, parse_mode=parse_mode, reply_markup=kb, protect_content=protect
                )
            else:
                sent = await update.message.reply_video(
                    video=vid, caption=result_caption, parse_mode=parse_mode, reply_markup=kb, protect_content=protect
                )

        # The original Instagram link is only needed during processing. Once
        # the reel has been delivered successfully, remove that incoming link
        # from the user's chat, matching the chat-cleanup behavior used by
        # the admin input flows.
        await delete_incoming(update)

        # Cache the real Instagram caption + source URL so the Copy Caption
        # fallback, Audio button, and Remove button under THIS specific video
        # can use them, keyed
        # to this exact message. The video file itself is deleted right
        # after sending (see finally: below), so Audio re-downloads
        # audio-only from the cached URL rather than needing the video kept
        # around on disk.
        _caption_cache[(sent.chat_id, sent.message_id)] = {
            "caption": ig_caption,
            "url": url,
            "uploader": ig_uploader,
            "title": (ig_caption.splitlines()[0] if ig_caption else ig_uploader or "Instagram Audio"),
        }
        if len(_caption_cache) > CAPTION_CACHE_MAX:
            _caption_cache.pop(next(iter(_caption_cache)))

        track_sent_message(sent.chat_id, sent.message_id)
        bump_usage(uid)
        BOT_DATA["metrics"]["reels_downloaded"] = BOT_DATA["metrics"].get("reels_downloaded", 0) + 1
        BOT_DATA["users"].setdefault(uid, {})["reels_count"] = BOT_DATA["users"].get(uid, {}).get("reels_count", 0) + 1
        save_data()
        await log_event(
            context,
            f"📥 New download — user {user_id} (@{user_obj.username or 'no username'}) — {url}",
        )
        await send_reel_delivered_card(
            context,
            user_name=user_obj.full_name or (f"@{user_obj.username}" if user_obj.username else str(user_id)),
            user_id=user_id,
            reel_number=BOT_DATA["metrics"]["reels_downloaded"],
            original_reel_url=url,
            username=user_obj.username,
        )

        seconds = menu.get("auto_delete_seconds")
        if seconds is None:
            seconds = BOT_DATA["settings"].get("global_auto_delete_seconds", 0)
        await schedule_delete(context, sent.chat_id, sent.message_id, seconds)
        await status_msg.delete()
    except Exception as e:  # noqa: BLE001
        anim_task.cancel()
        log.exception("Download failed")
        # Technical details stay in logs/admin activity only. Never expose
        # yt-dlp/Instagram/ffmpeg exceptions to the end user.
        if "no video formats found" in str(e).lower():
            log_error("ytdlp_no_formats", f"url={url} err={e} — yt-dlp may need updating (pip install -U yt-dlp)")
        log_error("download", f"url={url} err={e}")
        try:
            await status_msg.edit_text(USER_ERR_NOT_AVAILABLE, parse_mode="HTML")
        except Exception:
            try:
                await update.message.reply_text(USER_ERR_NOT_AVAILABLE, parse_mode="HTML")
            except Exception:
                pass
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
    entry = _caption_cache.get(key)
    caption = entry.get("caption") if entry else None
    if not caption:
        await query.message.reply_text("ℹ️ " + to_small_caps("no caption found for this post (or cache expired)."))
        return
    # Telegram message limit is 4096 chars — split if needed.
    for i in range(0, len(caption), 4000):
        await query.message.reply_text(caption[i:i + 4000])
    uid = str(query.from_user.id)
    u = BOT_DATA["users"].setdefault(uid, {})
    u["caption_count"] = u.get("caption_count", 0) + 1
    BOT_DATA["metrics"]["caption_gets"] = BOT_DATA["metrics"].get("caption_gets", 0) + 1
    save_data()


async def cb_copy_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback for captions longer than Telegram's 256-char native
    CopyTextButton limit. Sends the complete caption so the user can use
    Telegram's normal copy action without truncation."""
    query = update.callback_query
    await query.answer()
    key = (query.message.chat_id, query.message.message_id)
    entry = _caption_cache.get(key)
    caption = entry.get("caption") if entry else None
    if not caption:
        await query.message.reply_text("ℹ️ " + to_small_caps("caption cache expired."))
        return
    for i in range(0, len(caption), 4000):
        await query.message.reply_text(caption[i:i + 4000])
    uid = str(query.from_user.id)
    u = BOT_DATA["users"].setdefault(uid, {})
    u["caption_count"] = u.get("caption_count", 0) + 1
    BOT_DATA["metrics"]["caption_gets"] = BOT_DATA["metrics"].get("caption_gets", 0) + 1
    save_data()


async def cb_remove_reel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove only the caption and buttons; never delete the reel itself."""
    query = update.callback_query
    await query.answer(to_small_caps("Removed"))
    key = (query.message.chat_id, query.message.message_id)
    _caption_cache.pop(key, None)

    # The media message must remain untouched. Telegram lets us edit the
    # caption/reply markup of the delivered media message independently.
    try:
        await query.message.edit_caption(caption=None, reply_markup=None)
        return
    except Exception:
        pass
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


async def cb_get_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """v6 — 🎵 Audio button under a delivered reel. The video file is
    already deleted by the time this is tapped (cleaned up right after
    sending), so this re-downloads the source via yt-dlp and then extracts
    just the audio track.

    FIX — this used to hand the download straight to yt-dlp's
    FFmpegExtractAudio postprocessor, which needs `ffprobe` (not just
    `ffmpeg`) to inspect the file first. On a server that only has the
    portable `imageio-ffmpeg` binary (ffmpeg only, no ffprobe) that always
    failed with "unable to obtain file audio codec with ffprobe" — even
    though ffmpeg itself was perfectly available and capable of the job.
    We now download the plain video (same as the reel download, no
    postprocessor) and run ffmpeg directly to strip out the audio track,
    which never touches ffprobe at all.
    """
    query = update.callback_query
    await query.answer()
    key = (query.message.chat_id, query.message.message_id)
    entry = _caption_cache.get(key)
    url = entry.get("url") if entry else None
    if not url:
        await query.message.reply_text("ℹ️ " + to_small_caps("couldn't find the source link for this post (cache expired)."))
        return

    status_msg = await query.message.reply_text("🎵 " + to_small_caps("extracting audio..."))

    # FIX — "ffmpeg audio extraction failed ... Output file does not
    # contain any stream": the old selector "best[vcodec!=none][acodec!=
    # none]/best" falls back to plain "best" whenever Instagram serves this
    # post as separate video/audio DASH tracks instead of one muxed file.
    # "best" alone then happily grabs the highest-resolution VIDEO-ONLY
    # track (exactly the 1440x2560 vp09 stream seen in the error) — which
    # has no audio at all, so ffmpeg's "-vn" strip has nothing left to
    # write out. Since this button only ever needs the audio, ask yt-dlp
    # for an audio-only format first; only fall back toward muxed/video
    # formats if a post genuinely has no separate audio track.
    # FIX v2 — the acodec-metadata check added after the last fix was
    # itself wrong: Instagram's yt-dlp extractor frequently reports
    # acodec == "none" on formats that DO contain audio (it can't always
    # read real codec info from Instagram's API), so trusting that field
    # produced false "no audio track" errors on posts that were fine.
    # Metadata here just isn't trustworthy either way — so instead of
    # guessing from it, we download a candidate format and ask ffmpeg
    # itself (ground truth, no ffprobe needed) whether the actual file has
    # an audio stream. If not, we try the next candidate format instead of
    # giving up on the first guess.
    def probe_has_audio(path: str) -> bool:
        try:
            result = subprocess.run(
                [FFMPEG_PATH, "-i", path], capture_output=True, text=True, timeout=30
            )
            return bool(re.search(r"Stream #\d+:\d+.*Audio:", result.stderr))
        except Exception:
            return False

    def build_source_opts(fmt: str):
        opts = {
            "format": fmt,
            "outtmpl": os.path.join(DOWNLOAD_DIR, "%(id)s_audiosrc_" + str(int(time.time())) + "_%(format_id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
        }
        if FFMPEG_PATH:
            opts["ffmpeg_location"] = FFMPEG_PATH
        return opts

    def download_format(fmt: str):
        opts = build_source_opts(fmt)
        info = _ytdlp_extract_with_retry(opts, url, download=True)
        with yt_dlp.YoutubeDL(opts) as ydl:
            fp = ydl.prepare_filename(info)
        return fp if os.path.exists(fp) else None

    def run_source_download():
        # Try, in order: a dedicated audio-only stream; the best muxed
        # (video+audio) stream; then whatever "best" resolves to as a last
        # resort. Each candidate is verified against the real file, not
        # metadata, before we commit to it — failed candidates are cleaned
        # up immediately so we don't leave stray video-only files behind.
        candidates = ["bestaudio", "best[acodec!=none][vcodec!=none]", "best"]
        tried_paths = []
        for fmt in candidates:
            try:
                fp = download_format(fmt)
            except Exception as e:
                log.warning("audio-source download failed for format %r: %s", fmt, e)
                continue
            if not fp:
                continue
            tried_paths.append(fp)
            if probe_has_audio(fp):
                # Clean up any earlier failed attempts before returning.
                for p in tried_paths[:-1]:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
                return fp
        # Nothing worked — clean up every attempt and report clearly.
        for p in tried_paths:
            try:
                os.remove(p)
            except OSError:
                pass
        if tried_paths:
            raise RuntimeError("this post doesn't seem to have an audio track (checked multiple formats).")
        return None

    def extract_audio_ffmpeg(src_path: str) -> str:
        """Direct ffmpeg call — no ffprobe involved."""
        mp3_path = os.path.splitext(src_path)[0] + ".mp3"
        cmd = [
            FFMPEG_PATH, "-y", "-i", src_path,
            "-vn", "-acodec", "libmp3lame", "-q:a", "2",
            mp3_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if result.returncode != 0 or not os.path.exists(mp3_path):
            raise RuntimeError(f"ffmpeg audio extraction failed: {result.stderr[-500:]}")
        return mp3_path

    source_path = None
    audio_path = None
    try:
        if not FFMPEG_AVAILABLE:
            # Audio extraction (muxing out just the audio track) genuinely
            # needs ffmpeg, unlike plain video download — no safe fallback.
            await status_msg.edit_text(
                "❌ " + to_small_caps("audio extraction needs ffmpeg, which isn't available on this server.")
            )
            return
        source_path = await asyncio.to_thread(run_source_download)
        if not source_path:
            await status_msg.edit_text(USER_ERR_AUDIO_NOT_AVAILABLE, parse_mode="HTML")
            return
        audio_path = await asyncio.to_thread(extract_audio_ffmpeg, source_path)
        if not audio_path or not os.path.exists(audio_path):
            await status_msg.edit_text(USER_ERR_AUDIO_NOT_AVAILABLE, parse_mode="HTML")
            return
        protect = bool(BOT_DATA["settings"].get("lock_all_content", False))
        # Give Telegram a clean, human-readable filename/title instead of the
        # temporary yt-dlp filename full of ids and timestamps.
        audio_title = _safe_filename((entry or {}).get("uploader") or (entry or {}).get("title") or "Instagram Audio")
        filename = _safe_filename((entry or {}).get("title") or audio_title) + ".mp3"
        with open(audio_path, "rb") as aud:
            await query.message.reply_audio(
                audio=aud, protect_content=protect, filename=filename,
                title=audio_title, performer="Instagram"
            )
        uid = str(query.from_user.id)
        u = BOT_DATA["users"].setdefault(uid, {})
        u["audio_count"] = u.get("audio_count", 0) + 1
        BOT_DATA["metrics"]["audio_gets"] = BOT_DATA["metrics"].get("audio_gets", 0) + 1
        save_data()
        await status_msg.delete()
    except Exception as e:
        # Full technical error is useful for debugging, but must never be
        # shown to users. Keep it in the server/admin log only.
        log.exception("Audio extraction failed for %s", url)
        log_error("audio", f"url={url} err={e}")
        try:
            await status_msg.edit_text(USER_ERR_AUDIO_NOT_AVAILABLE, parse_mode="HTML")
        except Exception:
            try:
                await query.message.reply_text(USER_ERR_AUDIO_NOT_AVAILABLE, parse_mode="HTML")
            except Exception:
                pass
    finally:
        for p in (source_path, audio_path):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


async def cb_check_force_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    ok = await is_force_join_ok(context, update.effective_user.id)
    if ok:
        await query.answer(to_small_caps("✅ verified!"), show_alert=True)
        try:
            await query.message.delete()
        except Exception:
            pass
        # Both gates are clear now — land the user in the start menu (this
        # covers the onboarding path; if they were already past onboarding
        # and just got re-blocked later, show_post_onboarding is a no-op
        # past the language-picker/reply-keyboard first-run bits and just
        # re-renders start, which is fine here).
        uid = str(update.effective_user.id)
        await show_post_onboarding(context, query.message.chat_id, uid)
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
        styled_button("✅ Close Ticket", callback_data=f"tk_close:{ticket['id']}"),
        styled_button("🔁 Reopen", callback_data=f"tk_reopen:{ticket['id']}"),
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
    # v10 — same confirmation-to-admin fix as the one-shot support flow above.
    try:
        await context.bot.copy_message(
            chat_id=ticket["user_id"], from_chat_id=update.effective_chat.id, message_id=update.message.message_id
        )
        await update.message.reply_text("✅ " + to_title_small_caps("Reply sent to user."))
    except Exception:
        await update.message.reply_text(
            "⚠️ " + to_title_small_caps("Couldn't deliver reply — the user may have blocked the bot.")
        )


def get_support_prompt_text() -> str:
    """v10 — sourced from BOT_DATA['menus']['support'] so the admin can edit
    this from Menu & UI, falling back to the original default copy."""
    menu = BOT_DATA["menus"].get("support", {})
    return menu.get("text") or DEFAULT_MENUS["support"]["text"]


async def support_button_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """One-shot support flow: user gets exactly one message to submit.
    The admin receives a mention and can reply directly to that single support
    message; there is no lingering open-ticket state.

    NOTE — this prompt is intentionally the ONLY thing sent through
    _replace_rkb_screen (the shared "rkb_latest" panel slot). The
    confirmation sent after submission is sent separately (see
    handle_user_awaiting_input) and is never tracked under that slot, so
    switching to another bottom-keyboard button later can never delete the
    user's submission confirmation. The prompt itself IS explicitly deleted
    the moment the user submits their message — see the "support_message"
    branch of handle_user_awaiting_input."""
    chat_id = update.effective_chat.id
    context.user_data["awaiting"] = "support_message"
    await _replace_rkb_screen(context, chat_id, "support", get_support_prompt_text(), parse_mode="HTML")


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
            InlineKeyboardMarkup([[styled_button("🔁 Reopen", callback_data=f"tk_reopen:{tid}")]])
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
            InlineKeyboardMarkup([[styled_button("✅ Close Ticket", callback_data=f"tk_close:{tid}")]])
        )
    except Exception:
        pass
    await log_event(context, f"🎫 Ticket #{tid} reopened by {update.effective_user.id}")


_CARD_SEP = "──────────────────"


def _build_support_admin_card(rid: str) -> tuple:
    """Admin-facing 'New Support Request' card — sectioned layout, message
    quoted in « », live Pending/Resolved status, name is a clickable link
    to the user's profile. Reused both when the request first comes in and
    when an admin marks it resolved, so the card always reflects the live
    status."""
    req = BOT_DATA["support_requests"][rid]
    lbl = to_title_small_caps
    is_open = req["status"] == "pending"
    status_word = lbl("Pending") if is_open else lbl("Resolved")
    received = iso_to_ist_str(req["created_at"], "%d %B %Y") + " • " + iso_to_ist_str(req["created_at"], "%H:%M:%S") + " IST"
    payload = (
        "📩 " + lbl("New Support Request") + "\n\n"
        f"{_CARD_SEP}\n\n"
        "👤 " + lbl("User Information") + "\n\n"
        f"{lbl('Name')} : {req['user_mention']}\n"
        f"{lbl('Username')} : {html.escape(req['username_display'])}\n"
        f"{lbl('User Id')} : {req['user_id']}\n\n"
        "💬 " + lbl("Support Message") + "\n\n"
        f"«{html.escape(req['text'])}»\n\n"
        "🕒 " + lbl("Received At") + "\n\n"
        f"{received}\n\n"
        f"{_CARD_SEP}\n\n"
        "📌 " + f"{lbl('Status')} : {status_word}"
    )
    kb = None
    if is_open:
        kb = InlineKeyboardMarkup([[
            styled_button("✅ Mark Resolved", callback_data=f"sup_resolve:{rid}"),
        ]])
    return payload, kb


def _build_support_user_confirmation(rid: str) -> str:
    """User-facing confirmation — the exact requested copy, with the live
    status swapped between the just-submitted card and the resolved card."""
    req = BOT_DATA["support_requests"][rid]
    lbl = to_title_small_caps
    if req["status"] == "pending":
        body = (
            "✅ " + lbl("Message Submitted") + "\n\n"
            + lbl("Thank you for contacting support.") + "\n\n"
            + "📩 " + lbl("Your message has been sent to our support team.") + "\n\n"
            + lbl("We'll review it and get back to you as soon as possible.") + "\n\n"
            + "❤️ " + lbl("Thank you for your patience.")
        )
    else:
        body = (
            "✅ " + lbl("Support Request Resolved") + "\n\n"
            + lbl("Thank you for contacting support.") + "\n\n"
            + "📩 " + lbl("Your issue has been reviewed and marked as resolved.") + "\n\n"
            + lbl("Need anything else? Just send us a new message anytime.") + "\n\n"
            + "❤️ " + lbl("Thank you for your patience.")
        )
    return "<blockquote>" + body + "</blockquote>"


async def cb_support_resolve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin taps '✅ Mark Resolved' on a support card. This is what makes
    the user-facing status live instead of frozen on PENDING forever — we
    edit the SAME confirmation message in the user's chat in place, and
    edit this same admin card too, rather than sending fresh clutter."""
    query = update.callback_query
    if not is_admin(update.effective_user.id):
        await query.answer()
        return
    rid = query.data.split(":", 1)[1]
    req = BOT_DATA["support_requests"].get(rid)
    if not req:
        await query.answer("Request not found.", show_alert=True)
        return
    if req["status"] != "pending":
        await query.answer("Already resolved.")
        return

    req["status"] = "resolved"
    req["resolved_at"] = datetime.utcnow().isoformat()
    req["resolved_by"] = update.effective_user.id
    save_data()

    # Live-update the user's own confirmation message: PENDING -> SOLVED.
    try:
        await context.bot.edit_message_text(
            chat_id=req["confirm_chat_id"],
            message_id=req["confirm_message_id"],
            text=_build_support_user_confirmation(rid),
            parse_mode="HTML",
        )
    except Exception:
        # Message may have been deleted by the user / too old to edit —
        # fall back to a fresh "resolved" message in the same style.
        try:
            msg = await context.bot.send_message(
                chat_id=req["confirm_chat_id"], text=_build_support_user_confirmation(rid), parse_mode="HTML"
            )
            req["confirm_chat_id"] = msg.chat_id
            req["confirm_message_id"] = msg.message_id
            save_data()
        except Exception:
            pass

    # Live-update this admin card too (Pending -> Resolved, button removed).
    card_text, card_kb = _build_support_admin_card(rid)
    try:
        await query.edit_message_text(card_text, parse_mode="HTML", reply_markup=card_kb)
    except Exception:
        pass

    await query.answer("✅ Marked resolved.")
    await log_event(context, f"🎧 Support request #{rid} resolved by {update.effective_user.id}")


# ----------------------------------------------------------------------------
# v2 §3 — My usage
# ----------------------------------------------------------------------------

async def show_usage_screen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """📊 Stats screen. Premium status/expiry are hidden unless a Premium
    plan has actually been assigned to this user from the Admin Panel."""
    await _clear_ephemeral(context, update.effective_chat.id)
    user = update.effective_user
    uid = str(user.id)
    u = BOT_DATA["users"].setdefault(uid, {})
    today = datetime.utcnow().strftime("%Y-%m-%d")
    used = u.get("downloads_today", 0) if u.get("downloads_today_date") == today else 0
    assigned_premium = u.get("plan", "Free") != "Free"
    active = is_premium_active(uid)
    limit = BOT_DATA["settings"].get("daily_limit", 20)
    username = f"@{user.username}" if user.username else "Not set"
    limit_text = "Unlimited" if assigned_premium else str(limit)
    remaining = "Unlimited" if assigned_premium else str(max(0, limit - used))

    lines = [
        to_title_small_caps("My Usage"), "",
        f"Nᴀᴍᴇ : {html.escape(user.full_name)}",
        f"Uꜱᴇʀɴᴀᴍᴇ : {html.escape(username)}",
        f"Uꜱᴇʀ Iᴅ : {user.id}",
        f"Rᴇᴇʟs Dᴏᴡɴʟᴏᴀᴅᴇᴅ : {u.get('reels_count', 0)}",
        f"Aᴜᴅɪᴏs Gᴇᴛ : {u.get('audio_count', 0)}",
        f"Cᴀᴘᴛɪᴏɴs Gᴇᴛ : {u.get('caption_count', 0)}",
        f"Lɪᴍɪᴛ : {limit_text}",
        f"Uꜱᴇᴅ : {used}",
        f"Rᴇᴍᴀɪɴɪɴɢ : {remaining}",
    ]
    if assigned_premium:
        expiry = u.get("plan_expires_at")
        if expiry:
            try:
                expiry_text = to_ist(datetime.fromisoformat(expiry)).strftime("%d %b %Y, %I:%M %p") + " IST"
            except Exception:
                expiry_text = expiry
        else:
            expiry_text = "No expiry"
        lines += ["", f"💎 Pʀᴇᴍɪᴜᴍ Sᴛᴀᴛᴜꜱ : {'Active' if active else 'Expired'}", f"Eхᴘɪʀʏ : {html.escape(expiry_text)}"]

    text = "<blockquote>" + "\n".join(lines) + "</blockquote>"
    await _replace_rkb_screen(context, update.effective_chat.id, "usage", text, reply_markup=None, parse_mode="HTML")


# ----------------------------------------------------------------------------
# v2 §5 — Developer button
# ----------------------------------------------------------------------------

def _normalize_username_link(value: str) -> str:
    value = value.strip()
    if value.startswith("http://") or value.startswith("https://") or value.startswith("tg://"):
        return value
    return f"https://t.me/{value.lstrip('@')}"


async def resolve_share_url(context: ContextTypes.DEFAULT_TYPE) -> str:
    """v5 — where the 'My Usage' Share button points. Admin can set a
    custom link (e.g. a channel/landing page) in Admin Panel > Leaderboard
    & Sharing; otherwise it falls back to the bot's own t.me link."""
    url = BOT_DATA["settings"].get("share_url")
    if url:
        return url
    try:
        me = await context.bot.get_me()
        return f"https://t.me/{me.username}"
    except Exception:
        return "https://t.me/"


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
        await context.bot.send_message(update.effective_chat.id, to_small_caps("developer contact not set up yet."))
        return
    kb = InlineKeyboardMarkup([[styled_button("👨‍💻 " + to_small_caps("message developer"), url=url)]])
    menu = BOT_DATA["menus"].get("developer", {})
    banner = menu.get("text") or DEFAULT_MENUS["developer"]["text"]
    await _replace_rkb_screen(
        context, update.effective_chat.id, "developer",
        banner, reply_markup=kb, parse_mode=menu.get("parse_mode"),
    )


# ----------------------------------------------------------------------------
# v2 §4 — Gift flow (Telegram Stars + UPI)
# ----------------------------------------------------------------------------

async def show_gift_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """v4 — pure voluntary support/tip flow. This is separate from Premium
    Plans (those live under Usage now) — this is just 'send a gift to
    support the bot', any amount, no plan attached.
    v5 — the top-donor leaderboard now lives right inside this screen
    (not a separate top-level menu button), and only when the admin has
    turned it on in the Admin Panel.
    v6 — redesigned copy/buttons for a cleaner, more premium feel. Amount
    selection logic (Stars fixed buttons, UPI free-text entry) is left
    exactly as it already works — only the wording/labels changed here."""
    await _clear_ephemeral(context, update.effective_chat.id)
    kb_rows = [[styled_button("⭐ Support With Stars", callback_data="gift_stars", style="success")]]
    if BOT_DATA["settings"].get("upi_id"):
        kb_rows.append([styled_button("💳 Support Via UPI", callback_data="gift_upi", style="primary")])

    # v10 — banner text now comes from BOT_DATA["menus"]["gift"] so the
    # admin can edit it from Menu & UI like any other menu, instead of it
    # being hardcoded here. The Stars/UPI buttons above stay code-driven
    # since they carry real payment logic.
    menu = BOT_DATA["menus"].get("gift", {})
    text = menu.get("text") or DEFAULT_MENUS["gift"]["text"]

    if BOT_DATA["settings"].get("leaderboard_enabled"):
        text += "\n\n" + build_leaderboard_text(limit=3)
        kb_rows.append([styled_button("🏆 Full Leaderboard", callback_data="view_leaderboard")])
    await _replace_rkb_screen(
        context, update.effective_chat.id, "gift", text, reply_markup=InlineKeyboardMarkup(kb_rows),
        parse_mode=menu.get("parse_mode") or "HTML",
    )


async def cb_gift_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await show_gift_menu(update, context)


async def cb_view_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not BOT_DATA["settings"].get("leaderboard_enabled"):
        return
    await query.message.reply_text(build_leaderboard_text())


async def cb_gift_stars(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    # FIX — amounts changed from 10/50/100 to 50/100/500 per request; the
    # "Another amount" / "Dismiss" row below is left exactly as it was.
    kb = InlineKeyboardMarkup([
        [
            styled_button("⭐ 50", callback_data="gift_stars_amt:50", style="success"),
            styled_button("⭐ 100", callback_data="gift_stars_amt:100", style="success"),
            styled_button("⭐ 500", callback_data="gift_stars_amt:500", style="success"),
        ],
        [
            styled_button("➕ Another Amount", callback_data="gift_stars_custom", style="primary"),
            styled_button("🗑 Dismiss", callback_data="gift_dismiss", style="danger"),
        ],
    ])
    msg = await query.message.reply_text("⭐ " + to_title_small_caps("Choose an amount:"), reply_markup=kb)
    _track_ephemeral(context, msg)


async def cb_gift_dismiss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.message.delete()
    except Exception:
        pass


async def cb_gift_stars_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["awaiting"] = "gift_stars_custom_amount"
    msg = await query.message.reply_text("✏️ " + to_small_caps("enter a numeric star amount (e.g. 150)."))
    _track_ephemeral(context, msg)


def record_donation(uid: str, name: str, amount: float, kind: str):
    """v5 — leaderboard bookkeeping. kind is 'stars' or 'inr'. Both
    currencies are combined into one 'score' for ranking purposes (1 star
    == 1 rupee of ranking weight — simple and good enough for a fun
    leaderboard; admin can adjust the weighting later if it ever matters)."""
    if amount <= 0:
        return
    entry = BOT_DATA["donations"].setdefault(uid, {"name": name, "stars": 0, "inr": 0, "score": 0})
    entry["name"] = name or entry.get("name") or f"User {uid}"
    if kind == "stars":
        entry["stars"] = entry.get("stars", 0) + amount
    else:
        entry["inr"] = entry.get("inr", 0) + amount
    entry["score"] = entry.get("stars", 0) + entry.get("inr", 0)
    save_data()


def build_leaderboard_text(limit: int = 10) -> str:
    """Ranked list of top supporters, medal-styled for the top 3, with a
    warm note at the bottom so donating actually feels good to see — not
    just a bare list of numbers."""
    donors = [d for d in BOT_DATA.get("donations", {}).values() if d.get("score", 0) > 0]
    donors.sort(key=lambda d: d.get("score", 0), reverse=True)
    donors = donors[:limit]
    if not donors:
        return (
            "🏆 " + to_small_caps("top supporters") + "\n\n"
            + to_small_caps("no donations yet — be the first to make the list!")
        )
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 " + to_small_caps("top supporters"), to_small_caps("our amazing community, ranked"), ""]
    for i, d in enumerate(donors):
        rank = medals[i] if i < 3 else f"{i + 1}."
        bits = []
        if d.get("stars"):
            bits.append(f"{int(d['stars'])}⭐")
        if d.get("inr"):
            bits.append(f"₹{int(d['inr'])}")
        lines.append(f"{rank} {d.get('name', 'Anonymous')} — {' + '.join(bits)}")
    lines.append("")
    lines.append(to_small_caps("every gift helps keep this bot alive — thank you for the love! 💛"))
    return "\n".join(lines)


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
    title = to_title_small_caps(f"{plan_name} — Premium ⭐") if plan_name else to_title_small_caps("Gift The Developer ⭐")
    desc = to_title_small_caps(f"Unlock {plan_name}.") if plan_name else to_title_small_caps(f"Send {amount} Telegram Stars as a gift.")
    return await context.bot.send_invoice(
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
    # FIX — "chat ekdum clear rakhna hai": this invoice is part of the
    # optional support/tip flow (no plan attached), so it's tracked and
    # gets swept away automatically the moment another menu is opened.
    msg = await send_stars_invoice(context, query.message.chat_id, amount)
    _track_ephemeral(context, msg)


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
        # FIX — a voluntary support gift (no plan attached) must NOT auto-
        # grant Premium; that made zero sense to a user paying ₹1/1 star as
        # a tip and getting "premium added for 30 days" back. It's now a
        # pure thank-you, nothing unlocked.
        # FIX — leaderboard must ONLY count real 🎁 Send Gift donations, not
        # subscription/plan purchases, so record_donation() moved here.
        record_donation(uid, update.effective_user.full_name, sp.total_amount, "stars")
        text = (
            "🎉 " + to_small_caps("thank you so much for the support!") + "\n\n"
            f"💫 {to_small_caps('you sent')}: {sp.total_amount} ⭐\n\n"
            + to_small_caps("it really means a lot — thank you! ❤️")
        )
        log_line = f"⭐ Gift received — {sp.total_amount} stars from {update.effective_user.id}"
    await update.message.reply_text(text)
    await log_event(context, log_line)
    # FIX — "stars bhejega to kaise pata chalega": Stars payments settle
    # instantly through Telegram's own payment system (no manual admin
    # verification possible or needed), but the admin still had zero way to
    # know it happened unless a logger channel was configured. Every star
    # payment now also DMs every admin directly, guaranteed, regardless of
    # logger-channel setup.
    await dm_all_admins(context, "💰 " + log_line)


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
    kb = InlineKeyboardMarkup([[styled_button("✅ Done", callback_data=f"gift_upi_paid:{oid}", style="success")]])
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
            reply_markup=InlineKeyboardMarkup([[styled_button("✅ Done", callback_data=f"gift_upi_paid:{oid}", style="success")]]),
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
    plan = find_premium_plan(order.get("plan_id")) if order.get("plan_id") else None
    # FIX — "jab i've paid click kare tab QR expire ho jaye": the QR photo
    # message is now deleted immediately on tap instead of sitting there
    # indefinitely with a "marked as paid" note stacked below it.
    try:
        await query.message.delete()
    except Exception:
        try:
            await query.edit_message_reply_markup(None)
        except Exception:
            pass
    # FIX — clearer wording, same for plan purchases and gifts.
    user_text = (
        "✅ " + to_small_caps("your payment has been sent to admin.") + "\n"
        + to_small_caps("admin will verify and notify you shortly.")
    )
    await context.bot.send_message(chat_id=query.message.chat_id, text=user_text)
    targets = BOT_DATA.get("admins", [])
    kind = f"Plan purchase ({plan['name']})" if plan else "Support gift"
    # FIX — admin gets a real decision, not just a one-way "confirm":
    # Approve/Decline for a plan purchase (unlocks or refuses the plan),
    # Received/Not Received for a free-amount gift (records or ignores it).
    approve_label = "✅ Approve Payment" if plan else "✅ Received"
    decline_label = "❌ Decline Payment" if plan else "❌ Not Received"
    admin_kb = InlineKeyboardMarkup([[
        styled_button(approve_label, callback_data=f"gift_upi_confirm:{oid}", style="success"),
        styled_button(decline_label, callback_data=f"gift_upi_decline:{oid}", style="danger"),
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
    """Admin taps Approve (plan order) or Received (free-amount gift)."""
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
    donor_name = BOT_DATA["users"].get(uid, {}).get("name") or f"User {uid}"
    plan = find_premium_plan(order.get("plan_id")) if order.get("plan_id") else None
    if plan:
        days = plan.get("days", 30)
        grant_premium(uid, days)
        user_text = (
            "✅ " + to_small_caps("payment approved!") + "\n\n"
            f"💎 {to_small_caps('plan')}: {plan['name']}\n"
            f"💳 {to_small_caps('paid')}: ₹{order['amount']}\n"
            f"⏳ {to_small_caps('premium unlocked for')} {days} {to_small_caps('days')}"
        )
        log_line = f"💳 UPI order #{oid} approved by admin {update.effective_user.id} ({plan['name']}), premium granted"
        admin_ack = f"✅ Order #{oid} approved, plan unlocked for the user."
    else:
        # FIX — a free-amount gift/support payment does NOT unlock any
        # premium plan on its own; "Received" just confirms the money
        # arrived and says thanks. If the admin wants to reward it, that's
        # a separate, explicit action (e.g. gifting a plan manually).
        # FIX — leaderboard must ONLY count real 🎁 Send Gift donations, not
        # subscription/plan purchases, so record_donation() moved here.
        record_donation(uid, donor_name, order["amount"], "inr")
        user_text = (
            "🎉 " + to_small_caps("thank you so much for the support!") + "\n\n"
            f"💫 {to_small_caps('you sent')}: ₹{order['amount']}\n\n"
            + to_small_caps("it really means a lot — thank you! ❤️")
        )
        log_line = f"💳 UPI gift #{oid} marked received by admin {update.effective_user.id}"
        admin_ack = f"✅ Order #{oid} marked received."
    try:
        await context.bot.send_message(order["user_id"], user_text)
    except Exception:
        pass
    try:
        await query.edit_message_reply_markup(None)
        await query.message.reply_text(admin_ack)
    except Exception:
        pass
    await log_event(context, log_line)


async def cb_gift_upi_decline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """FIX — admin now has a real 'no' option: Decline Payment (plan order)
    or Not Received (free-amount gift). Nothing is granted, no donation is
    recorded, and the user is told clearly instead of being left hanging."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    oid = query.data.split(":", 1)[1]
    order = BOT_DATA["gift_orders"].get(oid)
    if not order or order["status"] in ("paid", "declined"):
        await query.answer("Already handled or not found.", show_alert=True)
        return
    order["status"] = "declined"
    save_data()
    plan = find_premium_plan(order.get("plan_id")) if order.get("plan_id") else None
    user_text = (
        "❌ " + to_small_caps("your payment could not be verified.") + "\n"
        + to_small_caps("if you believe this is a mistake, please contact support.")
    )
    log_line = (
        f"💳 UPI order #{oid} declined by admin {update.effective_user.id}"
        + (f" ({plan['name']})" if plan else " (gift)")
    )
    try:
        await context.bot.send_message(order["user_id"], user_text)
    except Exception:
        pass
    try:
        await query.edit_message_reply_markup(None)
        await query.message.reply_text(f"❌ Order #{oid} declined.")
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
    await query.message.reply_text(get_support_prompt_text(), parse_mode="HTML")


async def handle_user_awaiting_input(update: Update, context: ContextTypes.DEFAULT_TYPE, awaiting: str):
    text = (update.message.text or "").strip()
    user_obj = update.effective_user

    if awaiting == "support_message":
        # NOTE — pop "awaiting" first thing, unconditionally, so a support
        # request can never accidentally re-trigger on a later message even
        # if something below raises.
        context.user_data.pop("awaiting", None)

        # The "please type your message below" prompt is only ever needed
        # up until this exact moment — the user just submitted it. Delete
        # it now so the chat doesn't keep showing a now-stale instruction
        # sitting above the confirmation. It's the same "rkb_latest" panel
        # message _replace_rkb_screen tracked when the prompt was shown.
        _prompt_key = f"rkb_latest:{update.effective_chat.id}"
        _prompt_msg_id = BOT_DATA.get("panel_msg", {}).pop(_prompt_key, None)
        if _prompt_msg_id:
            try:
                await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=_prompt_msg_id)
            except Exception:
                pass
            save_data()

        rid = str(BOT_DATA["next_support_id"])
        BOT_DATA["next_support_id"] += 1
        created_at = datetime.utcnow().isoformat()
        req = {
            "user_id": user_obj.id,
            "user_mention": clickable_user(user_obj),
            "username_display": f"@{user_obj.username}" if user_obj.username else "No Username",
            "text": text,
            "status": "pending",
            "created_at": created_at,
            "resolved_at": None,
            "resolved_by": None,
            "confirm_chat_id": update.effective_chat.id,
            "confirm_message_id": None,
            "admin_message_ids": [],
        }
        BOT_DATA["support_requests"][rid] = req

        # Notify admins / the configured support chat with the live card.
        card_text, card_kb = _build_support_admin_card(rid)
        support_chat_id = BOT_DATA["settings"].get("support_chat_id")
        targets = [support_chat_id] if support_chat_id else BOT_DATA.get("admins", [])
        for target in targets:
            if not target:
                continue
            try:
                sent = await context.bot.send_message(
                    chat_id=target, text=card_text, parse_mode="HTML", reply_markup=card_kb
                )
                # Replying to this message still routes an admin's reply
                # straight to the user (unchanged), AND it's linked back to
                # this request for the live-status "Mark Resolved" button.
                BOT_DATA.setdefault("support_msg_map", {})[str(sent.message_id)] = str(user_obj.id)
                BOT_DATA.setdefault("support_admin_msg_map", {})[str(sent.message_id)] = rid
                req["admin_message_ids"].append(sent.message_id)
            except Exception:
                pass
        save_data()
        await log_event(context, f"🆘 Support request #{rid} from {user_obj.id}")

        # v11 — typing animation removed here per request (it was showing
        # raw <blockquote> markup mid-reveal on some clients since
        # intermediate frames are sent unparsed by design). Confirmation
        # now sends instantly, fully HTML-parsed, no animation.
        confirm_msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=_build_support_user_confirmation(rid),
            parse_mode="HTML",
        )
        if confirm_msg:
            req["confirm_message_id"] = confirm_msg.message_id
            req["confirm_chat_id"] = confirm_msg.chat_id
            save_data()

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
        msg = await send_stars_invoice(context, update.effective_chat.id, int(text))
        _track_ephemeral(context, msg)

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
            kb_rows.append([styled_button("🚫 Block This Link", callback_data=f"adm_block_link:{report['id']}")])
        if domain:
            kb_rows.append([styled_button(f"🚫 Block Domain ({domain})", callback_data=f"adm_block_domain:{report['id']}")])
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

def get_admin_panel_title() -> str:
    """v10 — sourced from BOT_DATA['menus']['admin'] so the admin can edit
    this banner from Menu & UI too, falling back to the original default."""
    menu = BOT_DATA["menus"].get("admin", {})
    return menu.get("text") or DEFAULT_MENUS["admin"]["text"]


def admin_panel_keyboard():
    # v6 — 2-per-row grid layout: every top-level Admin Panel entry is now
    # arranged two buttons per row instead of one, so the whole panel fits
    # on screen with far less scrolling, and related tools sit side by
    # side for a cleaner, more predictable control flow. Grouped by
    # purpose: money & plans, growth & sharing, people-facing ops,
    # communication tools, content/config, then diagnostics.
    # UPI Settings now lives inside 💎 Premium (it's payment config for
    # premium plans, so it belongs with them, not as its own top-level
    # button) and 🕵️ Live User Feed is paired with 👥 Users & Groups since
    # both are user-monitoring tools — nothing sits alone on its own row.
    # v10 — per request, every Admin Panel button is colorless/neutral
    # (no `style`) — colors were only wanted on the user-facing bottom
    # keyboard, not inside the admin tools. Category emojis are kept so
    # buttons stay easy to tell apart at a glance without relying on color.
    return InlineKeyboardMarkup(
        [
            [styled_button("💎 Premium", callback_data="adm_premium"),
             styled_button("🏆 Leaderboard", callback_data="adm_leaderboard")],
            [styled_button("📤 Share Settings", callback_data="adm_share"),
             styled_button("📢 Broadcast", callback_data="adm_broadcast")],
            [styled_button("👨‍💻 Developer Settings", callback_data="adm_devsettings"),
             styled_button("🎧 Support Settings", callback_data="adm_support_settings")],
            [styled_button("🎫 Tickets", callback_data="adm_tickets"),
             styled_button("📊 Statistics", callback_data="adm_stats")],
            [styled_button("👥 Users & Groups", callback_data="adm_users"),
             styled_button("🕵️ Live User Feed", callback_data="adm_live")],
            [styled_button("🎨 Menu & UI", callback_data="adm_menu_ui"),
             styled_button("🧪 Test Commands", callback_data="adm_cmdtest")],
            [styled_button("🔔 Notifications", callback_data="adm_notifications"),
             styled_button("📜 Activity Log", callback_data="adm_activity")],
            [styled_button("🩺 Self-Test", callback_data="adm_selftest"),
             styled_button("🧩 Feature Plugins", callback_data="adm_plugins")],
            [styled_button("⚙️ Settings", callback_data="adm_settings"),
             styled_button("☠️ Danger Zone", callback_data="adm_danger")],
        ]
    )


def back_row(cb="adm_back", label="🔙 Back"):
    # v10 — colorless, same as every other Admin Panel button.
    return [styled_button(label, callback_data=cb)]


def home_row():
    """#3 — extra row shown only on top-level category screens, alongside
    the regular (stack-aware) 🔙 Back row.
    v10 — colorless, same as every other Admin Panel button."""
    return [styled_button("🏠 Admin Home", callback_data="adm_home")]


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await _clear_ephemeral(context, update.effective_chat.id)
    context.user_data["adm_nav_stack"] = ["adm_home"]
    sent = await update.message.reply_text(get_admin_panel_title(), reply_markup=admin_panel_keyboard())
    await track_and_refresh_panel(context, update.effective_chat.id, "admin", sent)
    await delete_incoming(update)


async def _render_adm_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text(get_admin_panel_title(), reply_markup=admin_panel_keyboard())


async def cb_adm_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["adm_nav_stack"] = ["adm_home"]
    await _render_adm_home(update, context)


# ---- v3 §10 — activity log + self-test ---------------------------------------

async def _render_adm_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    entries = BOT_DATA.get("error_log", [])[-10:][::-1]
    if not entries:
        body = "✅ " + to_small_caps("activity log") + "\n\n" + to_small_caps("all clear — nothing to report.")
    else:
        # Group consecutive display by kind so repeats of the same issue
        # (e.g. force-join misconfigured) don't push everything else off
        # screen, and each entry explains what happened, why, and how to
        # fix it — not just a raw exception string.
        blocks = []
        fix_rows = []
        for n, e in enumerate(entries, 1):
            kind = e.get("kind", "unhandled")
            label, why, fix = ERROR_KIND_INFO.get(kind, ERROR_KIND_INFO["unhandled"])
            when = iso_to_ist_str(e.get("time"), "%Y-%m-%d %H:%M")
            detail = e.get("detail") or e.get("error") or ""
            blocks.append(
                f"{n}. {label}  •  {when} IST\n"
                f"What: {why}\n"
                f"Fix: {fix}\n"
                f"Detail: {detail[:180]}"
            )
            if e.get("id") is not None:
                fix_rows.append([styled_button(
                    f"🛠 Fix Now #{n} — {label}", callback_data=f"adm_fix:{e['id']}"
                )])
        body = (
            "📋 " + to_small_caps("activity log") + f" — {to_small_caps('last')} {len(entries)}\n\n"
            + "\n\n".join(blocks)
        )
    kb_rows = list(fix_rows) if entries else []
    kb_rows.append([styled_button("🗑 Clear Log", callback_data="adm_clear_activity")])
    kb_rows.append(back_row())
    kb_rows.append(home_row())
    kb = InlineKeyboardMarkup(kb_rows)
    await query.edit_message_text(body, reply_markup=kb)


async def cb_adm_fix_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🛠 Fix Now — attempts a real, automatic remediation for the error
    kinds that actually have one (re-checks force-join channels live,
    forces a fresh MongoDB reconnect attempt). For kinds that have no code-
    level fix (a duplicate instance, a one-off download failure, a bug that
    needs a real code change), it explains exactly why and what a human
    needs to do instead — it never just claims success."""
    query = update.callback_query
    if not is_admin(update.effective_user.id):
        await query.answer()
        return
    try:
        err_id = int(query.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await query.answer("⚠️ " + to_small_caps("bad fix reference."), show_alert=True)
        return
    entry = next((e for e in BOT_DATA.get("error_log", []) if e.get("id") == err_id), None)
    if entry is None:
        await query.answer("⚠️ " + to_small_caps("that log entry is gone — log was cleared."), show_alert=True)
        return
    kind = entry.get("kind", "unhandled")
    await query.answer("🛠 " + to_small_caps("running fix..."))

    if kind == "force_join":
        targets = _force_join_targets()
        if not targets:
            result = "ℹ️ " + to_small_caps("no force-join channel is configured — nothing to check.")
        else:
            lines = ["🔎 " + to_small_caps("re-checking force-join channel(s) now:")]
            for t in targets:
                chat_id = t.get("chat_id")
                if not chat_id:
                    lines.append(f"• {t.get('link')} — " + to_small_caps("link-only target, can't be auto-verified."))
                    continue
                try:
                    me = await context.bot.get_me()
                    member = await context.bot.get_chat_member(chat_id=chat_id, user_id=me.id)
                    if member.status in ("administrator", "creator"):
                        lines.append(f"• {chat_id} — ✅ " + to_small_caps("bot is admin here, looks correctly configured."))
                    else:
                        lines.append(f"• {chat_id} — ⚠️ " + to_small_caps(f"bot can see this chat but is only '{member.status}' — make it admin."))
                except Exception as e:
                    lines.append(f"• {chat_id} — ❌ " + to_small_caps(f"still failing: {e}"))
            result = "\n".join(lines)

    elif kind == "mongo":
        global _mongo_collection
        _mongo_collection = None  # force a fresh connection attempt
        col = get_mongo_collection()
        if col is not None:
            result = "✅ " + to_small_caps("reconnected to mongodb successfully.")
        else:
            result = "❌ " + to_small_caps(f"still can't connect — {_mongo_last_error or 'unknown error'}")

    elif kind == "conflict":
        result = (
            "ℹ️ " + to_small_caps("this can't be fixed from inside this process.") + "\n"
            + to_small_caps("make sure no other copy of this bot (old deploy, second terminal, another server) is running with the same bot token, then stop it.")
        )

    else:
        result = "ℹ️ " + to_small_caps(f"'{kind}' has no automatic fix — see the fix hint above for the manual step.")

    try:
        await query.message.reply_text(result)
    except Exception:
        pass


async def cb_adm_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    await _render_adm_activity(update, context)


async def cb_adm_clear_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    BOT_DATA["error_log"] = []
    save_data()
    await _render_adm_activity(update, context)


async def _render_adm_selftest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.edit_message_text("🧪 " + to_small_caps("running self-test..."))
    results = []
    results.append(("Bot token", "✅ OK" if BOT_TOKEN else "❌ missing"))
    try:
        me = await context.bot.get_me()
        results.append(("Telegram API", f"✅ OK (@{me.username})"))
    except Exception as e:
        results.append(("Telegram API", f"❌ {e}"))
    results.append(("ffmpeg", "✅ found" if FFMPEG_AVAILABLE else "⚠️ not found (merge downloads may fail)"))
    results.append(("ffprobe", "✅ found" if FFPROBE_AVAILABLE else "ℹ️ not found (not needed — audio uses direct ffmpeg)"))
    try:
        import yt_dlp as _yd
        results.append(("yt-dlp", f"✅ v{_yd.version.__version__}"))
    except Exception as e:
        results.append(("yt-dlp", f"❌ {e}"))
    results.append(("Download dir", "✅ writable" if os.access(DOWNLOAD_DIR, os.W_OK) else "❌ not writable"))
    body = "🧪 " + to_small_caps("self-test results") + "\n\n" + "\n".join(f"{k}: {v}" for k, v in results)
    await query.edit_message_text(body, reply_markup=InlineKeyboardMarkup([back_row(), home_row()]))


async def cb_adm_selftest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    await _render_adm_selftest(update, context)


# ---- 🧪 Test Commands — run/check every bot command straight from the panel --
# One small screen, one button per command the bot supports. Tapping a
# button runs that command's real, safe logic and drops the actual result
# right into this chat — no need to leave the admin panel or type anything
# to verify a command still works. Commands that need an argument (like
# /block <id>) show their usage instead of guessing one; owner-only
# commands only actually run for the owner.
COMMAND_TEST_LIST = [
    ("start", "🚀 /start"),
    ("help", "❓ /help"),
    ("language", "🌐 /language"),
    ("admin", "🛠 /admin"),
    ("ping", "🏓 /ping"),
    ("health", "🩺 /health"),
    ("dbstatus", "🗄 /dbstatus"),
    ("database", "💾 /database"),
    ("exportusers", "📤 /exportusers"),
    ("exportpdf", "📊 /exportpdf"),
    ("export", "📦 /export"),
    ("block", "🚫 /block"),
    ("unblock", "✅ /unblock"),
    ("cancel", "🧹 /cancel"),
]


def _cmdtest_kb() -> InlineKeyboardMarkup:
    rows, row = [], []
    for key, label in COMMAND_TEST_LIST:
        row.append(styled_button(label, callback_data=f"run_cmd:{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(back_row())
    rows.append(home_row())
    return InlineKeyboardMarkup(rows)


async def _render_adm_cmdtest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    body = (
        "🧪 " + to_small_caps("test commands") + "\n\n"
        + to_small_caps("tap any command below to run/check it right now — the real result is sent here, exactly like typing it.") + "\n\n"
        + to_small_caps("owner-only commands only actually run for the owner; /block and /unblock need an argument, so they just show their usage.")
    )
    await query.edit_message_text(body, reply_markup=_cmdtest_kb())


async def cb_adm_cmdtest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    await _render_adm_cmdtest(update, context)


async def cb_run_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    admin_id = update.effective_user.id
    if not is_admin(admin_id):
        await query.answer()
        return
    key = query.data.split(":", 1)[1]
    chat_id = update.effective_chat.id

    # Auto-clean the Test Commands screen: delete whatever result the last
    # tap here left behind before running a new one, so repeatedly tapping
    # through commands doesn't fill the chat with old test output.
    for old_mid in context.user_data.get("last_test_msg_ids", []):
        try:
            await context.bot.delete_message(chat_id, old_mid)
        except Exception:
            pass
    context.user_data["last_test_msg_ids"] = []

    async def send(text, **kwargs):
        sent = await context.bot.send_message(chat_id, text, **kwargs)
        context.user_data.setdefault("last_test_msg_ids", []).append(sent.message_id)
        return sent

    async def track(sent_msg):
        """For calls that send via context.bot directly (documents, etc.)
        instead of the send() helper above — still gets cleaned up next run."""
        if sent_msg is not None:
            context.user_data.setdefault("last_test_msg_ids", []).append(sent_msg.message_id)
        return sent_msg

    try:
        if key == "start":
            await query.answer("✅ " + to_small_caps("running /start..."))
            await track(await render_menu(context, chat_id, "start"))

        elif key == "help":
            await query.answer("✅ " + to_small_caps("running /help..."))
            await track(await render_menu(context, chat_id, "help_admin" if is_admin(admin_id) else "help_user"))

        elif key == "language":
            await query.answer("✅ " + to_small_caps("running /language..."))
            langs = BOT_DATA["settings"].get("languages", [])
            if not langs:
                await send(to_small_caps("no extra languages configured yet — add some in settings > languages."))
            else:
                await send("🌐 " + to_small_caps("choose a language:"), reply_markup=build_language_keyboard())

        elif key == "admin":
            await query.answer("✅ " + to_small_caps("running /admin..."))
            await send(get_admin_panel_title(), reply_markup=admin_panel_keyboard())

        elif key == "ping":
            # Same live status data/format as the real /ping command.
            await query.answer()
            t0 = time.monotonic()
            msg = await context.bot.send_message(chat_id, "🏓 " + to_title_small_caps("Pong!"))
            await track(msg)
            ms = int((time.monotonic() - t0) * 1000)
            backend = "MongoDB" if get_mongo_collection() is not None else "Local JSON File"
            lines = [
                f"↬ {to_title_small_caps('Uptime')} : {human_uptime_full()}",
                f"↬ {to_title_small_caps('Storage')} : {to_title_small_caps(backend)}",
                f"↬ {to_title_small_caps('Server Time')} : {now_ist_str('%d %b %Y, %H:%M:%S')} IST",
            ]
            await msg.edit_text(
                f"🏓 <b>{to_title_small_caps('Pong!')}</b> {ms}ms\n\n<blockquote>{html.escape(chr(10).join(lines))}</blockquote>",
                parse_mode="HTML",
            )

        elif key == "health":
            await query.answer()
            await send(build_health_text())

        elif key == "dbstatus":
            if not is_owner(admin_id):
                await query.answer("🔒 " + to_small_caps("owner only."), show_alert=True)
            else:
                await query.answer()
                col = get_mongo_collection()
                if col is not None:
                    text = "✅ MongoDB: connected"
                elif MONGO_URI:
                    text = f"❌ MongoDB: not connected\nReason: {_mongo_last_error}"
                else:
                    text = "ℹ️ MongoDB not configured — using local JSON file."
                text += f"\n\nUsers: {len(BOT_DATA['users'])} | Groups: {len(BOT_DATA['groups'])} | Admins: {len(BOT_DATA['admins'])}"
                await send(text)

        elif key == "database":
            if not is_owner(admin_id):
                await query.answer("🔒 " + to_small_caps("owner only."), show_alert=True)
            else:
                await query.answer("✅ " + to_small_caps("building backup..."))
                raw = json.dumps(BOT_DATA, ensure_ascii=False, indent=2).encode("utf-8")
                payload, encrypted = encrypt_backup_bytes(raw)
                filename = "bot_data_backup.json.enc" if encrypted else "bot_data_backup.json"
                path = os.path.join(tempfile.gettempdir(), f"bot_data_export_{int(time.time())}_{filename}")
                with open(path, "wb") as f:
                    f.write(payload)
                with open(path, "rb") as f:
                    doc = await context.bot.send_document(chat_id, document=f, filename=filename)
                await track(doc)
                os.remove(path)

        elif key == "exportusers":
            if not is_owner(admin_id):
                await query.answer("🔒 " + to_small_caps("owner only."), show_alert=True)
            else:
                await query.answer("✅ " + to_small_caps("building csv..."))
                path = os.path.join(tempfile.gettempdir(), f"users_export_{int(time.time())}.csv")
                with open(path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(["user_id", "name", "username", "joined", "last_active"])
                    for uid, info in BOT_DATA["users"].items():
                        writer.writerow([uid, info.get("name"), info.get("username"), info.get("joined"), info.get("last_active")])
                with open(path, "rb") as f:
                    doc = await context.bot.send_document(chat_id, document=f, filename="users_export.csv")
                await track(doc)
                os.remove(path)

        elif key == "exportpdf":
            if not is_owner(admin_id):
                await query.answer("🔒 " + to_small_caps("owner only."), show_alert=True)
            elif not PDF_REPORT_AVAILABLE:
                await query.answer()
                await send(to_small_caps("❌ pdf report needs matplotlib + reportlab — run `pip install matplotlib reportlab`."))
            else:
                await query.answer("✅ " + to_small_caps("building pdf report..."))
                pdf_path = await asyncio.to_thread(build_pdf_report)
                with open(pdf_path, "rb") as f:
                    doc = await context.bot.send_document(chat_id, document=f, filename="bot_report.pdf")
                await track(doc)
                os.remove(pdf_path)

        elif key == "export":
            if not is_owner(admin_id):
                await query.answer("🔒 " + to_small_caps("owner only."), show_alert=True)
            else:
                await query.answer()
                src_dir = os.path.dirname(os.path.abspath(__file__))
                zip_path = None
                try:
                    zip_path = _build_export_zip(src_dir)
                    size = os.path.getsize(zip_path)
                    if size > EXPORT_MAX_BYTES:
                        await send(
                            "⚠️ " + to_small_caps(
                                f"export would be {size / 1024 / 1024:.1f}mb — too large for telegram's ~50mb limit."
                            )
                        )
                    else:
                        await send(
                            "✅ " + to_small_caps(
                                f"export check passed — source zip builds fine at {size / 1024:.0f}kb."
                            ) + "\n" + to_small_caps("run the real /export command to actually receive the file.")
                        )
                finally:
                    if zip_path and os.path.exists(zip_path):
                        os.remove(zip_path)

        elif key == "block":
            await query.answer()
            await send(
                "ℹ️ " + to_small_caps("/block needs an argument, so it can't be run blindly from here.")
                + "\nUsage: /block <user_id | link | domain>"
            )

        elif key == "unblock":
            await query.answer()
            await send(
                "ℹ️ " + to_small_caps("/unblock needs an argument, so it can't be run blindly from here.")
                + "\nUsage: /unblock <user_id | link | domain>"
            )

        elif key == "cancel":
            await query.answer("✅ " + to_small_caps("running /cancel..."))
            for k in (
                "awaiting", "btn_flow", "style_source_text", "style_target",
                "message_target", "report_link_draft", "owner_contact_label_draft",
                "autoreply_key_draft",
            ):
                context.user_data.pop(k, None)
            await send("✅ " + to_small_caps("cancelled — any pending flow (for you) has been cleared."))

        else:
            await query.answer(to_small_caps("unknown command."), show_alert=True)
    except Exception as e:
        log.exception("cb_run_cmd failed for %s", key)
        try:
            await send("❌ " + to_small_caps(f"'{key}' check failed: {e}"))
        except Exception:
            pass


# ---- Live User Feed (anti-misuse monitoring) ---------------------------------

async def _render_adm_live(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    col = get_mongo_collection()
    backend = "MongoDB ✅ (synced)" if col is not None else "Local (in-memory/JSON, no Mongo connected)"
    entries = BOT_DATA.get("activity_log", [])[-15:][::-1]
    lines = [f"🕵️ Live User Feed\n🗄 Backend: {backend}\n👥 Total tracked users: {len(BOT_DATA['users'])}\n"]
    if not entries:
        lines.append(to_small_caps("no activity has been recorded yet."))
    else:
        for e in entries:
            uname = f"@{e['username']}" if e.get("username") else "(no username)"
            when = iso_to_ist_str(e.get("time"), "%H:%M:%S")
            lines.append(f"• {when} IST — {e.get('name')} {uname} [{e.get('user_id')}]\n  ↳ {e.get('url')}")
    body = "\n".join(lines)
    s = BOT_DATA["settings"]
    kb = InlineKeyboardMarkup(
        [
            [styled_button("🚫 Ban / Unban User (by ID)", callback_data="adm_quickban")],
            [styled_button(
                toggle_label("📡 Feed To Admin DM", s.get('user_activity_dm', True)),
                callback_data="stgl:user_activity_dm:adm_live",
            )],
            [styled_button(
                toggle_label("🆕 Detailed Join Alerts", s.get('detailed_join_alerts', True)),
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
        to_small_caps("🚫 send the user's numeric id — if already banned they will be unbanned, otherwise they will be banned.")
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
    mem = get_memory_mb()
    backend = "MongoDB" if get_mongo_collection() is not None else "Local JSON File"
    uptime = human_uptime()
    text = (
        "📊 " + to_title_small_caps("Stats & Activity") + "\n\n"
        + f"{to_title_small_caps('Users')} : {len(BOT_DATA['users'])}\n"
        + f"{to_title_small_caps('Groups')} : {len(BOT_DATA['groups'])}\n"
        + f"{to_title_small_caps('Blocked')} : {len(BOT_DATA.get('blocked_users', []))}\n"
        + f"{to_title_small_caps('Broadcasts Sent')} : {BOT_DATA['metrics'].get('broadcasts_sent', 0)}\n"
        + f"{to_title_small_caps('Reels Downloaded')} : {BOT_DATA['metrics'].get('reels_downloaded', 0)}\n"
        + f"{to_title_small_caps('/Start Count')} : {BOT_DATA['metrics'].get('start_count', 0)}\n"
        + f"{to_title_small_caps('Copyright Reports')} : {len(BOT_DATA['copyright_reports'])}\n\n"
        + f"{to_title_small_caps('Uptime')} : {uptime}\n"
        + f"{to_title_small_caps('Memory')} : {mem if mem is not None else 'N/A'} MB\n"
        + f"{to_title_small_caps('Storage')} : {to_title_small_caps(backend)}"
    )
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([back_row(), home_row()]))


async def cb_adm_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _render_adm_stats(update, context)


# ---- 🔔 Notification Center ---------------------------------------------------
# A single live "what's going on" dashboard so the admin doesn't have to open
# Tickets, Activity Log, Users & Groups, and Settings separately just to see
# whether anything needs attention right now.

async def _render_adm_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    now = datetime.utcnow()
    day_ago = now - timedelta(hours=24)

    def _parse(ts):
        try:
            return datetime.fromisoformat(ts)
        except Exception:
            return None

    # New users / groups in the last 24h.
    new_users_24h = sum(
        1 for u in BOT_DATA["users"].values()
        if (dt := _parse(u.get("joined"))) and dt >= day_ago
    )
    new_groups_24h = sum(
        1 for g in BOT_DATA["groups"].values()
        if (dt := _parse(g.get("added_at"))) and dt >= day_ago
    )
    reel_activity_24h = sum(
        1 for e in BOT_DATA.get("activity_log", [])
        if (dt := _parse(e.get("time"))) and dt >= day_ago
    )

    # Tickets / support requests still waiting on a reply.
    open_tickets = sum(1 for t in BOT_DATA.get("tickets", {}).values() if t.get("status") == "open")
    pending_support = sum(
        1 for r in BOT_DATA.get("support_requests", {}).values() if r.get("status") != "resolved"
    )

    # Errors logged in the last 24h, most recent first.
    recent_errors = [
        e for e in BOT_DATA.get("error_log", [])
        if (dt := _parse(e.get("time"))) and dt >= day_ago
    ]
    recent_errors.sort(key=lambda e: e.get("time") or "", reverse=True)
    last_error = recent_errors[0] if recent_errors else None

    maintenance_on = bool(BOT_DATA["settings"].get("maintenance"))
    blocked_count = len(BOT_DATA.get("blocked", []))

    lines = ["🔔 " + to_title_small_caps("Notification Center"), ""]

    # Needs-attention section first, so the admin sees anything urgent
    # without scrolling.
    alerts = []
    if maintenance_on:
        alerts.append("🔒 " + to_small_caps("maintenance mode is currently ON — users can't use the bot."))
    if open_tickets:
        alerts.append(f"🎫 {open_tickets} " + to_small_caps("open ticket(s) waiting for a reply."))
    if pending_support:
        alerts.append(f"🆘 {pending_support} " + to_small_caps("support request(s) still pending."))
    if recent_errors:
        alerts.append(f"⚠️ {len(recent_errors)} " + to_small_caps("error(s) logged in the last 24h."))
    if not alerts:
        alerts.append("✅ " + to_small_caps("all clear — nothing needs attention right now."))
    lines.extend(alerts)
    lines.append("")

    # Live activity snapshot.
    lines.append("📈 " + to_small_caps("last 24 hours"))
    lines.append(f"👤 " + to_small_caps("new users: ") + str(new_users_24h))
    lines.append(f"👨‍👩‍👧 " + to_small_caps("new groups: ") + str(new_groups_24h))
    lines.append(f"🎬 " + to_small_caps("reel activity: ") + str(reel_activity_24h))
    lines.append("")

    if last_error:
        kind = last_error.get("kind", "unhandled")
        label, _why, _fix = ERROR_KIND_INFO.get(kind, ERROR_KIND_INFO["unhandled"])
        when = iso_to_ist_str(last_error.get("time"), "%Y-%m-%d %H:%M")
        lines.append("🐞 " + to_small_caps("most recent error"))
        lines.append(f"{label} — {when} IST")
        lines.append("")

    lines.append("📊 " + to_small_caps("current totals"))
    lines.append(f"👥 " + to_small_caps("users: ") + str(len(BOT_DATA["users"])))
    lines.append(f"🚫 " + to_small_caps("blocked: ") + str(blocked_count))
    lines.append(f"🎬 " + to_small_caps("reels delivered: ") + str(BOT_DATA["metrics"].get("reels_downloaded", 0)))
    lines.append(f"⏱ " + to_small_caps("uptime: ") + human_uptime())

    text = "\n".join(lines)
    kb = InlineKeyboardMarkup(
        [
            [styled_button("🎫 Tickets", callback_data="adm_tickets"),
             styled_button("📜 Activity Log", callback_data="adm_activity")],
            [styled_button("🔄 Refresh", callback_data="adm_notifications")],
            back_row(),
            home_row(),
        ]
    )
    await query.edit_message_text(text, reply_markup=kb)


async def cb_adm_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _render_adm_notifications(update, context)


# ---- Users & Groups ----------------------------------------------------------

async def _render_adm_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    kb = InlineKeyboardMarkup(
        [
            [styled_button("📋 List Users (last 20)", callback_data="adm_users_list")],
            [styled_button("👨‍👩‍👧 List Groups", callback_data="adm_groups_list")],
            [styled_button("🔍 Check User", callback_data="adm_check_user")],
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
        text = to_small_caps("the bot is not in any group yet.")
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
        text = to_small_caps("no user records found yet.")
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
    await query.message.reply_text(to_small_caps("send the id of the user you want to message."))


async def cb_adm_check_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["awaiting"] = "check_user_id"
    await query.message.reply_text(to_small_caps("send the user id you want to check."))


async def build_user_details_card(context: ContextTypes.DEFAULT_TYPE, target_id: int) -> str:
    """Full live detail card for one user, shown to an admin from
    '🔍 Check User' — same sectioned house style as the New User Started
    alert / Stats screen, but pulled fresh from stored data + a live
    getChat call, and with a clickable Name."""
    lbl = to_title_small_caps
    uid = str(target_id)
    u = BOT_DATA["users"].get(uid, {})

    # Prefer a live getChat so name/username/premium reflect the user's
    # CURRENT profile, not whatever was cached at their last /start. Falls
    # back to stored data if the chat can't be fetched (user blocked the
    # bot, invalid id, never started it, etc.).
    chat_obj = None
    try:
        chat_obj = await context.bot.get_chat(target_id)
    except Exception:
        pass

    if chat_obj is not None:
        name = chat_obj.full_name if hasattr(chat_obj, "full_name") else (
            " ".join(filter(None, [getattr(chat_obj, "first_name", None), getattr(chat_obj, "last_name", None)]))
        )
        name = name or u.get("name") or str(target_id)
        username = getattr(chat_obj, "username", None) or u.get("username")
        is_premium = getattr(chat_obj, "is_premium", None)
        name_link = f'<a href="https://t.me/{username}">{html.escape(name)}</a>' if username else f'<a href="tg://user?id={target_id}">{html.escape(name)}</a>'
    else:
        name = u.get("name") or str(target_id)
        username = u.get("username")
        is_premium = None
        name_link = f'<a href="tg://user?id={target_id}">{html.escape(name)}</a>'

    if not u and chat_obj is None:
        return None  # unknown to the bot AND not resolvable live — caller shows "not found"

    username_display = f"@{username}" if username else "Not Set"
    today = datetime.utcnow().strftime("%Y-%m-%d")
    used_today = u.get("downloads_today", 0) if u.get("downloads_today_date") == today else 0
    plan = u.get("plan", "Free")
    active_premium = is_premium_active(uid)
    joined = u.get("joined_at") or u.get("started_at")
    joined_display = iso_to_ist_str(joined, "%d %B %Y • %H:%M:%S") + " IST" if joined else "Unknown"
    banned = target_id in BOT_DATA.get("blocked", [])

    lines = [
        "🔍 " + lbl("User Details"),
        "",
        _CARD_SEP,
        "",
        "👤 " + lbl("User Information"),
        "",
        f"{lbl('Name')} : {name_link}",
        f"{lbl('Username')} : {html.escape(username_display)}",
        f"{lbl('User Id')} : {target_id}",
        "",
        "📊 " + lbl("Activity"),
        "",
        f"{lbl('Reels Downloaded')} : {u.get('reels_count', 0)}",
        f"{lbl('Audios Get')} : {u.get('audio_count', 0)}",
        f"{lbl('Captions Get')} : {u.get('caption_count', 0)}",
        f"{lbl('Used Today')} : {used_today}",
        "",
        "💎 " + lbl("Premium"),
        "",
        f"{lbl('Plan')} : {lbl(plan)}",
        f"{lbl('Status')} : {lbl('Active') if active_premium else lbl('Not Active')}",
        "",
        "⚙️ " + lbl("Account"),
        "",
        f"{lbl('Joined At')} : {joined_display}",
        f"{lbl('Banned')} : {lbl('Yes') if banned else lbl('No')}",
    ]
    if is_premium is not None:
        lines.append(f"{lbl('Telegram Premium')} : {lbl('Yes') if is_premium else lbl('No')}")
    lines += ["", _CARD_SEP]

    return "<blockquote>" + "\n".join(lines) + "</blockquote>"


# ---- Broadcast (reliable delivery, admin copy, /broadcast command, and month-wise delete) ----
#
# What changed and why:
#  1. do_broadcast() used to fire every send back-to-back with zero pacing.
#     Telegram enforces a hard ~30 messages/second global rate limit; blast
#     past it and the API replies with 429 "Too Many Requests" (RetryAfter),
#     which the old code caught with a bare `except Exception` and simply
#     logged as a permanent failure. That's the "bar bar failed ho jata hai"
#     — most of those "failures" were really just flood-control hits that a
#     short pause and one retry would have delivered fine. Fixed by pacing
#     every send and giving a RetryAfter exactly one honoured retry.
#  2. Failures are now categorized (blocked the bot, invalid/deleted chat,
#     rate-limited-then-recovered, other) instead of one flat "failed"
#     number, so the admin can actually see *why* delivery didn't land.
#  3. Every successfully delivered message ID is now recorded per user
#     against the broadcast, so a broadcast can be pulled back out of every
#     recipient's chat later (see Delete Broadcast below).
BROADCAST_SEND_DELAY = 0.05  # ~20 msg/sec — safely under Telegram's cap


def _broadcast_report_text(entry: dict) -> str:
    """Build the normal broadcast completion report."""
    total = entry.get("total_targeted", 0)
    sent = entry.get("recipients", 0)
    blocked = entry.get("blocked", 0)
    invalid_chat = entry.get("invalid_chat", 0)
    other_failed = entry.get("other_failed", 0)
    recovered = entry.get("recovered", 0)
    failed_total = blocked + invalid_chat + other_failed
    return (
        "✅ " + to_small_caps("broadcast complete") + "\n\n"
        + to_small_caps("total targeted") + f": {total}\n"
        + to_small_caps("delivered successfully") + f": {sent}\n"
        + to_small_caps("not delivered") + f": {failed_total}\n\n"
        + to_small_caps("breakdown — why some were not delivered") + "\n"
        + "🚫 " + to_small_caps("blocked the bot / account deactivated") + f": {blocked}\n"
        + "⚠️ " + to_small_caps("chat unavailable / never started the bot") + f": {invalid_chat}\n"
        + "❓ " + to_small_caps("other delivery error") + f": {other_failed}\n"
        + ("🔁 " + to_small_caps("recovered after a brief rate-limit pause") + f": {recovered}\n" if recovered else "")
        + "\n🔐 " + to_small_caps("forward-lock") + f": {'✅ ON' if entry.get('protect') else '❌ OFF'}"
    )


async def cb_adm_start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show confirmation before sending the bot's normal /start experience to everyone."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    kb = InlineKeyboardMarkup([
        [styled_button("✅ Confirm /start Broadcast", callback_data="adm_start_broadcast_confirm")],
        [styled_button("❌ Cancel", callback_data="adm_broadcast")]
    ])
    await query.edit_message_text(
        "⚠️ " + to_small_caps("confirm /start broadcast") + "\n\n"
        + to_small_caps("this will send the bot's normal /start welcome message to every registered user, plus this admin chat.") + "\n"
        + to_small_caps("no custom broadcast content will be used."),
        reply_markup=kb,
    )


async def cb_adm_start_broadcast_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send the normal /start experience as a simple alive/working notification."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return

    status = await query.message.reply_text("🚀 " + to_small_caps("/start broadcast in progress..."))
    targets = set(BOT_DATA["users"].keys())
    targets.add(str(update.effective_user.id))
    sent = 0
    failed = 0

    for uid in targets:
        try:
            # Render the exact same start destination/menu users get from /start,
            # without pretending Telegram received a command from the bot.
            await context.bot.send_message(chat_id=int(uid), text="/start")
            sent += 1
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 0.5)
            try:
                await context.bot.send_message(chat_id=int(uid), text="/start")
                sent += 1
            except Exception:
                failed += 1
        except Exception:
            failed += 1
        await asyncio.sleep(BROADCAST_SEND_DELAY)

    try:
        await status.edit_text(
            "✅ " + to_small_caps("/start broadcast complete") + "\n\n"
            + to_small_caps("delivered") + f": {sent}\n"
            + to_small_caps("failed") + f": {failed}"
        )
    except Exception:
        pass
    await log_event(context, f"🚀 /start broadcast by {update.effective_user.id} — {sent} delivered, {failed} failed")


async def _render_adm_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    s = BOT_DATA["settings"]
    protect = s.get("protect_broadcasts", True)
    kb = InlineKeyboardMarkup(
        [
            [styled_button("📢 New Broadcast", callback_data="adm_bc_new"),
             styled_button("📜 Broadcast Log", callback_data="adm_bc_log")],
            [styled_button(
                toggle_label("🔐 Forward-Lock", protect),
                callback_data="stgl:protect_broadcasts:adm_broadcast",
            ),
             styled_button("🚀 /start Broadcast", callback_data="adm_start_broadcast")],
            [styled_button("🗑 Delete Broadcast", callback_data="adm_bc_delmenu")],
            back_row(),
            home_row(),
        ]
    )
    total_users = len(BOT_DATA["users"])
    body = (
        to_small_caps("📢 broadcast centre") + "\n"
        + to_small_caps("send an announcement to every registered user") + "\n\n"
        + to_small_caps("total reachable users") + f": {total_users}\n\n"
        + to_small_caps("/start broadcast sends the normal bot welcome message as an alive/working notification")
    )
    await query.edit_message_text(body, reply_markup=kb)


async def cb_adm_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _render_adm_broadcast(update, context)


async def cb_adm_bc_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["awaiting"] = "broadcast_content"
    await query.message.reply_text(
        to_small_caps("📢 send your broadcast now") + "\n"
        + to_small_caps("text, photo or video — one single message") + "\n\n"
        + to_small_caps("it will be delivered to every registered user and this admin chat, respecting your forward-lock setting")
    )


async def cb_adm_bc_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    entries = BOT_DATA["broadcast_log"][-10:][::-1]
    if not entries:
        text = to_small_caps("📜 broadcast log") + "\n\n" + to_small_caps("no broadcasts have been sent yet.")
    else:
        lines = [to_small_caps("📜 last 10 broadcasts") + "\n"]
        for e in entries:
            when = iso_to_ist_str(e.get("at"), "%Y-%m-%d %H:%M") + " IST"
            line = (
                f"• {when} — "
                + to_small_caps("delivered") + f" {e.get('recipients', 0)} • "
                + to_small_caps("blocked") + f" {e.get('blocked', 0)} • "
                + to_small_caps("failed") + f" {e.get('other_failed', 0)}"
            )
            lines.append(line)
        text = "\n".join(lines)
    kb = InlineKeyboardMarkup([back_row("adm_broadcast")])
    await query.edit_message_text(text, reply_markup=kb)


async def do_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    # #4 — the master "lock everything" switch ORs together with the
    # broadcast-specific forward-lock toggle.
    s = BOT_DATA["settings"]
    protect = s.get("protect_broadcasts", True) or s.get("lock_all_content", False)
    broadcast_id = BOT_DATA.get("broadcast_next_id", 1)
    BOT_DATA["broadcast_next_id"] = broadcast_id + 1

    status_msg = await update.message.reply_text(
        "📢 " + to_small_caps("broadcast in progress...") + " 0%"
    )

    delivered_ids = {}
    sent = 0
    blocked = 0          # user blocked the bot / kicked it / deactivated account
    invalid_chat = 0     # chat no longer exists / never started the bot
    other_failed = 0     # anything else (unexpected)
    recovered = 0        # succeeded only after a flood-control retry

    user_ids = list(BOT_DATA["users"].keys())
    # The sending admin also receives a copy, so the broadcast is visible in
    # the admin chat exactly like it is for users.
    admin_uid = str(update.effective_user.id)
    if admin_uid not in user_ids:
        user_ids.append(admin_uid)
    total = len(user_ids)
    for i, uid in enumerate(user_ids):
        try:
            copied = await context.bot.copy_message(
                chat_id=int(uid), from_chat_id=msg.chat_id, message_id=msg.message_id,
                protect_content=protect, reply_markup=start_kb,
            )
        except RetryAfter as e:
            # Flood control — Telegram itself tells us exactly how long to
            # wait. Honour it once, then retry this one user before giving
            # up, instead of silently counting a recoverable hit as failed.
            await asyncio.sleep(e.retry_after + 0.5)
            try:
                copied = await context.bot.copy_message(
                    chat_id=int(uid), from_chat_id=msg.chat_id, message_id=msg.message_id,
                    protect_content=protect, reply_markup=start_kb,
                )
                recovered += 1
            except Exception:
                other_failed += 1
                copied = None
        except Forbidden:
            # User blocked the bot, deleted their account, or kicked it
            # from a group — permanent, not worth retrying.
            blocked += 1
            copied = None
        except BadRequest:
            # Chat not found / user never actually opened a DM with the
            # bot — also permanent.
            invalid_chat += 1
            copied = None
        except TelegramError:
            other_failed += 1
            copied = None
        except Exception:
            other_failed += 1
            copied = None

        if copied:
            track_sent_message(int(uid), copied.message_id)
            # BUGFIX — broadcasts must NEVER auto-delete. They used to be
            # swept up by the same global_auto_delete_seconds timer used for
            # ordinary bot replies, so an admin announcement could vanish
            # from a user's chat on its own a few minutes after being sent.
            # A broadcast is only ever removed by an explicit admin action
            # (🗑 Delete Broadcast), never by a timer.
            delivered_ids[uid] = copied.message_id
            sent += 1

        await asyncio.sleep(BROADCAST_SEND_DELAY)

        if total and (i + 1) % 25 == 0:
            pct = int(((i + 1) / total) * 100)
            try:
                await status_msg.edit_text("📢 " + to_small_caps("broadcast in progress...") + f" {pct}%")
            except Exception:
                pass

    failed_total = blocked + invalid_chat + other_failed
    at = datetime.utcnow().isoformat()
    entry = {
        "id": broadcast_id,
        "by": update.effective_user.id,
        "at": at,
        "recipients": sent,
        "blocked": blocked,
        "invalid_chat": invalid_chat,
        "other_failed": other_failed,
        "recovered": recovered,
        "total_targeted": total,
        "messages": delivered_ids,  # {user_id: message_id} — used by Delete Broadcast
        "protect": protect,
        "report_chat_id": None,
        "report_message_id": None,
    }
    BOT_DATA["broadcast_log"].append(entry)
    BOT_DATA["metrics"]["broadcasts_sent"] = BOT_DATA["metrics"].get("broadcasts_sent", 0) + 1
    save_data()

    report = _broadcast_report_text(entry)
    try:
        await status_msg.edit_text(report)
        # Remember where this report lives so cb_bc_start_now can keep it
        # live-updated as users tap 🚀 Start Bot, instead of the numbers
        # only ever being a one-time snapshot.
        entry["report_chat_id"] = status_msg.chat_id
        entry["report_message_id"] = status_msg.message_id
    except Exception:
        sent_report = await update.message.reply_text(report)
        entry["report_chat_id"] = sent_report.chat_id
        entry["report_message_id"] = sent_report.message_id
    save_data()
    await log_event(
        context,
        f"📢 Broadcast sent by {update.effective_user.id} — {sent}/{total} delivered "
        f"(blocked {blocked}, invalid {invalid_chat}, other {other_failed})",
    )


# ---- Delete Broadcast (month-wise) -------------------------------------------

def _broadcast_month_key(iso_ts: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_ts)
    except Exception:
        return "unknown"
    return dt.strftime("%B %Y")  # e.g. "September 2026"


async def _render_adm_bc_delmenu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    entries = BOT_DATA["broadcast_log"]
    months = {}
    for idx, e in enumerate(entries):
        key = _broadcast_month_key(e.get("at", ""))
        months.setdefault(key, []).append(idx)

    if not months:
        text = to_small_caps("🗑 delete broadcast") + "\n\n" + to_small_caps("no broadcasts recorded yet.")
        kb = InlineKeyboardMarkup([back_row("adm_broadcast")])
        await query.edit_message_text(text, reply_markup=kb)
        return

    month_keys = list(months.keys())
    rows = []
    for i in range(0, len(month_keys), 2):
        pair = month_keys[i:i + 2]
        rows.append([
            styled_button(f"🗓 {mk} ({len(months[mk])})", callback_data=f"adm_bc_delmonth:{mk}")
            for mk in pair
        ])
    rows.append(back_row("adm_broadcast"))
    rows.append(home_row())
    text = (
        to_small_caps("🗑 delete broadcast") + "\n"
        + to_small_caps("pick a month to see broadcasts sent that month")
    )
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))


async def cb_adm_bc_delmenu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _render_adm_bc_delmenu(update, context)


async def cb_adm_bc_delmonth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    month = query.data.split(":", 1)[1]
    entries = BOT_DATA["broadcast_log"]
    rows = []
    lines = [to_small_caps("🗓 broadcasts in") + f" {month}\n"]
    for idx, e in enumerate(entries):
        if _broadcast_month_key(e.get("at", "")) != month:
            continue
        when = e.get("at", "?")[:16].replace("T", " ")
        lines.append(f"#{idx} — {when} — " + to_small_caps("delivered") + f" {e.get('recipients', 0)}")
        rows.append([styled_button(f"🗑 #{idx} — {when}", callback_data=f"adm_bc_delconfirm:{idx}")])
    rows.append(back_row("adm_bc_delmenu"))
    rows.append(home_row())
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))


async def cb_adm_bc_delconfirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split(":", 1)[1])
    entries = BOT_DATA["broadcast_log"]
    if idx < 0 or idx >= len(entries):
        await query.edit_message_text(
            "❌ " + to_small_caps("that broadcast no longer exists."),
            reply_markup=InlineKeyboardMarkup([back_row("adm_bc_delmenu")]),
        )
        return
    e = entries[idx]
    when = e.get("at", "?")[:16].replace("T", " ")
    recipients = len(e.get("messages", {}))
    text = (
        "⚠️ " + to_small_caps("confirm delete") + "\n\n"
        + to_small_caps("broadcast") + f" #{idx} — {when}\n"
        + to_small_caps("this will remove it from") + f" {recipients} " + to_small_caps("recipient chats.")
        + "\n\n" + to_small_caps("this action cannot be undone.")
    )
    kb = InlineKeyboardMarkup(
        [
            [styled_button("✅ Yes, Delete", callback_data=f"adm_bc_deldo:{idx}"),
             styled_button("❌ Cancel", callback_data="adm_bc_delmenu")],
        ]
    )
    await query.edit_message_text(text, reply_markup=kb)


async def cb_adm_bc_deldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split(":", 1)[1])
    entries = BOT_DATA["broadcast_log"]
    if idx < 0 or idx >= len(entries):
        await query.edit_message_text(
            "❌ " + to_small_caps("that broadcast no longer exists."),
            reply_markup=InlineKeyboardMarkup([back_row("adm_bc_delmenu")]),
        )
        return
    e = entries[idx]
    messages = e.get("messages", {})
    removed = 0
    gone = 0
    for uid, mid in messages.items():
        try:
            await context.bot.delete_message(chat_id=int(uid), message_id=int(mid))
            removed += 1
        except Exception:
            gone += 1
        await asyncio.sleep(BROADCAST_SEND_DELAY)
    entries.pop(idx)
    save_data()
    text = (
        "✅ " + to_small_caps("broadcast deleted") + "\n\n"
        + to_small_caps("removed from chats") + f": {removed}\n"
        + to_small_caps("already gone / could not remove") + f": {gone}"
    )
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([back_row("adm_bc_delmenu"), home_row()]))
    await log_event(context, f"🗑 Broadcast #{idx} deleted by {update.effective_user.id} — {removed} removed")


# ---- Menu & UI (#1, #2, #4, #7 controls) -------------------------------------

MENU_DISPLAY_NAMES = {
    # v10 — short labels requested for the Menu & UI list, so a button
    # name never gets cut off / hard to read on a phone screen. Anything
    # not in this map falls back to its raw id (title-cased) below.
    "start": "Start",
    "disclaimer": "Disclaimer",
    "download": "Downld",
    "howto": "HTU",
    "gift": "Gift",
    "language": "Language",
    "developer": "Developer",
    "support": "Support",
    "admin": "Admin",
    "help_user": "Help User",
    "reel_result": "Reel Result",
    "maintenance": "Maintenance",
    "bot_live": "Bot Live",
    "help_admin": "Help Admin",
}


async def _render_adm_menu_ui(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    # v6 — 2-per-row grid for the menu list too, so it stays compact even
    # as more menus get added.
    # v10 — one badge, not two: a single ✅ appears on a menu's button only
    # when it already has an image set; nothing is added when it doesn't
    # (previously showed a 🖼️ frame emoji AND a ✅/❌ on every button).
    # Labels now use the short MENU_DISPLAY_NAMES above instead of the raw
    # menu id, so nothing overflows on a phone screen.
    menu_ids = list(BOT_DATA["menus"])

    def _label(mid):
        has_image = bool(BOT_DATA["menus"][mid].get("image_file_id"))
        name = MENU_DISPLAY_NAMES.get(mid, mid.replace("_", " ").title())
        return f"{name} ✅" if has_image else name

    rows = [[styled_button("🖼️ Set Welcome Image", callback_data="adm_menu_img:start")]]
    for i in range(0, len(menu_ids), 2):
        pair = menu_ids[i:i + 2]
        rows.append([styled_button(_label(mid), callback_data=f"adm_menu_edit:{mid}") for mid in pair])
    rows.append(back_row())
    rows.append(home_row())
    text = (
        to_deco(to_title_small_caps("Menu & UI")) + "\n\n"
        + to_small_caps("tap a menu below to edit its text, image or buttons.") + "\n"
        + to_small_caps("✅ next to a menu means it already has an image set.")
    )
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))


async def cb_adm_menu_ui(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _render_adm_menu_ui(update, context)


def _build_menu_edit_screen(menu_id: str):
    """Single source of truth for the 'editing menu X' screen — used both
    when first opening it and when refreshing it in place after a save
    (e.g. right after an image upload), so the two never drift apart."""
    menu = BOT_DATA["menus"][menu_id]
    parse_mode_label = menu.get("parse_mode") or "OFF (Raw Text)"
    override = menu.get("auto_delete_seconds")
    override_label = f"{override}s" if override is not None else "Uses Global"
    has_image = bool(menu.get("image_file_id"))
    image_status = "✅ " + to_title_small_caps("Set") if has_image else "❌ " + to_title_small_caps("Not Set")
    image_btn_label = ("🖼️ Change Image" if has_image else "🖼️ Set Image")

    rows = [
        [styled_button("✏️ Edit Text", callback_data=f"adm_menu_txt:{menu_id}"),
         styled_button("🅰️ Style Text", callback_data=f"adm_menu_style:{menu_id}")],
        [styled_button(f"🔤 Parse Mode: {parse_mode_label}", callback_data=f"adm_menu_parsemode:{menu_id}"),
         styled_button("🔘 Manage Buttons", callback_data=f"adm_menu_btns:{menu_id}")],
    ]
    if has_image:
        rows.append([
            styled_button(image_btn_label, callback_data=f"adm_menu_img:{menu_id}"),
            styled_button("🗑️ Remove Image", callback_data=f"adm_menu_rmimg:{menu_id}"),
        ])
    else:
        rows.append([styled_button(image_btn_label, callback_data=f"adm_menu_img:{menu_id}")])
    rows.append([
        styled_button(f"⏱ Auto-Delete: {override_label}", callback_data=f"adm_menu_autodel:{menu_id}"),
        styled_button("🌐 Translations", callback_data=f"adm_menu_trans:{menu_id}"),
    ])
    rows.append([styled_button("🔙 Back", callback_data="adm_menu_ui")])

    # v10 — the header now names the menu with its short display name (not
    # a generic "Editing Menu" title with the id buried below), and a
    # quoted preview of the CURRENT text is shown right under the status
    # lines — so it's confirmed at a glance which menu this is and what's
    # already set for it, without needing to tap "Edit Text" first.
    display_name = MENU_DISPLAY_NAMES.get(menu_id, menu_id.replace("_", " ").title())
    current_text = menu.get("text") or ""
    preview = html.escape(re.sub(r"<[^>]+>", "", current_text)).strip()
    if len(preview) > 200:
        preview = preview[:200].rstrip() + "…"
    text = (
        to_deco(to_title_small_caps(f"Editing Menu: {display_name}")) + "\n\n"
        + f"<b>ID:</b> <code>{html.escape(menu_id)}</code>\n"
        + f"<b>Image:</b> {image_status}\n"
        + f"<b>Parse Mode:</b> {html.escape(parse_mode_label)}\n"
        + f"<b>Auto-Delete:</b> {html.escape(override_label)}\n\n"
        + "<b>" + to_title_small_caps("Currently Set") + ":</b>\n"
        + f"<blockquote>{preview or to_small_caps('(empty)')}</blockquote>\n\n"
        + to_small_caps("choose what you'd like to change below")
    )
    return text, InlineKeyboardMarkup(rows)


async def cb_adm_menu_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    menu_id = query.data.split(":", 1)[1]
    text, kb = _build_menu_edit_screen(menu_id)
    await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")


async def cb_adm_menu_trans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    menu_id = query.data.split(":", 1)[1]
    langs = BOT_DATA["settings"].get("languages", [])
    if not langs:
        await query.message.reply_text(
            to_small_caps("first add at least one language via settings & admins → 🌐 manage languages.")
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
        to_small_caps(f"send the {LANG_NAMES.get(code, code)} translation text for '{menu_id}'") + "\n"
        + to_small_caps("(buttons stay the same as the base menu — they are not translated.)")
    )


async def cb_adm_menu_txt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    menu_id = query.data.split(":", 1)[1]
    context.user_data["awaiting"] = f"menu_text:{menu_id}"
    await query.message.reply_text(
        to_small_caps("send the new text — it will be saved exactly as sent, with no auto-reformatting.")
    )


async def cb_adm_menu_style(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    menu_id = query.data.split(":", 1)[1]
    context.user_data["awaiting"] = f"menu_style_source:{menu_id}"
    await query.message.reply_text(to_small_caps("send plain text and a preview of every available style will be shown."))


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
    # Remember this exact "editing menu" screen so that once the photo is
    # received, we can flip its 🖼️ status straight to ✅ Set in place,
    # instead of leaving the admin to guess whether it actually saved.
    remember_panel_message(context, query, f"menu_edit:{menu_id}")
    await query.message.reply_text(to_small_caps("send a photo (image only, not a video)."))


async def cb_adm_menu_rmimg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    menu_id = query.data.split(":", 1)[1]
    BOT_DATA["menus"][menu_id]["image_file_id"] = None
    save_data()
    await query.message.reply_text(to_small_caps("✅ image removed — this is now a text-only menu."))
    await cb_adm_menu_edit(update, context)


async def cb_adm_menu_autodel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    menu_id = query.data.split(":", 1)[1]
    context.user_data["awaiting"] = f"menu_autodel:{menu_id}"
    await query.message.reply_text(
        to_small_caps("send the auto-delete time in seconds for this menu (0 = never, or type 'global' to use the global default).")
    )


def _menu_btns_keyboard(menu_id: str) -> InlineKeyboardMarkup:
    buttons = BOT_DATA["menus"][menu_id]["buttons"]
    rows = []
    for i, b in enumerate(buttons):
        rows.append([
            styled_button(f"{i}: {_short_btn_label(b['label'])}", callback_data="noop"),
            styled_button("✏️", callback_data=f"adm_btn_edit:{menu_id}:{i}"),
            styled_button("🅰️", callback_data=f"adm_btn_style:{menu_id}:{i}"),
            styled_button("❌", callback_data=f"adm_btn_del:{menu_id}:{i}"),
        ])
    rows.append([styled_button("➕ Add Button", callback_data=f"adm_btn_add:{menu_id}")])
    rows.append([styled_button("🔙 Back", callback_data=f"adm_menu_edit:{menu_id}")])
    return InlineKeyboardMarkup(rows)


async def cb_adm_menu_btns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    menu_id = query.data.split(":", 1)[1]
    text = to_deco(to_title_small_caps(f"Buttons: {menu_id}"))
    await query.edit_message_text(text, reply_markup=_menu_btns_keyboard(menu_id))


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
    """Helper to redraw the buttons list after a delete/edit, without a fresh query.data."""
    text = to_deco(to_title_small_caps(f"Buttons: {menu_id}"))
    await update.callback_query.edit_message_text(text, reply_markup=_menu_btns_keyboard(menu_id))


async def cb_adm_btn_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Edit-in-place: pre-fills the same label/type/value/row flow used by
    Add Button with the button's current values, so the admin can just send
    '-' at any step to keep it as-is instead of deleting + re-adding."""
    query = update.callback_query
    await query.answer()
    _, menu_id, idx = query.data.split(":", 2)
    idx = int(idx)
    buttons = BOT_DATA["menus"][menu_id]["buttons"]
    if not (0 <= idx < len(buttons)):
        await query.message.reply_text(to_small_caps("that button no longer exists — refresh the list."))
        return
    existing = dict(buttons[idx])
    context.user_data["btn_flow"] = {"menu_id": menu_id, "data": dict(existing), "edit_idx": idx}
    context.user_data["awaiting"] = "btn_step_label"
    await query.message.reply_text(
        to_small_caps(f"editing button {idx}. current label: {existing.get('label')}") + "\n"
        + to_small_caps("send the new label, or send - to keep it as-is.")
    )


async def cb_adm_btn_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    menu_id = query.data.split(":", 1)[1]
    context.user_data["btn_flow"] = {"menu_id": menu_id, "data": {}}
    context.user_data["awaiting"] = "btn_step_label"
    await query.message.reply_text(to_small_caps("send the new button's label (emoji are welcome)."))


async def cb_btn_type_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, btype = query.data.split(":", 1)
    flow = context.user_data.get("btn_flow")
    if not flow:
        await query.message.reply_text(to_small_caps("session expired — please try again via /admin."))
        return
    if btype == "keep":
        # Edit flow only — type & value stay as they already are in flow["data"].
        context.user_data["awaiting"] = "btn_step_value"
        cur_val = flow["data"].get("value", "")
        await query.message.reply_text(
            to_small_caps(f"current value: {cur_val}") + "\n" + to_small_caps("send a new value, or send - to keep it.")
        )
        return
    flow["data"]["type"] = btype
    context.user_data["awaiting"] = "btn_step_value"
    prompts = {
        "menu": to_small_caps("which menu_id should this open? (e.g. start, help_user)"),
        "url": to_small_caps("send the url (must start with https://)."),
        "callback": to_small_caps("send the internal action's callback_data (e.g. adm_stats)."),
        "toggle": to_small_caps("send the settings key to toggle (e.g. maintenance)."),
    }
    await query.message.reply_text(prompts.get(btype, to_small_caps("send the value:")))


# ---- Settings & Admins --------------------------------------------------------

async def _render_adm_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    s = BOT_DATA["settings"]
    # v6 — same 2-per-row grid treatment as the top-level Admin Panel, so
    # deep submenus stay just as easy to scan and control.
    kb = InlineKeyboardMarkup(
        [
            [styled_button("🖼 Set Welcome Image", callback_data="adm_menu_img:start"),
             styled_button("🔒 Maintenance", callback_data="adm_maintenance")],
            [styled_button(f"⏱ Global Auto-Delete: {s.get('global_auto_delete_seconds', 0)}s", callback_data="adm_set_autodelete"),
             styled_button("💬 Auto-Replies", callback_data="adm_autoreply_list")],
            [styled_button("👤 Manage Admins", callback_data="adm_manage_admins"),
             styled_button("📥 Restore Backup", callback_data="adm_restore_info")],
            [styled_button(
                 toggle_label("🔐 Lock All Forwarding", s.get('lock_all_content')),
                 callback_data="stgl:lock_all_content:adm_settings",
             ),
             styled_button("👑 Owner/Developer Contact", callback_data="adm_owner_contact")],
            [styled_button("📋 Logger Channel", callback_data="adm_logger_channel"),
             styled_button("📣 Activity Channel", callback_data="adm_activity_channel")],
            [styled_button(f"📢 Force-Join: {s.get('force_join_channel') or 'OFF'}", callback_data="adm_force_join")],
            [styled_button(
                toggle_label("📄 Send As Document", s.get('send_as_document')),
                callback_data="stgl:send_as_document:adm_settings",
            )],
            [styled_button(
                 toggle_label("🌟 Premium Emoji Greeting", s.get('premium_emoji_enabled')),
                 callback_data="stgl:premium_emoji_enabled:adm_settings",
             ),
             styled_button("✏️ Set Premium Emoji", callback_data="adm_set_premium_emoji")],
            back_row(),
            home_row(),
        ]
    )
    await query.edit_message_text(
        "⚙️ " + to_small_caps("settings") + "\n"
        + to_small_caps("configure core bot behaviour below."),
        reply_markup=kb,
    )


async def cb_adm_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _render_adm_settings(update, context)


async def cb_adm_set_premium_emoji(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["awaiting"] = "premium_emoji_capture"
    await query.message.reply_text(
        to_small_caps("🌟 send (or forward) a message that contains ONE premium custom emoji.") + "\n"
        + to_small_caps("i'll grab that emoji's id and use it in the start greeting when premium emoji is turned on.") + "\n\n"
        + to_small_caps("note: this only works if the sender actually has telegram premium — that's a telegram limit, not this bot's.")
    )


async def handle_premium_emoji_capture(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reads MessageEntity(type='custom_emoji') off the admin's sample
    message. Registered as its own MessageHandler (entities aren't plain
    text) rather than folded into handle_admin_text_input."""
    if context.user_data.get("awaiting") != "premium_emoji_capture":
        return
    if not is_admin(update.effective_user.id):
        return
    context.user_data["awaiting"] = None
    msg = update.message
    entities = msg.entities or msg.caption_entities or []
    ce = next((e for e in entities if e.type == MessageEntity.CUSTOM_EMOJI), None)
    if not ce:
        await msg.reply_text(
            to_small_caps("❌ no custom emoji found in that message — make sure it's an actual premium animated emoji, not a regular unicode emoji.")
        )
        return
    text_src = msg.text or msg.caption or ""
    # offset/length are UTF-16 code units per the Bot API spec.
    utf16 = text_src.encode("utf-16-le")
    glyph_bytes = utf16[ce.offset * 2: (ce.offset + ce.length) * 2]
    glyph = glyph_bytes.decode("utf-16-le", errors="ignore") or "🌟"
    BOT_DATA["settings"]["premium_emoji_id"] = ce.custom_emoji_id
    BOT_DATA["settings"]["premium_emoji_char"] = glyph
    save_data()
    await msg.reply_text(
        to_small_caps("✅ premium emoji saved.") + "\n"
        + to_small_caps("turn on '🌟 premium emoji greeting' in settings to use it on /start.")
    )


async def send_premium_emoji_greeting(bot, chat_id: int):
    """Sends a short standalone greeting line with the admin-configured
    Premium custom emoji, right before the normal /start menu. Kept as its
    own message (entities-based, no HTML) so it never conflicts with the
    HTML parse_mode used everywhere else. Non-Premium viewers automatically
    see the fallback glyph — that's Telegram's own behaviour, not ours."""
    s = BOT_DATA["settings"]
    if not s.get("premium_emoji_enabled") or not s.get("premium_emoji_id"):
        return
    glyph = s.get("premium_emoji_char") or "🌟"
    caption = to_small_caps(" welcome!")
    text = glyph + caption
    glyph_len = len(glyph.encode("utf-16-le")) // 2
    entities = [MessageEntity(type=MessageEntity.CUSTOM_EMOJI, offset=0, length=glyph_len, custom_emoji_id=s["premium_emoji_id"])]
    try:
        await bot.send_message(chat_id, text, entities=entities)
    except Exception:
        log.warning("Premium emoji greeting failed (id may be stale/invalid)", exc_info=True)


# ---- Maintenance (single combined screen: status + toggle + set message) ---

def _maintenance_kb(is_on: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [styled_button(toggle_label("Maintenance", is_on), callback_data="stgl:maintenance:adm_maintenance")],
            [styled_button("✏️ Set New Message", callback_data="adm_maint_setmsg")],
            back_row("adm_settings"),
            home_row(),
        ]
    )


async def _render_adm_maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    is_on = bool(BOT_DATA["settings"].get("maintenance"))
    current_text = BOT_DATA["menus"].get("maintenance", {}).get("text", "")
    preview = (current_text[:250] + "…") if len(current_text) > 250 else current_text
    status_line = (
        "🔴 " + to_small_caps("active — only admins can use the bot right now")
        if is_on else
        "🟢 " + to_small_caps("off — bot is live and working normally")
    )
    body = (
        "🔒 " + to_small_caps("maintenance") + "\n\n"
        + status_line + "\n\n"
        + to_small_caps("current message shown to users") + ":\n"
        + preview
    )
    await query.edit_message_text(body, reply_markup=_maintenance_kb(is_on))


async def cb_adm_maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _render_adm_maintenance(update, context)


async def cb_adm_maint_setmsg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["awaiting"] = "maintenance_set_msg"
    await query.message.reply_text(
        "✏️ " + to_small_caps("send the new maintenance message now") + "\n"
        + to_small_caps("this is exactly what users will see while maintenance is on.")
    )


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
    await query.message.reply_text(to_small_caps("send the button label (e.g. '👑 developer' or '💬 contact us')."))


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
        f"Enabled: {'✅ ON' if s.get('logger_enabled') else '❌ OFF'}\n\n"
        "Logs new users, downloads, broadcasts, admin changes, copyright "
        "reports, and errors here."
    )
    kb = InlineKeyboardMarkup(
        [
            [styled_button("✏️ Set Channel", callback_data="adm_logger_channel_set")],
            [styled_button(
                toggle_label("🔀 Enabled", s.get('logger_enabled')),
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


# ---- #18 — Activity Channel (dedicated home for the Reel Delivered card) ------

def _build_adm_activity_channel_view():
    s = BOT_DATA["settings"]
    text = (
        "📣 Activity Channel\n\n"
        f"Channel ID: {s.get('activity_channel_id') or '(not set)'}\n"
        f"Enabled: {'✅ ON' if s.get('activity_channel_enabled') else '❌ OFF'}\n\n"
        "Every delivered reel posts a clean 'Reel Delivered' card here — "
        "separate from the general Logger Channel, so this stays a pure "
        "delivery feed with no error/debug noise mixed in."
    )
    kb = InlineKeyboardMarkup(
        [
            [styled_button("✏️ Set Channel", callback_data="adm_activity_channel_set")],
            [styled_button(
                toggle_label("🔀 Enabled", s.get('activity_channel_enabled')),
                callback_data="stgl:activity_channel_enabled:adm_activity_channel",
            )],
            back_row(),
        ]
    )
    return text, kb


async def _render_adm_activity_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    text, kb = _build_adm_activity_channel_view()
    await query.edit_message_text(text, reply_markup=kb)


async def cb_adm_activity_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _render_adm_activity_channel(update, context)


async def cb_adm_activity_channel_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    remember_panel_message(context, query, "activity_channel")
    context.user_data["awaiting"] = "activity_channel_id"
    await query.message.reply_text(
        "Forward any message from the target channel here (bot must be an "
        "admin there), or just type its numeric ID (looks like -100xxxxxxxxxx)."
    )


# ---- v3 §7 — Force-join channel ------------------------------------------------

def _build_adm_force_join_view():
    targets = _force_join_targets()
    lines = []
    for i, t in enumerate(targets, 1):
        lines.append(f"{i}. {t.get('chat_id') or '(link-only)'}\n   🔗 {t.get('link') or 'auto-resolve'}")
    text = (
        "📢 Multiple Force-Join\n\n"
        + ("\n\n".join(lines) if lines else "No channels set — force-join disabled.")
        + "\n\nUsers must satisfy ALL listed channels. Public @usernames, numeric "
          "channel IDs and t.me/invite links are supported. Join requests are also "
          "accepted when Telegram delivers the request update to this bot."
    )
    kb_rows = [
        [styled_button("➕ Add Channel / Link", callback_data="adm_force_join_set")],
        [styled_button("🗑 Remove Last", callback_data="adm_force_join_remove")] if targets else [],
        [styled_button("❌ Disable All", callback_data="adm_force_join_clear")],
        back_row(),
    ]
    kb_rows = [r for r in kb_rows if r]
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
        "Send a public @channelusername, numeric channel ID (-100xxxxxxxxxx), "
        "or a full https://t.me/... invite/join link. You can add multiple channels one by one. "
        "For reliable membership checking, the bot should be an admin in each channel."
    )


async def cb_adm_force_join_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    BOT_DATA["settings"]["force_join_channel"] = None
    BOT_DATA["settings"]["force_join_channels"] = []
    save_data()
    await _render_adm_force_join(update, context)


async def cb_adm_force_join_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    targets = _force_join_targets()
    if targets:
        targets.pop()
        BOT_DATA["settings"]["force_join_channels"] = targets
        BOT_DATA["settings"]["force_join_channel"] = targets[0].get("chat_id") if targets else None
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


def _build_adm_leaderboard_view():
    # FIX — split out of the old combined "Leaderboard & Sharing" screen;
    # this screen is now Leaderboard-only. Share settings moved to their
    # own "📤 Share Settings" section (see _build_adm_share_view below).
    s = BOT_DATA["settings"]
    lb_on = s.get("leaderboard_enabled", False)
    donor_count = len([d for d in BOT_DATA.get("donations", {}).values() if d.get("score", 0) > 0])
    text = (
        "🏆 Leaderboard\n\n"
        f"Status: {'✅ ON' if lb_on else '❌ OFF'} — shown inside 🎁 Send Gift, "
        f"{donor_count} donor(s) ranked so far.\n\n"
        "Only voluntary 🎁 Send Gift donations count here — Premium Plan "
        "purchases are subscriptions, not gifts, so they're never counted.\n\n"
        + build_leaderboard_text()
    )
    kb_rows = [
        [styled_button(
            f"{'✅' if lb_on else '❌'} Leaderboard",
            callback_data="stgl:leaderboard_enabled:adm_leaderboard",
        )],
        [styled_button("📢 Post Leaderboard to All Users", callback_data="adm_post_leaderboard")],
        back_row(),
    ]
    return text, InlineKeyboardMarkup(kb_rows)


async def _render_adm_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    text, kb = _build_adm_leaderboard_view()
    await query.edit_message_text(text, reply_markup=kb)


async def cb_adm_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _render_adm_leaderboard(update, context)


async def cb_adm_post_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """FIX — this used to only post into the logger channel (or the admin's
    own chat as a fallback), so it never actually reached users — exactly
    the "sirf mere chat me raha, users me nahi gaya" bug. It now broadcasts
    the leaderboard to every user the bot knows about, same delivery path
    as the real Broadcast feature, and still cross-posts to the logger
    channel too if one is configured."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    text = build_leaderboard_text()
    sent, failed = 0, 0
    for uid in list(BOT_DATA["users"].keys()):
        try:
            m = await context.bot.send_message(int(uid), text)
            track_sent_message(int(uid), m.message_id)
            await schedule_delete(context, int(uid), m.message_id, BOT_DATA["settings"].get("global_auto_delete_seconds", 0))
            sent += 1
        except Exception:
            failed += 1
    channel = BOT_DATA["settings"].get("logger_channel_id") if BOT_DATA["settings"].get("logger_enabled") else None
    if channel:
        try:
            await context.bot.send_message(channel, text)
        except Exception:
            pass
    await query.message.reply_text(f"✅ {to_small_caps('leaderboard posted to all users')}\nSent: {sent} | Failed: {failed}")
    await log_event(context, f"🏆 Leaderboard posted to users by {update.effective_user.id} — {sent} recipients")


def _build_adm_share_view():
    # FIX — split out of the old combined "Leaderboard & Sharing" screen;
    # this is now its own standalone "📤 Share Settings" section.
    s = BOT_DATA["settings"]
    share_on = s.get("share_enabled", True)
    share_url = s.get("share_url") or to_small_caps("(default — bot's own link)")
    text = (
        "📤 Share Settings\n\n"
        f"Share button (under My Usage): {'✅ ON' if share_on else '❌ OFF'}\n"
        f"Share URL: {share_url}"
    )
    kb_rows = [
        [styled_button(
            f"{'✅' if share_on else '❌'} Share Button",
            callback_data="stgl:share_enabled:adm_share",
        )],
        [styled_button("✏️ Set Share URL", callback_data="adm_share_url_set")],
    ]
    if s.get("share_url"):
        kb_rows.append([styled_button("🗑️ Reset Share URL", callback_data="adm_share_url_clear")])
    kb_rows.append(back_row())
    return text, InlineKeyboardMarkup(kb_rows)


async def _render_adm_share(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    text, kb = _build_adm_share_view()
    await query.edit_message_text(text, reply_markup=kb)


async def cb_adm_share(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _render_adm_share(update, context)


async def cb_adm_share_url_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    remember_panel_message(context, query, "share")
    context.user_data["awaiting"] = "share_url"
    await query.message.reply_text(
        "Type the URL the '📤 Share' button (under My Usage) should open — "
        "e.g. your channel link or a landing page. Send /cancel to leave it as-is."
    )


async def cb_adm_share_url_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    BOT_DATA["settings"]["share_url"] = None
    save_data()
    await _render_adm_share(update, context)


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
    was_on = bool(BOT_DATA["settings"].get(key, False))
    BOT_DATA["settings"][key] = not was_on
    save_data()

    if key == "maintenance":
        # Single combined screen now (status + toggle + set-message all in
        # one place) — toggling just re-renders that same screen instead of
        # showing a separate bulky "MAINTENANCE ON" / "BOT IS LIVE" card.
        if BOT_DATA["settings"]["maintenance"]:
            await query.answer("🔒 " + to_small_caps("maintenance enabled."), show_alert=False)
        else:
            await query.answer("🟢 " + to_small_caps("bot is live again."), show_alert=False)
            # Tell every user who actually hit the maintenance wall — not
            # just the admin looking at this panel — that the bot is back.
            await broadcast_bot_live(context)
        await _render_adm_maintenance(update, context)
        return

    renderer = SCREEN_RENDERERS.get(return_to)
    if renderer is not None:
        await renderer(update, context)
    else:
        await query.answer(to_small_caps("✅ updated."), show_alert=False)


async def cb_adm_lang_manage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    langs = BOT_DATA["settings"].get("languages", [])
    text = "🌐 Enabled Languages\n\n" + ("\n".join(f"• {LANG_NAMES.get(c, c)}" for c in langs) if langs else to_small_caps("none — default language only."))
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
    await query.message.reply_text(to_small_caps("which language would you like to add?"), reply_markup=InlineKeyboardMarkup(rows))


async def cb_adm_lang_add_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    code = query.data.split(":", 1)[1]
    if code not in BOT_DATA["settings"]["languages"]:
        BOT_DATA["settings"]["languages"].append(code)
        save_data()
    await query.edit_message_text(to_small_caps(f"✅ {LANG_NAMES.get(code, code)} added. you can now add text for it via 🌐 translations in any menu."))


async def cb_adm_lang_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    langs = BOT_DATA["settings"].get("languages", [])
    if not langs:
        await query.message.reply_text(to_small_caps("no languages have been added yet."))
        return
    rows = [[styled_button(LANG_NAMES.get(c, c), callback_data=f"adm_lang_remove_do:{c}")] for c in langs]
    await query.message.reply_text(to_small_caps("which language would you like to remove?"), reply_markup=InlineKeyboardMarkup(rows))


async def cb_adm_lang_remove_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    code = query.data.split(":", 1)[1]
    if code in BOT_DATA["settings"]["languages"]:
        BOT_DATA["settings"]["languages"].remove(code)
        save_data()
    await query.edit_message_text(to_small_caps(f"✅ {LANG_NAMES.get(code, code)} removed."))


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
        [f"• `{k}` → {v[:30]}" for k, v in replies.items()] or [to_small_caps("no auto-reply has been set.")]
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
    await query.message.reply_text(to_small_caps("send the trigger keyword or phrase — it will auto-reply whenever a message contains it."))


async def cb_adm_autoreply_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["awaiting"] = "autoreply_delkey"
    await query.message.reply_text(to_small_caps("send the keyword you want to remove."))


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
        await query.message.reply_text(to_small_caps("only the owner can add a new admin."))
        return
    context.user_data["awaiting"] = "add_admin_id"
    await query.message.reply_text(to_small_caps("send the new admin's user id."))


async def cb_adm_remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_owner(update.effective_user.id):
        await query.message.reply_text(to_small_caps("only the owner can remove an admin."))
        return
    context.user_data["awaiting"] = "remove_admin_id"
    await query.message.reply_text(to_small_caps("send the user id of the admin to remove."))


async def cb_adm_restore_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        to_small_caps("📥 restore backup") + "\n\n"
        + to_small_caps("send me the .json or .json.enc backup file directly in this dm (the one exported via /database). ")
        + to_small_caps("an automatic backup of the current data will be taken first, then a confirmation screen will be shown.") + "\n\n"
        + (to_small_caps("🔒 encrypted (.json.enc) backups only restore on this same bot instance, unless you copied backup.key over first.") if BACKUP_ENCRYPTION_AVAILABLE else "")
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
    premium_count = sum(1 for uid in BOT_DATA["users"] if is_premium_active(uid))
    lines = [
        f"💎 Premium\n\nMaster switch: {'✅ ON' if s.get('premium_enabled') else '❌ OFF'}",
        f"Daily free limit: {s.get('daily_limit', 20)}",
        f"👥 Active premium users: {premium_count}",
        "",
    ]
    # Main controls stay at the top; every ➕ Add action is grouped at the
    # bottom so the management flow is cleaner on mobile.
    kb_rows = [
        [styled_button(toggle_label("🔀 Master Switch", s.get('premium_enabled')),
                        callback_data="stgl:premium_enabled:adm_premium")],
        [styled_button("✏️ Set Daily Limit", callback_data="adm_set_dailylimit")],
        [styled_button("👥 See Premium Users", callback_data="adm_premium_users")],
        [styled_button("💳 UPI Settings", callback_data="adm_upi")],
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
    # Keep all Add actions together at the bottom.
    kb_rows.append([styled_button("➕ Add Premium User (By ID)", callback_data="adm_premium_grant")])
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


# ---- NEW — See Premium Users / manually grant premium by user ID --------------

async def cb_adm_premium_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """👥 See Premium Users — lists every user currently marked premium,
    with days remaining (or 'no expiry' for lifetime grants)."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    rows = []
    now = datetime.utcnow()
    for uid, u in BOT_DATA["users"].items():
        if not is_premium_active(uid):
            continue
        name = u.get("name") or f"User {uid}"
        exp = u.get("plan_expires_at")
        if exp:
            try:
                remaining = (datetime.fromisoformat(exp) - now).days
                when = f"{remaining}d left" if remaining >= 0 else "expiring"
            except Exception:
                when = "unknown expiry"
        else:
            when = "no expiry"
        rows.append(f"• {name} (`{uid}`) — {when}")
    if rows:
        text = "👥 " + to_small_caps("premium users") + f" ({len(rows)})\n\n" + "\n".join(rows[:60])
        if len(rows) > 60:
            text += f"\n… and {len(rows) - 60} more"
    else:
        text = "👥 " + to_small_caps("no premium users right now.")
    kb = InlineKeyboardMarkup([back_row("adm_premium")])
    await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")


async def cb_adm_premium_grant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """➕ Add Premium User (by ID) — admin types a Telegram user ID (and
    optionally how many days) and premium unlocks automatically, no
    payment/plan flow needed. Same manual-override tool admins expect for
    comps, testers, or fixing a missed payment."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    remember_panel_message(context, query, "premium")
    context.user_data["awaiting"] = "premium_grant_userid"
    await query.message.reply_text(
        "👤 Send the user's Telegram ID to unlock Premium for.\n"
        "Optionally add days after a space (default 30) — e.g. `123456789 90`.",
        parse_mode="Markdown",
    )


# ---- Premium plan CRUD (add / toggle / delete) ---------------------------------

async def cb_adm_plan_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    remember_panel_message(context, query, "premium")
    context.user_data["new_plan"] = {}
    context.user_data["awaiting"] = "plan_step_name"
    await query.message.reply_text(
        to_small_caps("➕ new plan — step 1/4") + "\n" + to_small_caps("send the plan name (e.g. 'monthly', 'weekly pro').")
    )


async def cb_adm_plan_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pid = query.data.split(":", 1)[1]
    for p in BOT_DATA["settings"].get("premium_plans", []):
        if p["id"] == pid:
            p["enabled"] = not p.get("enabled")
            save_data()
            await log_event(context, f"💎 Plan '{p['name']}' toggled {'✅ ON' if p['enabled'] else '❌ OFF'} by {update.effective_user.id}")
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
    await query.message.reply_text(to_small_caps("send the new daily free-download limit (a number)."))


def _build_adm_upi_view():
    upi = BOT_DATA["settings"].get("upi_id")
    kb = InlineKeyboardMarkup([
        [styled_button("✏️ Set UPI ID", callback_data="adm_upi_set")],
        [styled_button("❌ Clear", callback_data="adm_upi_clear")],
        back_row("adm_premium"), home_row(),
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
    await query.message.reply_text(to_small_caps("send the upi id (e.g. name@bank)."))


async def cb_adm_upi_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    BOT_DATA["settings"]["upi_id"] = None
    save_data()
    await query.edit_message_text("✅ UPI ID cleared.", reply_markup=InlineKeyboardMarkup([back_row()]))


def _build_adm_devsettings_view():
    s = BOT_DATA["settings"]
    dev_id = s.get("developer_id")
    dev_link = s.get("developer_link")
    current = f"@{dev_link.rstrip('/').rsplit('/', 1)[-1]}" if dev_link else (str(dev_id) if dev_id else "(not set)")
    text = (
        "👨‍💻 Developer Settings\n\n"
        f"Current: {current}\n\n"
        "Set by numeric user ID or by @username — either works."
    )
    kb = InlineKeyboardMarkup([
        [styled_button("✏️ Set Developer (ID or @username)", callback_data="adm_dev_id")],
        [styled_button("🔗 Set Custom Link (advanced)", callback_data="adm_dev_link")],
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
    await query.message.reply_text(
        to_small_caps("send the developer's numeric user id, OR their @username — either works.")
    )


async def cb_adm_dev_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    remember_panel_message(context, query, "devsettings")
    context.user_data["awaiting"] = "developer_link"
    await query.message.reply_text(to_small_caps("send the t.me/username link (or type 'clear' to remove it)."))


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
    await query.message.reply_text(to_small_caps("✅ broadcast log cleared."))


async def cb_adm_reset_menus_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kb = InlineKeyboardMarkup(
        [[styled_button(to_small_caps("⚠️ yes, reset"), callback_data="adm_reset_menus_do"),
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
        [[styled_button(to_small_caps("⚠️ yes, reset everything"), callback_data="adm_reset_do"),
          styled_button("Cancel", callback_data="adm_danger")]]
    )
    await query.edit_message_text(
        to_small_caps("⚠️ are you sure? this will delete ALL bot data — users, settings, menus, everything.") + "\n" + to_small_caps("an auto-backup will be taken first."),
        reply_markup=kb,
    )


RESET_ALL_PASSCODE = "03"


async def cb_adm_reset_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A destructive, irreversible action gets one extra layer beyond the
    button confirm — a short passcode the admin has to type, so a stray or
    mis-tapped click can never wipe the bot on its own."""
    query = update.callback_query
    await query.answer()
    context.user_data["awaiting"] = "reset_all_passcode"
    await query.message.reply_text(
        "🔐 " + to_small_caps("last step — send the reset passcode to confirm.")
    )


async def _do_reset_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_DATA
    make_backup_snapshot(reason="pre_reset")
    BOT_DATA = json.loads(json.dumps(DEFAULT_DATA))
    save_data()
    await update.message.reply_text(to_small_caps("✅ reset complete. the previous data is safely stored in a backup."))


# ----------------------------------------------------------------------------
# Text-input flows triggered from admin panel buttons
# ----------------------------------------------------------------------------

async def handle_admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE, awaiting: str):
    text = (update.message.text or "").strip()
    # FIX — "jo admin ne bheja usko chat se delete kar de, bas panel section
    # pe wo add ho jaye": every admin config-input message (plan steps, URLs,
    # IDs, menu text, etc.) is now auto-deleted right after being read, same
    # cleanup pattern /start and /admin already use — only the confirmation /
    # refreshed panel view stays in chat, not the raw text the admin typed.
    await delete_incoming(update)

    if awaiting == "maintenance_set_msg":
        context.user_data.pop("awaiting", None)
        menu = BOT_DATA["menus"]["maintenance"]
        menu["text"] = text
        menu["updated_by"] = update.effective_user.id
        menu["updated_at"] = datetime.utcnow().isoformat()
        save_data()
        # Confirmation + the combined status/toggle screen right below it,
        # so the admin can flip maintenance on/off immediately without
        # hunting for a separate button.
        is_on = bool(BOT_DATA["settings"].get("maintenance"))
        await update.message.reply_text(
            "✅ " + to_small_caps("maintenance message updated.") + "\n\n"
            + to_small_caps("preview") + ":\n" + text,
            reply_markup=_maintenance_kb(is_on),
        )

    elif awaiting.startswith("menu_text:"):
        menu_id = awaiting.split(":", 1)[1]
        context.user_data.pop("awaiting", None)
        menu = BOT_DATA["menus"][menu_id]
        if menu.get("image_file_id") and len(text) > 1024:
            await update.message.reply_text(
                to_small_caps(f"⚠️ this menu has an image, so the caption limit is 1024 characters — your text is {len(text)}. ")
                + to_small_caps("please shorten it, or remove the image first.")
            )
            return
        menu["text"] = text
        menu["updated_by"] = update.effective_user.id
        menu["updated_at"] = datetime.utcnow().isoformat()
        save_data()
        await update.message.reply_text(to_small_caps(f"✅ '{menu_id}' text updated."))

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
            await update.message.reply_text(to_small_caps("send a number, or type 'global'."))
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
        await update.message.reply_text(to_small_caps(f"✅ {LANG_NAMES.get(code, code)} translation for '{menu_id}' saved."))

    elif awaiting == "global_autodelete":
        context.user_data.pop("awaiting", None)
        if not text.isdigit():
            await update.message.reply_text(to_small_caps("please send a number only."))
            context.user_data["awaiting"] = awaiting
            return
        BOT_DATA["settings"]["global_auto_delete_seconds"] = int(text)
        save_data()
        await update.message.reply_text(f"✅ Global auto-delete set to {text}s.")

    elif awaiting == "autoreply_key":
        context.user_data["autoreply_key_draft"] = text.lower()
        context.user_data["awaiting"] = "autoreply_value"
        await update.message.reply_text(to_small_caps("now send the reply text."))

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
        await update.message.reply_text(to_small_caps("✅ removed, if that keyword existed."))

    elif awaiting == "add_admin_id":
        context.user_data.pop("awaiting", None)
        if not text.isdigit():
            await update.message.reply_text(to_small_caps("please send a valid numeric user id."))
            return
        new_id = int(text)
        if new_id not in BOT_DATA["admins"]:
            BOT_DATA["admins"].append(new_id)
            save_data()
        await update.message.reply_text(to_small_caps(f"✅ {new_id} is now an admin."))
        await log_event(context, f"👤 Admin added: {new_id} (by {update.effective_user.id})")

    elif awaiting == "remove_admin_id":
        context.user_data.pop("awaiting", None)
        if not text.isdigit():
            await update.message.reply_text(to_small_caps("please send a valid numeric user id."))
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
        await update.message.reply_text(to_small_caps("now send the user id or @username the button should point to."))

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
        raw = text.strip()
        if not raw:
            await update.message.reply_text("❌ Please send a valid channel username, ID, or t.me link.")
            return
        target = {"chat_id": None, "link": None}
        if raw.startswith("http://") or raw.startswith("https://"):
            target["link"] = raw
            # Public t.me usernames can also be checked directly.
            m = re.match(r"https?://(?:t\.me|telegram\.me)/([^/?#]+)", raw)
            if m and not m.group(1).startswith("+") and m.group(1) not in ("joinchat",):
                target["chat_id"] = "@" + m.group(1).lstrip("@")
        else:
            channel = raw
            if not channel.lstrip("-").isdigit() and not channel.startswith("@"):
                channel = "@" + channel
            target["chat_id"] = channel
        targets = _force_join_targets()
        key = str(target.get("chat_id") or target.get("link"))
        if any(str(x.get("chat_id") or x.get("link")) == key for x in targets):
            await update.message.reply_text("⚠️ That force-join target is already added.")
            return
        targets.append(target)
        BOT_DATA["settings"]["force_join_channels"] = targets
        BOT_DATA["settings"]["force_join_channel"] = targets[0].get("chat_id") if targets else None
        save_data()
        refreshed = await refresh_panel_after_save(context, "force_join", lambda: _build_adm_force_join_view())
        await update.message.reply_text(
            f"✅ Added force-join target: {raw}\n\n"
            "Add another from the panel if needed. For private link-only channels, "
            "membership can be confirmed through join-request updates; for normal "
            "membership checks, configure the channel ID/@username and make the bot admin."
            + ("" if refreshed else "\n(Reopen the panel to confirm.)")
        )

    elif awaiting == "share_url":
        context.user_data.pop("awaiting", None)
        url = text.strip()
        if url.lower() in ("/cancel", "cancel", "-"):
            await update.message.reply_text("Cancelled — share URL left unchanged.")
            return
        if not (url.startswith("http://") or url.startswith("https://") or url.startswith("tg://")):
            url = "https://t.me/" + url.lstrip("@")
        BOT_DATA["settings"]["share_url"] = url
        save_data()
        refreshed = await refresh_panel_after_save(context, "share", _build_adm_share_view)
        await update.message.reply_text(f"✅ Share URL set: {url}" + ("" if refreshed else "\n(reopen the share panel to confirm)"))

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
                to_small_caps("channel not detected. either forward a message from the channel, ")
                + to_small_caps("or type the numeric id (-100...).")
            )
            context.user_data["awaiting"] = "logger_channel_id"
            return
        BOT_DATA["settings"]["logger_channel_id"] = chat_id
        BOT_DATA["settings"]["logger_enabled"] = True
        save_data()
        refreshed = await refresh_panel_after_save(context, "logger_channel", _build_adm_logger_channel_view)
        await update.message.reply_text(f"✅ Logger channel set to {chat_id} and enabled." + ("" if refreshed else "\n(reopen the logger panel to confirm)"))

    elif awaiting == "activity_channel_id":
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
                to_small_caps("channel not detected. either forward a message from the channel, ")
                + to_small_caps("or type the numeric id (-100...).")
            )
            context.user_data["awaiting"] = "activity_channel_id"
            return
        BOT_DATA["settings"]["activity_channel_id"] = chat_id
        BOT_DATA["settings"]["activity_channel_enabled"] = True
        save_data()
        refreshed = await refresh_panel_after_save(context, "activity_channel", _build_adm_activity_channel_view)
        await update.message.reply_text(f"✅ Activity channel set to {chat_id} and enabled." + ("" if refreshed else "\n(reopen the activity panel to confirm)"))

    elif awaiting == "daily_limit":
        context.user_data.pop("awaiting", None)
        if not text.isdigit():
            await update.message.reply_text(to_small_caps("please send a valid number."))
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
        raw = text.strip()
        # Accept either a numeric user id OR an @username in the same
        # prompt, instead of forcing the admin into a separate "link
        # override" flow just to use a username.
        if raw.lstrip("@").isdigit() and not raw.startswith("@"):
            BOT_DATA["settings"]["developer_id"] = int(raw)
            BOT_DATA["settings"]["developer_link"] = None
            save_data()
            note = ""
            try:
                chat = await context.bot.get_chat(int(raw))
                if not chat.username:
                    note = "\n⚠️ " + to_small_caps("this account has no @username — the button may not always open reliably.")
            except Exception:
                note = "\n⚠️ " + to_small_caps("the bot hasn't seen this id yet (the developer has never messaged the bot) — the button won't open reliably until they do, or until you set an @username instead.")
            shown = raw
        else:
            uname = raw.lstrip("@")
            BOT_DATA["settings"]["developer_link"] = f"https://t.me/{uname}"
            BOT_DATA["settings"]["developer_id"] = None
            save_data()
            note = ""
            shown = f"@{uname}"
        refreshed = await refresh_panel_after_save(context, "devsettings", _build_adm_devsettings_view)
        await update.message.reply_text(f"✅ Developer contact set: {shown}{note}" + ("" if refreshed else "\n(reopen the developer panel to confirm)"))

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
            await update.message.reply_text(to_small_caps("group not detected. forward a message from it, or type the numeric id."))
            context.user_data["awaiting"] = "admin_group_id"
            return
        BOT_DATA["settings"]["admin_group_id"] = chat_id
        save_data()
        refreshed = await refresh_panel_after_save(context, "support_settings", _build_adm_support_settings_view)
        await update.message.reply_text(f"✅ Ticket group set to {chat_id}." + ("" if refreshed else "\n(reopen the support panel to confirm)"))

    elif awaiting == "premium_grant_userid":
        context.user_data.pop("awaiting", None)
        parts = text.strip().split()
        if not parts or not parts[0].isdigit():
            await update.message.reply_text(to_small_caps("⚠️ please send a valid numeric user id (e.g. 123456789 or 123456789 90)."))
            context.user_data["awaiting"] = "premium_grant_userid"
            return
        target_uid = parts[0]
        days = 30
        if len(parts) > 1 and parts[1].isdigit() and int(parts[1]) > 0:
            days = int(parts[1])
        grant_premium(target_uid, days)
        refreshed = await refresh_panel_after_save(context, "premium", _build_adm_premium_view)
        await update.message.reply_text(
            f"✅ Premium unlocked for user {target_uid} — {days} days."
            + ("" if refreshed else "\n(reopen the premium panel to confirm)")
        )
        try:
            await context.bot.send_message(
                int(target_uid),
                "🎉 " + to_small_caps("premium has been unlocked for your account!") + f"\n⏳ {to_small_caps('valid for')} {days} {to_small_caps('days')}",
            )
        except Exception:
            pass
        await log_event(context, f"💎 Premium manually granted to {target_uid} ({days}d) by admin {update.effective_user.id}")

    elif awaiting == "plan_step_name":
        name = text.strip()
        if not name:
            await update.message.reply_text(to_small_caps("a blank name won't work. please send the plan's name."))
            return
        context.user_data.setdefault("new_plan", {})["name"] = name
        context.user_data["awaiting"] = "plan_step_days"
        await update.message.reply_text("Step 2/4 — Plan kitne din chalega? (e.g. 30)")

    elif awaiting == "plan_step_days":
        if not text.strip().isdigit() or int(text.strip()) <= 0:
            await update.message.reply_text(to_small_caps("please send a valid number (e.g. 30)."))
            return
        context.user_data["new_plan"]["days"] = int(text.strip())
        context.user_data["awaiting"] = "plan_step_inr"
        await update.message.reply_text(
            to_small_caps("step 3/4 — send the ₹ (inr) price for upi payment.") + " "
            + to_small_caps("if this plan isn't sold via upi, send '0'.")
        )

    elif awaiting == "plan_step_inr":
        cleaned = text.strip().replace("₹", "")
        if not cleaned.isdigit():
            await update.message.reply_text(to_small_caps("please send a valid number (0 is fine if you don't want a upi price)."))
            return
        context.user_data["new_plan"]["price_inr"] = int(cleaned)
        context.user_data["awaiting"] = "plan_step_stars"
        await update.message.reply_text(
            to_small_caps("step 4/4 — send the ⭐ telegram stars price.") + " "
            + to_small_caps("if this plan isn't sold via stars, send '0'.")
        )

    elif awaiting == "plan_step_stars":
        context.user_data.pop("awaiting", None)
        cleaned = text.strip()
        if not cleaned.isdigit():
            await update.message.reply_text(to_small_caps("please send a valid number (0 is fine)."))
            context.user_data["awaiting"] = "plan_step_stars"
            return
        draft = context.user_data.pop("new_plan", {})
        draft["price_stars"] = int(cleaned)
        if not draft.get("price_inr") and not draft.get("price_stars"):
            await update.message.reply_text(
                to_small_caps("⚠️ plan cancelled — at least one price (₹ or ⭐) must be set.") + " "
                + to_small_caps("please try again via ➕ add plan.")
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
                "It's now visible to users in 📊 My Usage (above the Share button)."
                + ("" if refreshed else "\n(reopen the premium panel to confirm)")
            )

    elif awaiting == "reset_all_passcode":
        context.user_data.pop("awaiting", None)
        if text.strip() != RESET_ALL_PASSCODE:
            await update.message.reply_text(
                "❌ " + to_small_caps("wrong passcode — reset cancelled. nothing was touched.")
            )
            return
        await _do_reset_all(update, context)

    elif awaiting == "adm_ban_unban_userid":
        context.user_data.pop("awaiting", None)
        if not text.isdigit():
            await update.message.reply_text(to_small_caps("please send a valid numeric user id."))
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

    elif awaiting == "check_user_id":
        context.user_data.pop("awaiting", None)
        if not text.isdigit():
            await update.message.reply_text(to_small_caps("please send a valid numeric user id."))
            return
        card = await build_user_details_card(context, int(text))
        if card is None:
            await update.message.reply_text(to_small_caps("no record found for this user id."))
            return
        await update.message.reply_text(card, parse_mode="HTML")

    elif awaiting == "message_user_id":
        if not text.isdigit():
            await update.message.reply_text(to_small_caps("please send a valid numeric user id."))
            return
        context.user_data["awaiting"] = "message_user_body"
        context.user_data["message_target"] = int(text)
        await update.message.reply_text(to_small_caps("now send the message you want to deliver to this user."))

    elif awaiting == "message_user_body":
        context.user_data.pop("awaiting", None)
        target = context.user_data.pop("message_target", None)
        if target is None:
            await update.message.reply_text(to_small_caps("something went wrong — please try again."))
            return
        try:
            await context.bot.send_message(chat_id=target, text=text)
            await update.message.reply_text(to_small_caps("✅ message delivered."))
        except Exception as e:  # noqa: BLE001
            log.exception("Admin message delivery failed for target %s", target)
            log_error("message_user", f"target={target} err={e}")
            await update.message.reply_text(USER_ERR_GENERIC)

    elif awaiting == "broadcast_content":
        context.user_data.pop("awaiting", None)
        await do_broadcast(update, context)

    elif awaiting == "btn_step_label":
        flow = context.user_data.get("btn_flow")
        if not flow:
            context.user_data.pop("awaiting", None)
            return
        is_edit = flow.get("edit_idx") is not None
        if not (is_edit and text.strip() == "-"):
            label = text
            if BOT_DATA["settings"].get("small_caps_buttons_default", True):
                label = to_small_caps(label)
            flow["data"]["label"] = label
        context.user_data["awaiting"] = None
        rows = [
            [styled_button("📄 Open Menu", callback_data="btntype:menu")],
            [styled_button("🔗 URL Link", callback_data="btntype:url")],
            [styled_button("⚙️ Run Action", callback_data="btntype:callback")],
            [styled_button("🔀 Toggle Setting", callback_data="btntype:toggle")],
        ]
        if is_edit:
            cur_type = flow["data"].get("type", "?")
            rows.append([styled_button(f"↩️ Keep Current Type ({cur_type})", callback_data="btntype:keep")])
        await update.message.reply_text(to_small_caps("what type of button is this?"), reply_markup=InlineKeyboardMarkup(rows))

    elif awaiting == "btn_step_value":
        flow = context.user_data.get("btn_flow")
        if not flow:
            context.user_data.pop("awaiting", None)
            return
        is_edit = flow.get("edit_idx") is not None
        if not (is_edit and text.strip() == "-"):
            flow["data"]["value"] = text
        context.user_data["awaiting"] = "btn_step_row"
        cur_row = flow["data"].get("row")
        hint = f" (currently row {cur_row} — send - to keep it)" if is_edit and cur_row is not None else ""
        await update.message.reply_text(to_small_caps("which row should this button appear in? (1, 2, 3...)") + hint)

    elif awaiting == "btn_step_row":
        flow = context.user_data.pop("btn_flow", None)
        context.user_data.pop("awaiting", None)
        if not flow:
            return
        is_edit = flow.get("edit_idx") is not None
        keep_row = is_edit and text.strip() == "-"
        if not keep_row:
            if not text.isdigit():
                await update.message.reply_text(to_small_caps("please send a number."))
                context.user_data["btn_flow"] = flow
                context.user_data["awaiting"] = "btn_step_row"
                return
            flow["data"]["row"] = int(text)
        menu_id = flow["menu_id"]
        if is_edit:
            idx = flow["edit_idx"]
            buttons = BOT_DATA["menus"][menu_id]["buttons"]
            if 0 <= idx < len(buttons):
                buttons[idx] = flow["data"]
                save_data()
                await update.message.reply_text(to_small_caps(f"✅ button {idx} updated in '{menu_id}'."))
            else:
                await update.message.reply_text(to_small_caps("that button no longer exists — nothing changed."))
        else:
            BOT_DATA["menus"][menu_id]["buttons"].append(flow["data"])
            save_data()
            await update.message.reply_text(to_small_caps(f"✅ button added to '{menu_id}'."))

    else:
        context.user_data.pop("awaiting", None)
        await update.message.reply_text(to_small_caps("that wasn't understood — please try /admin again."))


async def handle_admin_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    awaiting = context.user_data.get("awaiting")
    user_id = update.effective_user.id
    if not awaiting or not is_admin(user_id):
        return
    # Same chat-cleanup as handle_admin_text_input — the admin's uploaded
    # photo/video used to set a menu image is deleted right after being read.
    await delete_incoming(update)

    if awaiting.startswith("menu_image:") and update.message.photo:
        menu_id = awaiting.split(":", 1)[1]
        context.user_data.pop("awaiting", None)
        file_id = update.message.photo[-1].file_id
        menu = BOT_DATA["menus"][menu_id]
        if len(menu.get("text", "")) > 1024:
            await update.message.reply_text(
                to_small_caps("⚠️ this menu's text is longer than 1024 characters and won't fit as an image caption. ")
                + to_small_caps("please shorten the text first, then add the image.")
            )
            return
        menu["image_file_id"] = file_id
        save_data()
        refreshed = await refresh_panel_after_save(
            context, f"menu_edit:{menu_id}", lambda: _build_menu_edit_screen(menu_id), parse_mode="HTML"
        )
        if not refreshed:
            await update.message.reply_text(to_small_caps(f"✅ '{menu_id}' image updated."))
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


def build_health_text() -> str:
    """Single source of truth for /health, used by both the real command
    and the admin panel's Test Commands screen, so the two never drift out
    of sync. Covers every subsystem the bot actually depends on, not just
    a subset — storage, timing, limits, downloads, premium, and every
    optional dependency that silently degrades instead of crashing."""
    col = get_mongo_collection()
    backend = "MongoDB ✅ connected" if col is not None else (
        f"❌ MongoDB configured but not connected ({_mongo_last_error})" if MONGO_URI
        else "Local JSON file (no MongoDB configured)"
    )
    mem = get_memory_usage_mb()
    s = BOT_DATA["settings"]
    premium_count = sum(1 for uid in BOT_DATA["users"] if is_premium_active(uid))
    recent_errors = [e for e in BOT_DATA.get("error_log", [])
                     if (datetime.utcnow() - datetime.fromisoformat(e["time"])) < timedelta(hours=24)]
    lines = [
        "🩺 " + to_small_caps("health report"),
        "",
        "— " + to_small_caps("system") + " —",
        f"🕒 Server time: {now_ist_str('%d %b %Y, %H:%M:%S')} IST",
        f"⏱ Uptime: {human_uptime()}",
        f"💾 Memory: {mem if mem is not None else 'n/a'} MB",
        f"🐍 python-telegram-bot: {'button-style support ✅' if SUPPORTS_BUTTON_STYLE else 'no button-style support (upgrade to 22.7+)'}",
        f"🎬 ffmpeg: {'✅ ' + FFMPEG_PATH if FFMPEG_AVAILABLE else '⚠️ not found (no-merge fallback in use)'}",
        f"🧾 QR generation: {'✅ styled/local' if QRCODE_STYLED_AVAILABLE else ('✅ plain/local' if QRCODE_AVAILABLE else '⚠️ remote fallback (install qrcode[pil])')}",
        "",
        "— " + to_small_caps("data") + " —",
        f"🗄 Storage: {backend}",
        f"👥 Users: {len(BOT_DATA['users'])} | Groups: {len(BOT_DATA['groups'])} | Admins: {len(BOT_DATA['admins'])}",
        f"💎 Active premium users: {premium_count}",
        f"📢 Broadcasts sent (all-time): {len(BOT_DATA['broadcast_log'])}",
        f"🎫 Open tickets: {sum(1 for t in BOT_DATA.get('tickets', {}).values() if not t.get('closed_at'))}" if isinstance(BOT_DATA.get("tickets"), dict) else "🎫 Open tickets: n/a",
        "",
        "— " + to_small_caps("limits & modes") + " —",
        f"🔒 Maintenance: {'✅ ON' if s.get('maintenance') else '❌ OFF'}",
        f"🎛 Rate limit: {s.get('rate_limit_max')} msgs / {s.get('rate_limit_window_seconds')}s",
        f"📅 Daily free-download limit: {s.get('daily_limit', 20)}",
        "",
        "— " + to_small_caps("last 24h") + " —",
        f"🐞 Errors logged: {len(recent_errors)}" + (" — check 📋 Activity Log" if recent_errors else ""),
    ]
    return "\n".join(lines)


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(build_health_text())


async def cmd_database(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    raw = json.dumps(BOT_DATA, ensure_ascii=False, indent=2).encode("utf-8")
    payload, encrypted = encrypt_backup_bytes(raw)
    filename = "bot_data_backup.json.enc" if encrypted else "bot_data_backup.json"
    path = os.path.join(tempfile.gettempdir(), f"bot_data_export_{int(time.time())}_{filename}")
    with open(path, "wb") as f:
        f.write(payload)
    caption = None
    if encrypted:
        caption = to_small_caps("🔒 encrypted with the bot's local backup.key — restore only works on this same bot instance (or copy backup.key to a new one first).")
    with open(path, "rb") as f:
        await update.message.reply_document(document=f, filename=filename, caption=caption)
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


def _parse_iso(s):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def build_pdf_report_chart_images() -> list:
    """Two matplotlib charts as PNG file paths: (1) new-user growth over
    the last 14 days, built from real per-user 'joined' timestamps —
    there's no separate daily-stats log, so this buckets what's actually
    stored; (2) a bar chart of the lifetime metrics counters. Caller is
    responsible for deleting the returned files."""
    paths = []

    # Chart 1 — daily new users, last 14 days
    days = [(datetime.utcnow().date() - timedelta(days=i)) for i in range(13, -1, -1)]
    counts = {d: 0 for d in days}
    for info in BOT_DATA["users"].values():
        dt = _parse_iso(info.get("joined"))
        if dt and dt.date() in counts:
            counts[dt.date()] += 1
    fig1, ax1 = plt.subplots(figsize=(6, 3))
    ax1.bar([d.strftime("%d %b") for d in days], [counts[d] for d in days], color="#3b82f6")
    ax1.set_title("New Users — Last 14 Days")
    ax1.tick_params(axis="x", rotation=45, labelsize=7)
    fig1.tight_layout()
    p1 = os.path.join(tempfile.gettempdir(), f"chart_growth_{int(time.time())}.png")
    fig1.savefig(p1, dpi=150)
    plt.close(fig1)
    paths.append(p1)

    # Chart 2 — lifetime metrics counters
    metrics = BOT_DATA.get("metrics", {})
    labels = list(metrics.keys())
    values = [metrics[k] for k in labels]
    fig2, ax2 = plt.subplots(figsize=(6, 3))
    ax2.barh(labels, values, color="#10b981")
    ax2.set_title("Lifetime Metrics")
    fig2.tight_layout()
    p2 = os.path.join(tempfile.gettempdir(), f"chart_metrics_{int(time.time())}.png")
    fig2.savefig(p2, dpi=150)
    plt.close(fig2)
    paths.append(p2)

    return paths


def build_pdf_report() -> str:
    """Builds a full PDF report (summary table + the two charts above) and
    returns its file path. Caller deletes the file after sending. Raises
    if PDF_REPORT_AVAILABLE is False — callers should check that first."""
    chart_paths = build_pdf_report_chart_images()
    pdf_path = os.path.join(tempfile.gettempdir(), f"bot_report_{int(time.time())}.pdf")
    doc = SimpleDocTemplate(pdf_path, pagesize=A4, topMargin=1.5 * cm, bottomMargin=1.5 * cm)
    styles = getSampleStyleSheet()
    story = [
        Paragraph("Bot Report", styles["Title"]),
        Paragraph(f"Generated {now_ist_str('%d %b %Y, %H:%M:%S')} IST", styles["Normal"]),
        Spacer(1, 0.5 * cm),
    ]

    s = BOT_DATA["settings"]
    metrics = BOT_DATA.get("metrics", {})
    summary_rows = [
        ["Metric", "Value"],
        ["Total Users", str(len(BOT_DATA["users"]))],
        ["Total Groups", str(len(BOT_DATA["groups"]))],
        ["Total Admins", str(len(BOT_DATA["admins"]))],
        ["Reels Downloaded", str(metrics.get("reels_downloaded", 0))],
        ["Start Count", str(metrics.get("start_count", 0))],
        ["Broadcasts Sent", str(metrics.get("broadcasts_sent", 0))],
        ["Maintenance Mode", "ON" if s.get("maintenance") else "OFF"],
    ]
    table = Table(summary_rows, colWidths=[8 * cm, 6 * cm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), rl_colors.HexColor("#1f2937")),
        ("TEXTCOLOR", (0, 0), (-1, 0), rl_colors.white),
        ("GRID", (0, 0), (-1, -1), 0.5, rl_colors.grey),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [rl_colors.whitesmoke, rl_colors.white]),
    ]))
    story.append(table)
    story.append(Spacer(1, 1 * cm))

    for cp in chart_paths:
        story.append(RLImage(cp, width=14 * cm, height=7 * cm))
        story.append(Spacer(1, 0.5 * cm))

    doc.build(story)
    for cp in chart_paths:
        os.remove(cp)
    return pdf_path


async def cmd_exportpdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """📊 PDF report — summary table + charts, generated fresh each time."""
    if not is_owner(update.effective_user.id):
        return
    if not PDF_REPORT_AVAILABLE:
        await update.message.reply_text(
            to_small_caps("❌ pdf report needs matplotlib + reportlab — run `pip install matplotlib reportlab` and try again.")
        )
        return
    status = await update.message.reply_text("📊 " + to_small_caps("building pdf report..."))
    try:
        pdf_path = await asyncio.to_thread(build_pdf_report)
        with open(pdf_path, "rb") as f:
            await update.message.reply_document(document=f, filename="bot_report.pdf")
        os.remove(pdf_path)
        await status.delete()
    except Exception:
        log_error("unhandled", "/exportpdf failed")
        await status.edit_text("❌ " + to_small_caps("pdf report failed — see the activity log for details."))


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """v2 §12 — latency check, available to everyone. Admins get the full
    picture (uptime, RAM/CPU/disk load, storage backend, server time in
    IST) rendered as a Telegram quote block; regular users just get the
    plain pong, since the extra detail isn't meant for them."""
    t0 = time.monotonic()
    msg = await update.message.reply_text("🏓 Pong!")
    ms = int((time.monotonic() - t0) * 1000)
    if is_admin(update.effective_user.id):
        col = get_mongo_collection()
        backend = "MongoDB ✅" if col is not None else "Local JSON"
        cpu = get_cpu_percent()
        ram = get_ram_percent()
        disk = get_disk_percent()

        lines = [f"↬ {to_title_small_caps('Uptime')} : {human_uptime_full()}"]
        if ram is not None:
            lines.append(f"↬ {to_title_small_caps('RAM')} : {ram:.1f}%")
        if cpu is not None:
            lines.append(f"↬ {to_title_small_caps('CPU')} : {cpu:.1f}%")
        if disk is not None:
            lines.append(f"↬ {to_title_small_caps('Disk')} : {disk:.1f}%")
        lines.append(f"↬ {to_title_small_caps('Storage')} : {to_title_small_caps(backend)}")
        lines.append(f"↬ {to_title_small_caps('Server Time')} : {now_ist_str('%d %b %Y, %H:%M:%S')} IST")

        quote_body = html.escape("\n".join(lines))
        text = f"🏓 <b>{to_title_small_caps('Pong!')}</b> {ms}ms\n\n<blockquote>{quote_body}</blockquote>"
        await msg.edit_text(text, parse_mode="HTML")
    else:
        await msg.edit_text(f"🏓 Pong! {ms}ms")


EXPORT_MAX_BYTES = 49 * 1024 * 1024  # stay under Telegram's ~50MB bot upload cap
EXPORT_EXCLUDE_DIRS = {DOWNLOAD_DIR, BACKUP_DIR, "__pycache__", ".git", ".venv", "venv"}
EXPORT_EXCLUDE_FILES = {DATA_FILE}


def _build_export_zip(src_dir: str) -> str:
    """Zip the bot's source code only — never the downloads/backups/data
    folders, which is what actually made /export unusable before: those
    directories fill up with downloaded videos and JSON backups over time,
    so the old 'zip literally everything' approach routinely built an
    archive well past Telegram's ~50MB bot upload limit and the send would
    just silently fail with no feedback to the admin."""
    import zipfile

    zip_path = os.path.join(tempfile.gettempdir(), f"bot_export_{int(time.time())}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(src_dir):
            dirs[:] = [d for d in dirs if d not in EXPORT_EXCLUDE_DIRS and not d.startswith(".")]
            for fname in files:
                if fname in EXPORT_EXCLUDE_FILES or fname.endswith((".zip", ".pyc")):
                    continue
                full = os.path.join(root, fname)
                arcname = os.path.relpath(full, src_dir)
                try:
                    zf.write(full, arcname)
                except Exception:
                    continue  # skip any file that vanished/locked mid-walk
    return zip_path


def _scan_export_code_stats(src_dir: str):
    """Walk the same file set /export zips (source .py files only, same
    excludes) and return light stats about the codebase: how many files,
    total lines, a rough function/handler count, and the most recent
    modification time across those files (used as the "code last updated"
    timestamp — more meaningful than the export zip's own mtime, which is
    always "just now" since it's freshly built on every run)."""
    total_files = 0
    total_lines = 0
    total_funcs = 0
    latest_mtime = 0.0
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = [d for d in dirs if d not in EXPORT_EXCLUDE_DIRS and not d.startswith(".")]
        for fname in files:
            if not fname.endswith(".py"):
                continue
            full = os.path.join(root, fname)
            try:
                mtime = os.path.getmtime(full)
                latest_mtime = max(latest_mtime, mtime)
                with open(full, "r", encoding="utf-8", errors="ignore") as fh:
                    lines = fh.readlines()
                total_files += 1
                total_lines += len(lines)
                total_funcs += sum(1 for ln in lines if re.match(r"\s*(async\s+)?def\s+\w+\(", ln))
            except Exception:
                continue
    return {
        "files": total_files,
        "lines": total_lines,
        "funcs": total_funcs,
        "last_updated": to_ist(datetime.utcfromtimestamp(latest_mtime)).strftime("%d %b %Y, %H:%M") if latest_mtime else "?",
    }


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner-only — zips the bot's source code (never user downloads,
    backups, or the live data file) and DMs it to the owner. Wrapped
    end-to-end in error handling so a failure always tells the admin what
    went wrong instead of silently doing nothing.

    After the zip is sent, a small follow-up message is also sent with
    when the code was last modified (based on source file mtimes, not the
    zip's own build time) plus a light summary of the codebase (file/line/
    function counts, export size)."""
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("🚫 " + to_small_caps("owner only."))
        return
    status = await update.message.reply_text("📦 " + to_small_caps("building export..."))
    src_dir = os.path.dirname(os.path.abspath(__file__))
    zip_path = None
    try:
        zip_path = _build_export_zip(src_dir)
        size = os.path.getsize(zip_path)
        if size > EXPORT_MAX_BYTES:
            await status.edit_text(
                "⚠️ " + to_small_caps(f"export is {size / 1024 / 1024:.1f}mb — too large for telegram's ~50mb bot upload limit.")
                + "\n" + to_small_caps("try removing unused files from the project folder and run /export again.")
            )
            return
        with open(zip_path, "rb") as f:
            await context.bot.send_document(chat_id=update.effective_user.id, document=f, filename="bot_source.zip")
        try:
            await status.delete()
        except Exception:
            pass
        if update.effective_chat.id != update.effective_user.id:
            await update.message.reply_text("✅ " + to_small_caps("sent to your dm."))
        # Follow-up: last-updated timestamp + light code details, sent as a
        # separate message right under the exported file.
        try:
            stats = _scan_export_code_stats(src_dir)
            details_text = (
                "🗂 " + to_small_caps("code details") + "\n"
                f"🕒 " + to_small_caps("last updated") + f": {stats['last_updated']} IST\n"
                f"📄 " + to_small_caps("files") + f": {stats['files']}\n"
                f"📃 " + to_small_caps("total lines") + f": {stats['lines']}\n"
                f"⚙️ " + to_small_caps("functions") + f": {stats['funcs']}\n"
                f"📦 " + to_small_caps("export size") + f": {size / 1024:.0f} kb"
            )
            await context.bot.send_message(chat_id=update.effective_user.id, text=details_text)
        except Exception:
            # Purely informational — never let this break the actual export.
            log.warning("Could not send export code-details follow-up message", exc_info=True)
    except Forbidden:
        await status.edit_text(
            "⚠️ " + to_small_caps("couldn't dm you the export — start a private chat with the bot first, then try again.")
        )
    except Exception as e:
        log.exception("Export failed")
        log_error("unhandled", f"/export failed: {e}")
        await status.edit_text("❌ " + to_small_caps("export failed — see the activity log for details."))
    finally:
        if zip_path and os.path.exists(zip_path):
            try:
                os.remove(zip_path)
            except Exception:
                pass


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
    fname = (doc.file_name or "").lower() if doc else ""
    if not doc or not (fname.endswith(".json") or fname.endswith(".json.enc")):
        return

    tg_file = await doc.get_file()
    raw_path = os.path.join(tempfile.gettempdir(), f"incoming_{int(time.time())}_{fname}")
    await tg_file.download_to_drive(raw_path)

    try:
        with open(raw_path, "rb") as f:
            file_bytes = f.read()
        if fname.endswith(".json.enc"):
            try:
                file_bytes = decrypt_backup_bytes(file_bytes)
            except ValueError:
                await update.message.reply_text(
                    to_small_caps("❌ this backup is encrypted but the `cryptography` package isn't installed here — run `pip install cryptography` and try again.")
                )
                os.remove(raw_path)
                return
            except InvalidToken:
                await update.message.reply_text(
                    to_small_caps("❌ couldn't decrypt this backup — it was likely made with a different bot's backup.key. restore cancelled.")
                )
                os.remove(raw_path)
                return
        incoming = json.loads(file_bytes.decode("utf-8"))
    except Exception:
        await update.message.reply_text(to_small_caps("❌ this is not a valid backup file."))
        os.remove(raw_path)
        return

    if not set(DEFAULT_DATA.keys()).issubset(set(incoming.keys())):
        await update.message.reply_text(to_small_caps("❌ this doesn't look like a valid backup file. restore cancelled."))
        os.remove(raw_path)
        return

    context.user_data["pending_restore"] = incoming
    os.remove(raw_path)

    cur_users, new_users = len(BOT_DATA["users"]), len(incoming.get("users", {}))
    cur_admins, new_admins = len(BOT_DATA["admins"]), len(incoming.get("admins", []))
    text = (
        to_small_caps("⚠️ restore confirmation") + "\n\n"
        + f"{to_small_caps('users')}: {cur_users} → {new_users}\n{to_small_caps('admins')}: {cur_admins} → {new_admins}\n\n"
        + to_small_caps("⚠️ this will completely REPLACE the current live data.") + "\n" + to_small_caps("(the current data will be backed up first.)")
    )
    kb = InlineKeyboardMarkup(
        [[styled_button("✅ Confirm Restore", callback_data="restore_confirm"),
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
        await query.edit_message_text(to_small_caps("this has expired — please send the file again."))
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
    await query.edit_message_text(to_small_caps("restore cancelled."))


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

async def cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Friendly fallback for every unregistered /unknown command."""
    user = update.effective_user
    if user and is_blocked(user.id) and not is_admin(user.id):
        return
    if user and BOT_DATA["settings"].get("maintenance") and not is_admin(user.id):
        await send_maintenance_notice(context, update.effective_chat.id)
        return
    await update.message.reply_text(
        "Hey that command format doesn't work\nJust send /start 🚀"
    )


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command equivalent of the Broadcast Centre's New Broadcast button."""
    if not is_admin(update.effective_user.id):
        return
    context.user_data["awaiting"] = "broadcast_content"
    await update.message.reply_text(
        to_small_caps("📢 send your broadcast now") + "\n"
        + to_small_caps("text, photo or video — one single message") + "\n\n"
        + to_small_caps("it will be delivered to every registered user and this admin chat, respecting your forward-lock setting")
    )


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
    update_type = type(update).__name__ if update else "unknown"
    err_text = str(context.error)
    # A duplicate-instance getUpdates conflict is common and has a very
    # different (and non-code) fix from a generic bug, so it gets its own
    # Activity Log category instead of being lumped under "unhandled".
    kind = "conflict" if "Conflict" in err_text and "getUpdates" in err_text else "unhandled"
    log_error(kind, f"[{update_type}] {err_text}")
    save_data()
    try:
        await log_event(context, f"🐞 Error: {str(context.error)[:300]}")
    except Exception:
        pass

    # Urgent/critical errors go straight to every admin's DM, unconditionally
    # — this does NOT check the Logger Channel toggle (log_event above does),
    # so an admin who never bothered configuring a logger channel still finds
    # out immediately when something actually breaks, instead of it only
    # showing up next time they happen to open Activity Log.
    if kind == "unhandled":
        try:
            await dm_all_admins(
                context,
                "🚨 " + to_title_small_caps("Urgent Alert") + "\n\n"
                + f"{to_title_small_caps('Type')} : {html.escape(update_type)}\n"
                + f"{to_title_small_caps('Error')} : {html.escape(err_text[:300])}",
                parse_mode="HTML",
            )
        except Exception:
            pass


SCREEN_RENDERERS.update(
    {
        "adm_home": _render_adm_home,
        "adm_stats": _render_adm_stats,
        "adm_users": _render_adm_users,
        "adm_live": _render_adm_live,
        "adm_broadcast": _render_adm_broadcast,
        "adm_bc_delmenu": _render_adm_bc_delmenu,
        "adm_menu_ui": _render_adm_menu_ui,
        "adm_settings": _render_adm_settings,
        "adm_maintenance": _render_adm_maintenance,
        "adm_danger": _render_adm_danger,
        "adm_owner_contact": _render_adm_owner_contact,
        "adm_logger_channel": _render_adm_logger_channel,
        "adm_activity_channel": _render_adm_activity_channel,
        "adm_force_join": _render_adm_force_join,
        "adm_premium": _render_adm_premium,
        "adm_upi": _render_adm_upi,
        "adm_devsettings": _render_adm_devsettings,
        "adm_support_settings": _render_adm_support_settings,
        "adm_tickets": _render_adm_tickets,
        "adm_leaderboard": _render_adm_leaderboard,
        "adm_share": _render_adm_share,
        "adm_cmdtest": _render_adm_cmdtest,
        "adm_activity": _render_adm_activity,
        "adm_selftest": _render_adm_selftest,
        "adm_notifications": _render_adm_notifications,
    }
)


# ----------------------------------------------------------------------------
# 🧩 Feature Plugins — drop a .py file into plugins/ and its features load
# straight into the bot, without editing bot.py at all.
#
# HOW TO WRITE A PLUGIN (share this with whoever is building the feature):
#   1. Create a new file, e.g. plugins/my_feature.py
#   2. It must define one function: `def register(app):`
#   3. Inside register(), add handlers exactly like in bot.py, e.g.:
#
#        from __main__ import (
#            styled_button, is_premium_active, is_admin, to_small_caps,
#            require_premium, BOT_DATA, log_error,
#        )
#        from telegram.ext import CommandHandler
#
#        async def my_cool_feature(update, context):
#            await update.message.reply_text("Hello from a plugin!")
#
#        def register(app):
#            app.add_handler(CommandHandler("mycommand", my_cool_feature))
#
#   4. To make a feature Premium-only, wrap the handler with the
#      require_premium() decorator (see below) — it automatically blocks
#      non-premium users and shows them the upgrade menu, same as every
#      built-in premium feature.
#   5. Save the file into the bot's plugins/ folder and RESTART the bot —
#      plugin files are only scanned once, at startup, so a running bot
#      won't pick up a newly uploaded file until it's restarted.
#
# A broken plugin (syntax error, missing register(), exception while
# loading) is skipped and logged to the Activity Log — it can never crash
# the rest of the bot or stop other plugins from loading.
# ----------------------------------------------------------------------------

def require_premium(handler):
    """Decorator for plugin (or future core) handlers that should only run
    for active premium users. Non-premium users get the same 🎁 upgrade
    prompt used everywhere else in the bot, instead of the feature silently
    doing nothing or erroring."""
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *a, **kw):
        uid = str(update.effective_user.id)
        if is_premium_active(uid) or is_admin(update.effective_user.id):
            return await handler(update, context, *a, **kw)
        text = "💎 " + to_small_caps("this feature is for premium users only.")
        kb = InlineKeyboardMarkup([[styled_button("🎁 Upgrade to Premium", callback_data="gift_menu")]])
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.message.reply_text(text, reply_markup=kb)
        elif update.message:
            await update.message.reply_text(text, reply_markup=kb)
    return wrapped


_LOADED_PLUGINS = []   # [{"file": name, "ok": bool, "error": str|None}]


def load_plugins(app: Application) -> None:
    """Scans plugins/ once at startup and registers every valid plugin
    found. Never lets one bad file take down the others or the bot."""
    global _LOADED_PLUGINS
    _LOADED_PLUGINS = []
    if not os.path.isdir(PLUGIN_DIR):
        return
    import importlib.util
    for fname in sorted(os.listdir(PLUGIN_DIR)):
        if not fname.endswith(".py") or fname.startswith("_"):
            continue
        path = os.path.join(PLUGIN_DIR, fname)
        try:
            spec = importlib.util.spec_from_file_location(f"plugins.{fname[:-3]}", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if not hasattr(module, "register"):
                raise AttributeError("plugin file has no register(app) function")
            module.register(app)
            _LOADED_PLUGINS.append({"file": fname, "ok": True, "error": None})
            log.info("Loaded plugin: %s", fname)
        except Exception as e:
            log.exception("Failed to load plugin %s", fname)
            log_error("plugin", f"{fname}: {e}")
            _LOADED_PLUGINS.append({"file": fname, "ok": False, "error": str(e)[:200]})


async def _render_adm_plugins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not _LOADED_PLUGINS:
        body = (
            "🧩 " + to_small_caps("feature plugins") + "\n\n"
            + to_small_caps(f"no plugin files found in '{PLUGIN_DIR}/'.") + "\n\n"
            + to_small_caps("to add a feature: drop a .py file into that folder (with a register(app) function) and restart the bot.")
        )
    else:
        lines = ["🧩 " + to_small_caps("feature plugins") + "\n"]
        for p in _LOADED_PLUGINS:
            if p["ok"]:
                lines.append(f"✅ {p['file']}")
            else:
                lines.append(f"❌ {p['file']} — {p['error']}")
        lines.append("")
        lines.append(to_small_caps("uploaded a new file? restart the bot to load it — plugins are only scanned at startup."))
        body = "\n".join(lines)
    kb = InlineKeyboardMarkup([back_row(), home_row()])
    await query.edit_message_text(body, reply_markup=kb)


async def cb_adm_plugins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    if not is_admin(update.effective_user.id):
        return
    await _render_adm_plugins(update, context)


# BUGFIX — this must be registered here, AFTER _render_adm_plugins is
# defined, not inside the big SCREEN_RENDERERS.update({...}) block far
# above (which runs at module import time, before this function existed
# yet) — that ordering caused a hard NameError crash on every startup.
SCREEN_RENDERERS["adm_plugins"] = _render_adm_plugins


def build_app() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Group -1 runs BEFORE every handler added below (group 0, the default).
    # This is the actual enforcement point for "no button works until
    # agree + join" — see cb_global_button_gate's docstring.
    app.add_handler(CallbackQueryHandler(cb_global_button_gate), group=-1)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("language", cmd_language))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("dbstatus", cmd_dbstatus))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("database", cmd_database))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("exportusers", cmd_exportusers))
    app.add_handler(CommandHandler("exportpdf", cmd_exportpdf))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("block", cmd_block))
    app.add_handler(CommandHandler("unblock", cmd_unblock))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))

    app.add_handler(CallbackQueryHandler(cb_agree_terms, pattern="^agree_terms$"))
    app.add_handler(CallbackQueryHandler(cb_report_copyright, pattern="^report_copyright$"))
    app.add_handler(CallbackQueryHandler(cb_support_start, pattern="^support_start$"))
    app.add_handler(CallbackQueryHandler(cb_adm_block_link, pattern="^adm_block_link:"))
    app.add_handler(CallbackQueryHandler(cb_adm_block_domain, pattern="^adm_block_domain:"))
    app.add_handler(CallbackQueryHandler(cb_adm_owner_contact_set, pattern="^adm_owner_contact_set$"))
    app.add_handler(CallbackQueryHandler(cb_adm_owner_contact_clear, pattern="^adm_owner_contact_clear$"))
    app.add_handler(CallbackQueryHandler(cb_adm_logger_channel_set, pattern="^adm_logger_channel_set$"))
    app.add_handler(CallbackQueryHandler(cb_adm_activity_channel_set, pattern="^adm_activity_channel_set$"))
    app.add_handler(CallbackQueryHandler(nav_tracked("adm_force_join")(cb_adm_force_join), pattern="^adm_force_join$"))
    app.add_handler(CallbackQueryHandler(nav_tracked("adm_leaderboard")(cb_adm_leaderboard), pattern="^adm_leaderboard$"))
    app.add_handler(CallbackQueryHandler(cb_adm_post_leaderboard, pattern="^adm_post_leaderboard$"))
    app.add_handler(CallbackQueryHandler(nav_tracked("adm_share")(cb_adm_share), pattern="^adm_share$"))
    app.add_handler(CallbackQueryHandler(cb_adm_share_url_set, pattern="^adm_share_url_set$"))
    app.add_handler(CallbackQueryHandler(cb_adm_share_url_clear, pattern="^adm_share_url_clear$"))
    app.add_handler(CallbackQueryHandler(cb_adm_force_join_set, pattern="^adm_force_join_set$"))
    app.add_handler(CallbackQueryHandler(cb_adm_force_join_clear, pattern="^adm_force_join_clear$"))
    app.add_handler(CallbackQueryHandler(cb_adm_force_join_remove, pattern="^adm_force_join_remove$"))
    app.add_handler(CallbackQueryHandler(cb_adm_force_join_test, pattern="^adm_force_join_test$"))

    # Reel action buttons
    app.add_handler(CallbackQueryHandler(cb_get_caption, pattern="^get_caption$"))
    app.add_handler(CallbackQueryHandler(cb_copy_caption, pattern="^copy_caption$"))
    app.add_handler(CallbackQueryHandler(cb_get_audio, pattern="^get_audio$"))
    app.add_handler(CallbackQueryHandler(cb_remove_reel, pattern="^remove_reel$"))
    app.add_handler(CallbackQueryHandler(cb_download_another, pattern="^download_another$"))
    app.add_handler(CallbackQueryHandler(cb_check_force_join, pattern="^check_force_join$"))
    app.add_handler(ChatMemberHandler(cm_track_groups, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(ChatJoinRequestHandler(handle_force_join_request))

    app.add_handler(CallbackQueryHandler(cb_ticket_close, pattern="^tk_close:"))
    app.add_handler(CallbackQueryHandler(cb_ticket_reopen, pattern="^tk_reopen:"))
    app.add_handler(CallbackQueryHandler(cb_support_resolve, pattern="^sup_resolve:"))

    app.add_handler(CallbackQueryHandler(cb_gift_menu, pattern="^gift_menu$"))
    app.add_handler(CallbackQueryHandler(cb_gift_plan, pattern="^gift_plan:"))
    app.add_handler(CallbackQueryHandler(cb_gift_plan_stars, pattern="^gift_plan_stars:"))
    app.add_handler(CallbackQueryHandler(cb_gift_plan_upi, pattern="^gift_plan_upi:"))
    app.add_handler(CallbackQueryHandler(cb_gift_stars, pattern="^gift_stars$"))
    app.add_handler(CallbackQueryHandler(cb_gift_stars_custom, pattern="^gift_stars_custom$"))
    app.add_handler(CallbackQueryHandler(cb_gift_stars_amount, pattern="^gift_stars_amt:"))
    app.add_handler(CallbackQueryHandler(cb_gift_dismiss, pattern="^gift_dismiss$"))
    app.add_handler(CallbackQueryHandler(cb_view_leaderboard, pattern="^view_leaderboard$"))
    app.add_handler(CallbackQueryHandler(cb_gift_upi, pattern="^gift_upi$"))
    app.add_handler(CallbackQueryHandler(cb_gift_upi_paid, pattern="^gift_upi_paid:"))
    app.add_handler(CallbackQueryHandler(cb_gift_upi_confirm, pattern="^gift_upi_confirm:"))
    app.add_handler(CallbackQueryHandler(cb_gift_upi_decline, pattern="^gift_upi_decline:"))
    app.add_handler(PreCheckoutQueryHandler(cmd_precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, cmd_successful_payment))
    app.add_handler(CallbackQueryHandler(cb_nav, pattern="^nav:"))
    app.add_handler(CallbackQueryHandler(cb_toggle_menu_button, pattern="^tgl:"))
    app.add_handler(CallbackQueryHandler(cb_settings_toggle, pattern="^stgl:"))
    app.add_handler(CallbackQueryHandler(cb_styleset, pattern="^styleset:"))
    app.add_handler(CallbackQueryHandler(cb_noop, pattern="^noop$"))

    app.add_handler(CallbackQueryHandler(cb_adm_home, pattern="^adm_home$"))
    app.add_handler(CallbackQueryHandler(nav_tracked("adm_activity")(cb_adm_activity), pattern="^adm_activity$"))
    app.add_handler(CallbackQueryHandler(cb_adm_clear_activity, pattern="^adm_clear_activity$"))
    app.add_handler(CallbackQueryHandler(cb_adm_fix_now, pattern="^adm_fix:"))
    app.add_handler(CallbackQueryHandler(nav_tracked("adm_selftest")(cb_adm_selftest), pattern="^adm_selftest$"))
    app.add_handler(CallbackQueryHandler(nav_tracked("adm_plugins")(cb_adm_plugins), pattern="^adm_plugins$"))
    app.add_handler(CallbackQueryHandler(nav_tracked("adm_cmdtest")(cb_adm_cmdtest), pattern="^adm_cmdtest$"))
    app.add_handler(CallbackQueryHandler(cb_run_cmd, pattern="^run_cmd:"))
    app.add_handler(CallbackQueryHandler(cb_adm_back, pattern="^adm_back$"))
    app.add_handler(CallbackQueryHandler(nav_tracked("adm_stats")(cb_adm_stats), pattern="^adm_stats$"))
    app.add_handler(CallbackQueryHandler(nav_tracked("adm_notifications")(cb_adm_notifications), pattern="^adm_notifications$"))
    app.add_handler(CallbackQueryHandler(nav_tracked("adm_users")(cb_adm_users), pattern="^adm_users$"))
    app.add_handler(CallbackQueryHandler(cb_adm_users_list, pattern="^adm_users_list$"))
    app.add_handler(CallbackQueryHandler(cb_adm_groups_list, pattern="^adm_groups_list$"))
    app.add_handler(CallbackQueryHandler(cb_adm_users_msg, pattern="^adm_users_msg$"))
    app.add_handler(CallbackQueryHandler(cb_adm_check_user, pattern="^adm_check_user$"))
    app.add_handler(CallbackQueryHandler(nav_tracked("adm_live")(cb_adm_live), pattern="^adm_live$"))
    app.add_handler(CallbackQueryHandler(cb_adm_quickban, pattern="^adm_quickban$"))
    app.add_handler(CallbackQueryHandler(nav_tracked("adm_broadcast")(cb_adm_broadcast), pattern="^adm_broadcast$"))
    app.add_handler(CallbackQueryHandler(cb_adm_start_broadcast, pattern="^adm_start_broadcast$"))
    app.add_handler(CallbackQueryHandler(cb_adm_start_broadcast_confirm, pattern="^adm_start_broadcast_confirm$"))
    app.add_handler(CallbackQueryHandler(cb_adm_bc_new, pattern="^adm_bc_new$"))
    app.add_handler(CallbackQueryHandler(cb_adm_bc_log, pattern="^adm_bc_log$"))
    app.add_handler(CallbackQueryHandler(nav_tracked("adm_bc_delmenu")(cb_adm_bc_delmenu), pattern="^adm_bc_delmenu$"))
    app.add_handler(CallbackQueryHandler(cb_adm_bc_delmonth, pattern="^adm_bc_delmonth:"))
    app.add_handler(CallbackQueryHandler(cb_adm_bc_delconfirm, pattern="^adm_bc_delconfirm:"))
    app.add_handler(CallbackQueryHandler(cb_adm_bc_deldo, pattern="^adm_bc_deldo:"))

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
    app.add_handler(CallbackQueryHandler(cb_maint_notify_me, pattern="^maint_notify_me$"))
    app.add_handler(CallbackQueryHandler(cb_maint_notify_me_done, pattern="^maint_notify_me_done$"))
    app.add_handler(CallbackQueryHandler(cb_adm_btn_add, pattern="^adm_btn_add:"))
    app.add_handler(CallbackQueryHandler(cb_adm_btn_edit, pattern="^adm_btn_edit:"))
    app.add_handler(CallbackQueryHandler(cb_adm_btn_del, pattern="^adm_btn_del:"))
    app.add_handler(CallbackQueryHandler(cb_adm_btn_style, pattern="^adm_btn_style:"))
    app.add_handler(CallbackQueryHandler(cb_btn_type_pick, pattern="^btntype:"))

    app.add_handler(CallbackQueryHandler(nav_tracked("adm_settings")(cb_adm_settings), pattern="^adm_settings$"))
    app.add_handler(CallbackQueryHandler(cb_adm_set_premium_emoji, pattern="^adm_set_premium_emoji$"))
    app.add_handler(CallbackQueryHandler(nav_tracked("adm_maintenance")(cb_adm_maintenance), pattern="^adm_maintenance$"))
    app.add_handler(CallbackQueryHandler(cb_adm_maint_setmsg, pattern="^adm_maint_setmsg$"))
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
    app.add_handler(CallbackQueryHandler(nav_tracked("adm_activity_channel")(cb_adm_activity_channel), pattern="^adm_activity_channel$"))

    app.add_handler(CallbackQueryHandler(nav_tracked("adm_premium")(cb_adm_premium), pattern="^adm_premium$"))
    app.add_handler(CallbackQueryHandler(cb_adm_premium_users, pattern="^adm_premium_users$"))
    app.add_handler(CallbackQueryHandler(cb_adm_premium_grant, pattern="^adm_premium_grant$"))
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

    # Load any drop-in feature files from plugins/ LAST, so a plugin can
    # never conflict with or shadow a built-in command/callback pattern
    # registered above.
    load_plugins(app)

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
# - Menu version history (#14) — undo/rollback per-menu edits. Encrypted
#   backups + multi-language menus (rest of #14) are now done, see below.
# ------------------------------------------------------------------------
#
# ------------------------------------------------------------------------
# Follow-up pass — the 4 previously-skipped items, completed:
#
# 1. 📊 PDF Report — new /exportpdf command + a "📊 /exportpdf" button in
#    🧪 Test Commands. Builds two matplotlib charts (14-day new-user
#    growth from real per-user `joined` timestamps, and a lifetime-metrics
#    bar chart) and lays them out in a reportlab PDF with a summary table.
#    Degrades to a clear "install matplotlib+reportlab" message if those
#    packages aren't present — never crashes the bot.
# 2. ✏️ Button edit-in-place — every custom menu button in Admin Panel >
#    Menu & UI > (menu) > Buttons now has an ✏️ next to it. Editing reuses
#    the same label → type → value → row flow as Add Button, except each
#    step accepts "-" to keep that field exactly as it was, so a single
#    typo no longer means delete + re-add.
# 3. 🔒 Encrypted backups — /database, the admin-panel backup button, and
#    the scheduled backup job all now encrypt with a local Fernet key
#    (auto-generated once as `backup.key`, never written into the /export
#    source zip). Restore accepts both plain .json and .json.enc, and
#    gives a specific error if the key doesn't match or `cryptography`
#    isn't installed — never a silent failure.
# 4. 🌟 Premium custom emoji — Settings > "✏️ Set Premium Emoji" lets the
#    owner send one message containing a real Telegram Premium custom
#    emoji; the bot reads its `custom_emoji_id` off the message entities
#    and stores it. Turning on "🌟 Premium Emoji Greeting" sends that
#    emoji + a small-caps welcome line right before every /start menu.
#    Kept as its own entities-based message (no HTML) so it can never
#    collide with the HTML parse_mode used elsewhere. Non-Premium viewers
#    automatically see the fallback glyph captured from the same message —
#    that substitution is Telegram's own client behaviour, not this bot's.
#
# Multi-language menus were intentionally left alone this pass — already
# maintained separately outside this file, per instruction.
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
# 8. /export now sends a follow-up message right after the zip: when the
#    code was last modified (latest .py file mtime, not the zip's own
#    build time) plus a light summary — file count, total lines, function
#    count, export size. Purely informational; wrapped so a failure here
#    can never break the actual export.
# ------------------------------------------------------------------------
