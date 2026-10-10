import os
import asyncio
import time
import requests
import subprocess
import logging
from pathlib import Path
from telegram import Update, Bot
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from flask import Flask
from threading import Thread

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ── ENV ──────────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ["BOT_TOKEN"]
BASE_URL    = os.environ.get("STUDYBEE_BASE", "https://vidya.studybeepro.site")
QUALITY     = os.environ.get("QUALITY", "720p")
COOKIE_STR  = os.environ.get("STUDYBEE_COOKIE", "")
# ─────────────────────────────────────────────────────────────────────────────

# ── Hardcoded auth (from token login) ────────────────────────────────────────
HARDCODED_TOKEN  = os.environ.get("STUDYBEE_TOKEN", "")
HARDCODED_USERID = os.environ.get("STUDYBEE_USERID", "")
# ─────────────────────────────────────────────────────────────────────────────

ASK_COURSE, ASK_TARGET = range(2)   # no cookie step — auth is baked in

HEADERS = {
    "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer":      BASE_URL + "/",
    "Origin":       BASE_URL,
}


# ── HTTP session with JWT auth ────────────────────────────────────────────────
def make_session(token: str = "", userid: str = "") -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)

    tok = token or HARDCODED_TOKEN
    uid = userid or HARDCODED_USERID

    if tok:
        s.headers["Authorization"] = f"Bearer {tok}"
        s.headers["token"]         = tok
    if uid:
        s.headers["userid"]        = uid
        s.headers["user-id"]       = uid

    # also inject as cookies — site uses both
    if tok:
        s.cookies.set("token",  tok)
    if uid:
        s.cookies.set("userid", uid)

    # extra cookie string if still needed
    if COOKIE_STR:
        for part in COOKIE_STR.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                s.cookies.set(k.strip(), v.strip())
    return s

http = make_session()


# ── Course API ────────────────────────────────────────────────────────────────
def fetch_folder(course_id: int, parent_id: int = -1, session: requests.Session = None) -> list[dict]:
    """
    Recursively list all videos + PDFs in a course.
    The API works like this:
      parent_id=-1  → returns TOP-LEVEL folders (e.g. 'Home' folder with id=29440)
      parent_id=29440 → returns children of Home (subfolders or actual videos)
    So we recurse by passing item['id'] as the next parent_id.
    """
    s = session or http
    url = f"{BASE_URL}/get/folder_contentsv3"
    params = {"course_id": course_id, "parent_id": parent_id, "start": 0}
    try:
        r = s.get(url, params=params, timeout=30)
        r.raise_for_status()
        items = r.json().get("data", [])
    except Exception as e:
        log.error(f"fetch_folder error (parent_id={parent_id}): {e}")
        return []

    results = []
    for item in items:
        item_id   = int(item.get("id", 0))
        mat_type  = item.get("material_type", "")
        if mat_type == "FOLDER":
            # only recurse if this folder isn't the same as current parent (avoid loops)
            if item_id != parent_id:
                log.info(f"  → entering folder: {item.get('Title')} (id={item_id})")
                results.extend(fetch_folder(course_id, item_id, s))
        else:
            results.append(item)
    return results


def get_stream_url(course_id: int, video_id: str, quality: str = QUALITY, session: requests.Session = None) -> str | None:
    s = session or http
    url = f"{BASE_URL}/playx"
    params = {"course_id": course_id, "video_id": video_id}
    try:
        r = s.get(url, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            return None
        for q in data.get("qualities", []):
            if q["quality"] == quality:
                return q["url"]
        # fallback quality order
        for fallback in ["720p", "480p", "360p", "240p", "1080p"]:
            for q in data.get("qualities", []):
                if q["quality"] == fallback:
                    return q["url"]
        return data.get("decoded_video_link")
    except Exception as e:
        log.error(f"playx error: {e}")
        return None


# ── Helpers ───────────────────────────────────────────────────────────────────
def sanitize(name: str) -> str:
    return "".join(c if c.isalnum() or c in " ._-" else "_" for c in name).strip()[:80]


def hms(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"


async def safe_send(bot: Bot, chat_id: str, text: str, **kw):
    """Send with retry on flood wait."""
    for attempt in range(5):
        try:
            return await bot.send_message(chat_id=chat_id, text=text, **kw)
        except TelegramError as e:
            if "Flood" in str(e) or "retry" in str(e).lower():
                await asyncio.sleep(5 * (attempt + 1))
            else:
                raise


async def safe_send_video(bot: Bot, chat_id: str, path: Path, caption: str):
    for attempt in range(5):
        try:
            with open(path, "rb") as f:
                await bot.send_video(
                    chat_id=chat_id,
                    video=f,
                    caption=caption[:1024],
                    supports_streaming=True,
                    read_timeout=300,
                    write_timeout=300,
                    connect_timeout=60,
                )
            return True
        except TelegramError as e:
            if "Flood" in str(e) or "retry" in str(e).lower():
                await asyncio.sleep(10 * (attempt + 1))
            elif "file is too big" in str(e).lower():
                return False  # skip oversized
            else:
                log.error(f"send_video error: {e}")
                return False
    return False


async def safe_send_document(bot: Bot, chat_id: str, path: Path, caption: str):
    for attempt in range(5):
        try:
            with open(path, "rb") as f:
                await bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    caption=caption[:1024],
                    read_timeout=120,
                    write_timeout=120,
                )
            return True
        except TelegramError as e:
            if "Flood" in str(e) or "retry" in str(e).lower():
                await asyncio.sleep(10 * (attempt + 1))
            else:
                log.error(f"send_document error: {e}")
                return False
    return False


# ── Download worker ───────────────────────────────────────────────────────────
def download_hls(m3u8_url: str, out_path: Path) -> bool:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-loglevel", "error",
        "-i", m3u8_url,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        str(out_path)
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=1800)
    return result.returncode == 0


def download_pdf(url: str, out_path: Path) -> bool:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = http.get(url, stream=True, timeout=60)
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        return True
    except Exception as e:
        log.error(f"PDF download error: {e}")
        return False


# ── Main pipeline (runs in executor so bot stays async) ───────────────────────
async def run_pipeline(
    bot: Bot,
    requester_id: int,
    course_id: int,
    target_chat: str,
    cookie_str: str = "",   # kept for compat, unused now
):
    status_chat = requester_id
    tmp_dir = Path(f"/tmp/course_{course_id}_{int(time.time())}")
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # use global session with baked-in token/userid
    sess = http

    await safe_send(bot, status_chat,
        f"🔍 Extracting course <b>{course_id}</b>...\n"
        f"(recursing through all folders, may take a moment)",
        parse_mode=ParseMode.HTML)

    loop = asyncio.get_event_loop()
    try:
        items = await loop.run_in_executor(None, lambda: fetch_folder(course_id, -1, sess))
    except Exception as e:
        await safe_send(bot, status_chat, f"❌ Failed to fetch course: {e}")
        return

    if not items:
        await safe_send(bot, status_chat,
            "❌ No items found. Possible reasons:\n"
            "• Wrong course ID\n"
            "• Cookie expired / not set\n"
            "• Course has no direct video items (only sub-folders)\n\n"
            "Try /download again with fresh cookies.")
        return

    videos = [i for i in items if i.get("material_type") == "VIDEO"]
    pdfs   = [i for i in items if i.get("material_type") in ("PDF", "NOTES")]
    total  = len(videos) + len(pdfs)

    await safe_send(
        bot, status_chat,
        f"📦 Found <b>{len(videos)}</b> videos + <b>{len(pdfs)}</b> PDFs\n"
        f"🚀 Sending to <code>{target_chat}</code>",
        parse_mode=ParseMode.HTML
    )

    done = 0
    failed = 0
    start_time = time.time()

    # ── Videos ──
    for idx, item in enumerate(videos, 1):
        title    = sanitize(item.get("Title", f"video_{idx}"))
        video_id = str(item.get("video_id") or item.get("id") or "")
        out_path = tmp_dir / f"{idx:03d}_{title}.mp4"

        if not video_id:
            failed += 1
            continue

        await safe_send(
            bot, status_chat,
            f"⬇ [{idx}/{len(videos)}] <b>{title}</b>",
            parse_mode=ParseMode.HTML
        )

        m3u8 = await loop.run_in_executor(
            None, lambda vid=video_id: get_stream_url(course_id, vid, QUALITY, sess)
        )
        if not m3u8:
            await safe_send(bot, status_chat, f"⚠ No stream for: {title}")
            failed += 1
            continue

        ok = await loop.run_in_executor(None, download_hls, m3u8, out_path)

        if not ok or not out_path.exists():
            await safe_send(bot, status_chat, f"⚠ ffmpeg failed: {title}")
            failed += 1
            continue

        size_mb = out_path.stat().st_size / 1_048_576
        caption  = f"📹 {idx}. {title}\n📦 {size_mb:.1f} MB | Course {course_id}"

        sent = await safe_send_video(bot, target_chat, out_path, caption)
        if not sent:
            sent = await safe_send_document(bot, target_chat, out_path, caption)

        out_path.unlink(missing_ok=True)

        if sent:
            done += 1
        else:
            failed += 1
            await safe_send(bot, status_chat, f"⚠ Upload failed: {title}")

        elapsed = time.time() - start_time
        eta_secs = (elapsed / idx) * (len(videos) - idx) if idx else 0
        await safe_send(
            bot, status_chat,
            f"✅ {done}/{total} done | ❌ {failed} failed | ⏱ ETA ~{hms(eta_secs)}",
            parse_mode=ParseMode.HTML
        )
        await asyncio.sleep(1)

    # ── PDFs ──
    for idx, item in enumerate(pdfs, 1):
        title   = sanitize(item.get("Title", f"pdf_{idx}"))
        pdf_url = item.get("pdf_link") or item.get("study_material_link") or item.get("pdf_link2")
        out_path = tmp_dir / f"pdf_{idx:03d}_{title}.pdf"

        if not pdf_url:
            failed += 1
            continue

        ok = await loop.run_in_executor(None, download_pdf, pdf_url, out_path)

        if ok:
            caption = f"📄 {title} | Course {course_id}"
            sent = await safe_send_document(bot, target_chat, out_path, caption)
            out_path.unlink(missing_ok=True)
            done += 1 if sent else 0
            failed += 0 if sent else 1
        else:
            failed += 1

    try:
        tmp_dir.rmdir()
    except Exception:
        pass

    elapsed = time.time() - start_time
    await safe_send(
        bot, status_chat,
        f"🎉 <b>Course {course_id} complete!</b>\n"
        f"✅ Sent: {done} | ❌ Failed: {failed} | ⏱ Total: {hms(elapsed)}",
        parse_mode=ParseMode.HTML
    )


# ── Conversation handlers ─────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>Course Extractor Bot</b>\n\n"
        "Send /download to start extracting a course.",
        parse_mode=ParseMode.HTML
    )


async def download_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    auth_ok = "✅" if (HARDCODED_TOKEN or COOKIE_STR) else "⚠ no auth set"
    await update.message.reply_text(
        f"📚 <b>Step 1/2</b> — Enter the <b>Course ID</b>:\n"
        f"Example: <code>41</code>\n\n"
        f"🔐 Auth: {auth_ok}",
        parse_mode=ParseMode.HTML
    )
    return ASK_COURSE


async def got_course_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("⚠ That's not a valid number. Send the Course ID again:")
        return ASK_COURSE

    ctx.user_data["course_id"] = int(text)
    await update.message.reply_text(
        f"✅ Course ID: <b>{text}</b>\n\n"
        "📨 <b>Step 2/2</b> — Enter the <b>Target Group/Channel ID</b>:\n"
        "Example: <code>-1001234567890</code> or <code>@mychannel</code>\n\n"
        "⚠ Bot must be <b>admin</b> in that group/channel!",
        parse_mode=ParseMode.HTML
    )
    return ASK_TARGET


async def got_target(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    target    = update.message.text.strip()
    course_id = ctx.user_data.get("course_id")

    await update.message.reply_text(
        f"🚀 <b>Starting!</b>\n"
        f"📚 Course: <code>{course_id}</code>\n"
        f"📤 Channel: <code>{target}</code>\n"
        f"🔐 Auth: {'✅ token set' if HARDCODED_TOKEN else '⚠ no token'}\n\n"
        f"Progress updates here. Go grab a coffee ☕",
        parse_mode=ParseMode.HTML
    )

    asyncio.create_task(
        run_pipeline(
            bot=ctx.bot,
            requester_id=update.effective_user.id,
            course_id=course_id,
            target_chat=target,
            cookie_str="",
        )
    )

    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


# ── Keep-alive Flask server (Render needs HTTP) ───────────────────────────────
flask_app = Flask(__name__)

@flask_app.route("/")
def ping():
    return "🟢 CourseBot alive", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    Thread(target=run_flask, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("download", download_cmd)],
        states={
            ASK_COURSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_course_id)],
            ASK_TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_target)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)

    log.info("Bot started.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
