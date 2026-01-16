import os
import re
import time
import json
import math
import asyncio
import aiohttp
import humanize
import subprocess
from urllib.parse import urlparse, unquote

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import BOT_TOKEN, API_ID, API_HASH, DOWNLOAD_DIR

# ===========================
# LIMITS
# ===========================
URL_UPLOAD_LIMIT = 2 * 1024 * 1024 * 1024   # 2GB URL uploader
COMPRESS_LIMIT = 700 * 1024 * 1024          # 700MB compressor limit
CHUNK_SIZE = 1024 * 256

# ===========================
# GLOBALS
# ===========================
USER_URL = {}
USER_TASKS = {}
USER_CANCEL = set()

USER_STATE = {}
LAST_MEDIA = {}  # uid -> {"type","path","size","name"}

UI_STATUS_MSG = {}

# ===========================
# Regex: Instagram / YouTube
# ===========================
INSTA_REGEX = re.compile(r"(https?://(www\.)?instagram\.com/(reel|p)/[A-Za-z0-9_\-]+)")
YT_REGEX = re.compile(r"(https?://(www\.)?(youtube\.com|youtu\.be)/\S+)")

def is_instagram_url(text: str) -> bool:
    return bool(INSTA_REGEX.search(text or ""))

def clean_insta_url(text: str) -> str:
    m = INSTA_REGEX.search(text or "")
    return m.group(1) if m else (text or "").strip()

def is_youtube_url(text: str) -> bool:
    return bool(YT_REGEX.search(text or ""))

def clean_youtube_url(text: str) -> str:
    return (text or "").strip().split("&")[0]

# ===========================
# Utils
# ===========================
def is_url(text: str):
    return text.startswith("http://") or text.startswith("https://")

def safe_filename(name: str):
    name = re.sub(r"[\\/:*?\"<>|]", "_", name)
    name = name.strip().strip(".")
    if not name:
        name = f"file_{int(time.time())}"
    return name[:180]

def clean_display_name(name: str):
    base = os.path.splitext(name)[0]
    base = unquote(base)
    base = re.sub(r"[^a-zA-Z0-9]+", "_", base).strip("_")
    if len(base) > 60:
        base = base[:60].rstrip("_")
    return base or f"file_{int(time.time())}"

def format_time(seconds: float):
    if seconds <= 0:
        return "0s"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"

def naturalsize(num_bytes: int):
    if num_bytes is None:
        return "Unknown"
    if num_bytes <= 0:
        return "0 B"
    return humanize.naturalsize(num_bytes, binary=True)

# ✅ Emoji color transition bar
def make_circle_bar(percent: float, slots: int = 14):
    percent = max(0, min(100, percent))
    filled = int((percent / 100) * slots)

    if percent <= 0:
        icon = "⚪"
    elif percent < 25:
        icon = "🔴"
    elif percent < 50:
        icon = "🟠"
    elif percent < 75:
        icon = "🟡"
    elif percent < 100:
        icon = "🟢"
    else:
        icon = "✅"

    return f"[{icon * filled}{'⚪' * (slots - filled)}]"

def make_progress_text(title, done, total, speed, eta):
    percent = (done / total * 100) if total else 0
    bar = make_circle_bar(percent)
    speed_str = naturalsize(int(speed)) + "/s" if speed else "0 B/s"

    return (
        f"✨ **{title}**\n\n"
        f"{bar}\n\n"
        f"📊 Progress: **{percent:.2f}%**\n"
        f"📦 Size: **{naturalsize(done)} / {naturalsize(total) if total else 'Unknown'}**\n"
        f"⚡ Speed: **{speed_str}**\n"
        f"⏳ ETA: **{format_time(eta)}**"
    )

async def safe_edit(msg, text, reply_markup=None):
    try:
        await msg.edit(text, reply_markup=reply_markup)
    except:
        pass

def busy(uid: int) -> bool:
    return uid in USER_TASKS and not USER_TASKS[uid].done()

async def get_or_create_status(message, uid):
    if uid in UI_STATUS_MSG:
        return UI_STATUS_MSG[uid]
    status = await message.reply("⏳ Processing...")
    UI_STATUS_MSG[uid] = status
    return status

def clean_file(p):
    if p and os.path.exists(p):
        try:
            os.remove(p)
        except:
            pass

# ===========================
# Video meta + thumbnail
# ===========================
def get_video_meta(path: str):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error",
             "-select_streams", "v:0",
             "-show_entries", "stream=width,height,duration",
             "-of", "json", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        data = json.loads(r.stdout)
        stream = data["streams"][0]
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
        duration = float(stream.get("duration") or 0)
        return int(duration), width, height
    except:
        return 0, 0, 0

async def gen_thumbnail(input_path: str, out_thumb: str):
    dur, _, _ = get_video_meta(input_path)
    ss = dur // 2 if dur and dur > 6 else 3

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(ss),
        "-i", input_path,
        "-frames:v", "1",
        "-vf", "scale=640:-1",
        "-q:v", "2",
        out_thumb
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL
    )
    await proc.wait()
    return os.path.exists(out_thumb)

# ===========================
# Telegram download progress
# ===========================
async def tg_download_progress(current, total, status_msg, uid, start_time):
    if uid in USER_CANCEL:
        raise asyncio.CancelledError

    elapsed = time.time() - start_time
    speed = current / elapsed if elapsed > 0 else 0
    eta = (total - current) / speed if speed > 0 else 0

    now = time.time()
    if not hasattr(status_msg, "_last_edit"):
        status_msg._last_edit = 0

    if now - status_msg._last_edit > 2.0:
        status_msg._last_edit = now
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])
        await safe_edit(status_msg, make_progress_text("⬇️ Downloading", current, total, speed, eta), kb)

# ===========================
# URL download stream
# ===========================
async def get_filename_and_size(url: str):
    filename = None
    total = 0
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, allow_redirects=True) as r:
                if r.headers.get("Content-Length"):
                    total = int(r.headers.get("Content-Length"))
                cd = r.headers.get("Content-Disposition", "")
                if "filename=" in cd:
                    filename = cd.split("filename=")[-1].strip().strip('"').strip("'")
                if not filename:
                    p = urlparse(str(r.url))
                    base = os.path.basename(p.path)
                    base = unquote(base)
                    if base:
                        filename = base
    except:
        pass

    if not filename:
        filename = f"file_{int(time.time())}.bin"
    return safe_filename(filename), total

async def download_stream(url, file_path, status_msg, uid):
    USER_CANCEL.discard(uid)
    timeout = aiohttp.ClientTimeout(total=None)

    downloaded = 0
    start_time = time.time()
    last_edit = 0
    total = 0

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, allow_redirects=True) as r:
            if r.status != 200:
                raise Exception(f"HTTP {r.status}")

            if r.headers.get("Content-Length"):
                total = int(r.headers.get("Content-Length"))

            if total and total > URL_UPLOAD_LIMIT:
                raise Exception("❌ URL file too large (max 2GB)")

            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, "wb") as f:
                async for chunk in r.content.iter_chunked(CHUNK_SIZE):
                    if uid in USER_CANCEL:
                        raise asyncio.CancelledError
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)

                    elapsed = time.time() - start_time
                    speed = downloaded / elapsed if elapsed > 0 else 0
                    eta = (total - downloaded) / speed if total and speed > 0 else 0

                    if time.time() - last_edit > 2:
                        last_edit = time.time()
                        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Download", callback_data=f"cancel_{uid}")]])
                        await safe_edit(status_msg, make_progress_text("⬇️ Downloading", downloaded, total, speed, eta), kb)

# ===========================
# Upload progress
# ===========================
async def upload_progress(current, total, status_msg, uid, start_time):
    if uid in USER_CANCEL:
        raise asyncio.CancelledError

    elapsed = time.time() - start_time
    speed = current / elapsed if elapsed > 0 else 0
    eta = (total - current) / speed if speed > 0 else 0

    now = time.time()
    if not hasattr(status_msg, "_last_edit"):
        status_msg._last_edit = 0

    if now - status_msg._last_edit > 2:
        status_msg._last_edit = now
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Upload", callback_data=f"cancel_{uid}")]])
        await safe_edit(status_msg, make_progress_text("📤 Uploading", current, total, speed, eta), kb)

# ===========================
# Instagram downloader (yt-dlp)
# ===========================
async def insta_download(url: str, uid: int, status_msg=None):
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    url = clean_insta_url(url)

    outtmpl = os.path.join(DOWNLOAD_DIR, f"insta_{uid}_{int(time.time())}.%(ext)s")

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--socket-timeout", "30",
        "--retries", "5",
        "-f", "bv*+ba/best",
        "--merge-output-format", "mp4",
        "-o", outtmpl,
        url
    ]

    if status_msg:
        await safe_edit(status_msg, "📥 Downloading Instagram Reel...\n⏳ Please wait...")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = (stderr.decode(errors="ignore") or stdout.decode(errors="ignore"))[:350]
        raise Exception(f"Insta download failed: {err}")

    mp4_path = outtmpl.replace("%(ext)s", "mp4")
    if os.path.exists(mp4_path):
        return mp4_path

    files = [f for f in os.listdir(DOWNLOAD_DIR) if f.startswith(f"insta_{uid}_") and f.endswith(".mp4")]
    if not files:
        raise Exception("Downloaded MP4 not found.")
    files.sort(key=lambda x: os.path.getmtime(os.path.join(DOWNLOAD_DIR, x)), reverse=True)
    return os.path.join(DOWNLOAD_DIR, files[0])

# ===========================
# YouTube downloader system
# ===========================
YT_QUALITIES = ["1080p", "720p", "360p"]

async def yt_download(url: str, uid: int, quality: str, status_msg=None):
    """
    Download YouTube video with selected quality using yt-dlp.
    """
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    url = clean_youtube_url(url)

    outtmpl = os.path.join(DOWNLOAD_DIR, f"yt_{uid}_{int(time.time())}.%(ext)s")

    # format mapping
    if quality == "1080p":
        fmt = "bestvideo[height<=1080]+bestaudio/best[height<=1080]"
    elif quality == "720p":
        fmt = "bestvideo[height<=720]+bestaudio/best[height<=720]"
    else:
        fmt = "bestvideo[height<=360]+bestaudio/best[height<=360]"

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--socket-timeout", "30",
        "--retries", "5",
        "-f", fmt,
        "--merge-output-format", "mp4",
        "-o", outtmpl,
        url
    ]

    if status_msg:
        await safe_edit(status_msg, f"📥 Downloading YouTube ({quality})...\n⏳ Please wait...")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = (stderr.decode(errors="ignore") or stdout.decode(errors="ignore"))[:400]
        # clean message for private/restricted
        if "private" in err.lower() or "sign in" in err.lower() or "restricted" in err.lower():
            raise Exception("YouTube video is private / restricted / blocked. Try another link.")
        raise Exception(f"YouTube download failed: {err}")

    mp4_path = outtmpl.replace("%(ext)s", "mp4")
    if os.path.exists(mp4_path):
        return mp4_path

    files = [f for f in os.listdir(DOWNLOAD_DIR) if f.startswith(f"yt_{uid}_") and f.endswith(".mp4")]
    if not files:
        raise Exception("Downloaded MP4 not found.")
    files.sort(key=lambda x: os.path.getmtime(os.path.join(DOWNLOAD_DIR, x)), reverse=True)
    return os.path.join(DOWNLOAD_DIR, files[0])

# ===========================
# UI Keyboards
# ===========================
def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🌐 URL Uploader", callback_data="menu_url"),
            InlineKeyboardButton("📸 Instagram", callback_data="menu_insta")
        ],
        [
            InlineKeyboardButton("🗜️ Compressor", callback_data="menu_compress"),
            InlineKeyboardButton("▶️ YouTube", callback_data="menu_youtube")
        ]
    ])

def back_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_main")]])

# ===========================
# BOT INIT
# ===========================
app = Client(
    "MultiFunctionBot",
    bot_token=BOT_TOKEN,
    api_id=API_ID,
    api_hash=API_HASH
)

WELCOME_TEXT = (
    "✨ **Welcome to Multifunctional Bot! 🤖💫**\n\n"
    "🌐 URL Uploader ➜ 2GB ✅\n"
    "📸 Instagram Reel Downloader ✅\n"
    "▶️ YouTube Video Downloader ✅\n"
    "🗜️ Compressor ✅\n\n"
    "🚀 Now send something to start 👇😊"
)

# ===========================
# START
# ===========================
@app.on_message(filters.private & filters.command("start"))
async def start_cmd(client, message):
    uid = message.from_user.id
    USER_STATE.pop(uid, None)
    await message.reply(WELCOME_TEXT, reply_markup=main_menu_keyboard())

@app.on_callback_query(filters.regex("^back_main$"))
async def back_main(client, cb):
    USER_STATE.pop(cb.from_user.id, None)
    await cb.answer()
    await cb.message.edit(WELCOME_TEXT, reply_markup=main_menu_keyboard())

# ===========================
# MENUS
# ===========================
@app.on_callback_query(filters.regex("^menu_url$"))
async def menu_url(client, cb):
    USER_STATE[cb.from_user.id] = "WAIT_URL"
    await cb.answer()
    await cb.message.edit("🌐 **URL Uploader Mode**\n\nSend a direct URL 👇", reply_markup=back_keyboard())

@app.on_callback_query(filters.regex("^menu_insta$"))
async def menu_insta(client, cb):
    USER_STATE[cb.from_user.id] = "WAIT_INSTA"
    await cb.answer()
    await cb.message.edit("📸 **Instagram Mode**\n\nSend Reel URL 👇", reply_markup=back_keyboard())

@app.on_callback_query(filters.regex("^menu_compress$"))
async def menu_compress(client, cb):
    USER_STATE[cb.from_user.id] = "WAIT_MEDIA_COMPRESS"
    await cb.answer()
    await cb.message.edit("🗜️ **Compressor Mode**\n\nSend a video/file 👇", reply_markup=back_keyboard())

@app.on_callback_query(filters.regex("^menu_youtube$"))
async def menu_youtube(client, cb):
    USER_STATE[cb.from_user.id] = "WAIT_YOUTUBE"
    await cb.answer()
    await cb.message.edit("▶️ **YouTube Downloader**\n\nSend YouTube URL 👇", reply_markup=back_keyboard())

# ===========================
# CANCEL
# ===========================
@app.on_callback_query(filters.regex("^cancel_"))
async def cancel_task(client, cb):
    try:
        uid = int(cb.data.split("_", 1)[1])
    except:
        return await cb.answer("Invalid", show_alert=True)

    USER_CANCEL.add(uid)
    task = USER_TASKS.get(uid)
    if task and not task.done():
        task.cancel()

    await cb.answer("✅ Cancelled!")
    try:
        await cb.message.edit("❌ Cancelled by user.")
    except:
        pass

# ===========================
# TEXT HANDLER
# ===========================
@app.on_message(filters.private & filters.text)
async def text_handler(client, message):
    uid = message.from_user.id
    text = message.text.strip()

    if text.startswith("/"):
        return

    # Auto detect Instagram
    if is_instagram_url(text):
        USER_URL[uid] = clean_insta_url(text)
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🎥 Video", callback_data="insta_video"),
                InlineKeyboardButton("📁 File", callback_data="insta_file")
            ],
            [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
        ])
        return await message.reply("✅ Instagram Reel Detected 📸\n\n👇 Select format:", reply_markup=kb)

    # Auto detect YouTube
    if is_youtube_url(text):
        USER_URL[uid] = clean_youtube_url(text)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎥 1080p", callback_data="yt_1080p"),
             InlineKeyboardButton("🎥 720p", callback_data="yt_720p"),
             InlineKeyboardButton("🎥 360p", callback_data="yt_360p")],
            [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
        ])
        return await message.reply("✅ YouTube Link Detected ▶️\n\nSelect quality:", reply_markup=kb)

    state = USER_STATE.get(uid, "")

    if state == "WAIT_URL" and is_url(text):
        USER_URL[uid] = text
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🎥 Video Upload", callback_data="send_video"),
                InlineKeyboardButton("📁 File Upload", callback_data="send_file")
            ],
            [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
        ])
        return await message.reply("✅ URL Received!\n\n👇 Select upload type:", reply_markup=kb)

    if state == "WAIT_INSTA":
        return await message.reply("❌ Please send Instagram reel URL.")

    if state == "WAIT_YOUTUBE":
        return await message.reply("❌ Please send valid YouTube URL.")

    if state == "WAIT_URL":
        return await message.reply("❌ Please send direct URL (http/https).")

    return

# ===========================
# Instagram callbacks
# ===========================
@app.on_callback_query(filters.regex("^insta_(video|file)$"))
async def insta_send(client, cb):
    uid = cb.from_user.id
    if uid not in USER_URL:
        return await cb.answer("Session expired. Send reel again.", show_alert=True)

    url = USER_URL[uid]
    mode = cb.data.replace("insta_", "")

    await cb.answer()
    status = await get_or_create_status(cb.message, uid)

    async def job():
        file_path = None
        thumb = None
        try:
            USER_CANCEL.discard(uid)
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])

            await safe_edit(status, "📥 Downloading Instagram reel...\n⏳ Please wait...", kb)
            file_path = await insta_download(url, uid, status_msg=status)

            if uid in USER_CANCEL:
                raise asyncio.CancelledError

            size = os.path.getsize(file_path)
            name = clean_display_name(os.path.basename(file_path))

            if mode == "video":
                thumb = os.path.splitext(file_path)[0] + "_thumb.jpg"
                try:
                    await gen_thumbnail(file_path, thumb)
                except:
                    thumb = None

                up_start = time.time()
                await safe_edit(status, "📤 Uploading...", kb)

                await client.send_video(
                    chat_id=cb.message.chat.id,
                    video=file_path,
                    caption=f"✅ Instagram Reel 🎥\n\n📌 `{name}`\n📦 {naturalsize(size)}",
                    supports_streaming=True,
                    thumb=thumb if thumb and os.path.exists(thumb) else None,
                    progress=upload_progress,
                    progress_args=(status, uid, up_start)
                )
            else:
                up_start = time.time()
                await safe_edit(status, "📤 Uploading...", kb)

                await client.send_document(
                    chat_id=cb.message.chat.id,
                    document=file_path,
                    caption=f"✅ Instagram Reel 📁\n\n📌 `{name}`\n📦 {naturalsize(size)}",
                    progress=upload_progress,
                    progress_args=(status, uid, up_start)
                )

            await safe_edit(status, "✅ Done ✅", reply_markup=main_menu_keyboard())

        except asyncio.CancelledError:
            await safe_edit(status, "❌ Cancelled ✅", reply_markup=main_menu_keyboard())
        except Exception as e:
            await safe_edit(status, f"❌ Insta Failed!\n\nError: `{e}`", reply_markup=main_menu_keyboard())
        finally:
            USER_URL.pop(uid, None)
            clean_file(thumb)
            clean_file(file_path)
            USER_CANCEL.discard(uid)

    t = asyncio.create_task(job())
    USER_TASKS[uid] = t

# ===========================
# YouTube callbacks
# ===========================
@app.on_callback_query(filters.regex("^yt_(1080p|720p|360p)$"))
async def youtube_cb(client, cb):
    uid = cb.from_user.id
    if uid not in USER_URL:
        return await cb.answer("Session expired. Send YouTube link again.", show_alert=True)

    url = USER_URL[uid]
    quality = cb.data.replace("yt_", "")

    await cb.answer()
    status = await get_or_create_status(cb.message, uid)

    async def job():
        file_path = None
        thumb = None
        try:
            USER_CANCEL.discard(uid)
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])

            await safe_edit(status, f"📥 Downloading YouTube ({quality})...\n⏳ Please wait...", kb)
            file_path = await yt_download(url, uid, quality, status_msg=status)

            if uid in USER_CANCEL:
                raise asyncio.CancelledError

            size = os.path.getsize(file_path)
            name = clean_display_name(os.path.basename(file_path))

            thumb = os.path.splitext(file_path)[0] + "_thumb.jpg"
            try:
                await gen_thumbnail(file_path, thumb)
            except:
                thumb = None

            up_start = time.time()
            await safe_edit(status, "📤 Uploading...", kb)

            await client.send_video(
                chat_id=cb.message.chat.id,
                video=file_path,
                caption=f"✅ YouTube Video 🎥 ({quality})\n\n📌 `{name}`\n📦 {naturalsize(size)}",
                supports_streaming=True,
                thumb=thumb if thumb and os.path.exists(thumb) else None,
                progress=upload_progress,
                progress_args=(status, uid, up_start)
            )

            await safe_edit(status, "✅ Done ✅", reply_markup=main_menu_keyboard())

        except asyncio.CancelledError:
            await safe_edit(status, "❌ Cancelled ✅", reply_markup=main_menu_keyboard())
        except Exception as e:
            await safe_edit(status, f"❌ YouTube Failed!\n\nError: `{e}`", reply_markup=main_menu_keyboard())
        finally:
            USER_URL.pop(uid, None)
            clean_file(thumb)
            clean_file(file_path)
            USER_CANCEL.discard(uid)

    t = asyncio.create_task(job())
    USER_TASKS[uid] = t

# ===========================
# URL Upload Callback
# ===========================
@app.on_callback_query(filters.regex("^(send_file|send_video)$"))
async def send_url_upload(client, cb):
    uid = cb.from_user.id
    if uid not in USER_URL:
        return await cb.answer("Session expired. Send link again.", show_alert=True)

    url = USER_URL[uid]
    mode = cb.data.replace("send_", "")

    await cb.answer()
    status = await cb.message.reply("⏳ Preparing...")

    async def job():
        file_path = None
        thumb = None
        try:
            USER_CANCEL.discard(uid)

            fname, total = await get_filename_and_size(url)
            fname_clean = clean_display_name(fname)

            file_path = os.path.join(DOWNLOAD_DIR, f"{uid}_{int(time.time())}_{fname}")

            await safe_edit(status, "⬇️ Starting download...")
            await download_stream(url, file_path, status, uid)

            if uid in USER_CANCEL:
                raise asyncio.CancelledError

            size = os.path.getsize(file_path)

            if mode == "video":
                thumb = os.path.splitext(file_path)[0] + "_thumb.jpg"
                try:
                    await gen_thumbnail(file_path, thumb)
                except:
                    thumb = None

                up_start = time.time()
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Upload", callback_data=f"cancel_{uid}")]])
                await safe_edit(status, "📤 Uploading...", kb)

                await client.send_video(
                    chat_id=cb.message.chat.id,
                    video=file_path,
                    caption=f"✅ Uploaded 🎥\n\n📌 `{fname_clean}`\n📦 {naturalsize(size)}",
                    supports_streaming=True,
                    thumb=thumb if thumb and os.path.exists(thumb) else None,
                    progress=upload_progress,
                    progress_args=(status, uid, up_start)
                )
            else:
                up_start = time.time()
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Upload", callback_data=f"cancel_{uid}")]])
                await safe_edit(status, "📤 Uploading...", kb)

                await client.send_document(
                    chat_id=cb.message.chat.id,
                    document=file_path,
                    caption=f"✅ Uploaded 📁\n\n📌 `{fname_clean}`\n📦 {naturalsize(size)}",
                    progress=upload_progress,
                    progress_args=(status, uid, up_start)
                )

            await safe_edit(status, "✅ Done ✅", reply_markup=main_menu_keyboard())

        except asyncio.CancelledError:
            await safe_edit(status, "❌ Cancelled ✅", reply_markup=main_menu_keyboard())
        except Exception as e:
            await safe_edit(status, f"❌ Failed!\n\nError: `{e}`", reply_markup=main_menu_keyboard())
        finally:
            USER_URL.pop(uid, None)
            clean_file(thumb)
            clean_file(file_path)
            USER_CANCEL.discard(uid)

    t = asyncio.create_task(job())
    USER_TASKS[uid] = t

# ===========================
# RUN
# ===========================
if __name__ == "__main__":
    if not BOT_TOKEN or not API_ID or not API_HASH:
        print("❌ Please set BOT_TOKEN, API_ID, API_HASH in env!")
        raise SystemExit

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    print("✅ Bot started...")
    app.run()
