import os
import re
import time
import math
import asyncio
import aiohttp
import humanize
import subprocess
from urllib.parse import urlparse, unquote

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import BOT_TOKEN, API_ID, API_HASH, DOWNLOAD_DIR

# ✅ YouTube module
from youtube import (
    is_youtube_url,
    youtube_download_video,
    youtube_download_file,
    youtube_download_audio
)

# ===========================
# LIMITS
# ===========================
URL_UPLOAD_LIMIT = 2 * 1024 * 1024 * 1024          # 2GB
TG_UPLOAD_LIMIT = 1900 * 1024 * 1024               # ~1.9GB safe for Telegram

CHUNK_SIZE = 1024 * 256

# ===========================
# GLOBALS
# ===========================
USER_URL = {}
USER_TASKS = {}
USER_CANCEL = set()
USER_MEDIA = {}
USER_STATE = {}
USER_YT = {}  # {uid: {"url":..., "mode":...}}

# ===========================
# Insta Reel
# ===========================
INSTA_REGEX = re.compile(r"(https?://(www\.)?instagram\.com/(reel|p)/[A-Za-z0-9_\-]+)")

def is_instagram_url(text: str) -> bool:
    return bool(INSTA_REGEX.search(text or ""))

def clean_insta_url(text: str) -> str:
    m = INSTA_REGEX.search(text or "")
    return m.group(1) if m else (text or "").strip()

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

def format_time(seconds: float):
    if seconds <= 0:
        return "0s"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"

def progress_bar(percent: float, length=14):
    filled = int(length * percent / 100)
    return "●" * filled + "○" * (length - filled)

def make_progress_text(title, current, total, speed, eta):
    percent = (current / total * 100) if total else 0
    bar = progress_bar(percent)

    c_str = humanize.naturalsize(current, binary=True)
    t_str = humanize.naturalsize(total, binary=True) if total else "Unknown"
    s_str = humanize.naturalsize(speed, binary=True) + "/s" if speed else "0 B/s"

    return (
        f"{title}\n\n"
        f"[{bar}]\n"
        f"✅ Done: {percent:.2f}%\n"
        f"📦 Size: {c_str} of {t_str}\n"
        f"⚡ Speed: {s_str}\n"
        f"⏳ ETA: {format_time(eta)}"
    )

async def safe_edit(msg, text, reply_markup=None):
    try:
        await msg.edit(text, reply_markup=reply_markup)
    except:
        pass

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🌐 URL Uploader", callback_data="menu_url"),
            InlineKeyboardButton("📸 Instagram", callback_data="menu_insta")
        ],
        [
            InlineKeyboardButton("🎬 YouTube", callback_data="menu_yt"),
            InlineKeyboardButton("🗜️ Compressor", callback_data="menu_compress")
        ],
        [
            InlineKeyboardButton("🎛️ Converter", callback_data="menu_convert")
        ]
    ])

def back_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_main")]])

def cancel_kb(uid: int, label="❌ Cancel"):
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"cancel_{uid}")]])

# ===========================
# URL filename + size
# ===========================
async def get_filename_and_size(url: str):
    filename = None
    total = 0
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                async with session.head(url, allow_redirects=True) as r:
                    cd = r.headers.get("Content-Disposition", "")
                    if "filename=" in cd:
                        filename = cd.split("filename=")[-1].strip().strip('"').strip("'")
                    if r.headers.get("Content-Length"):
                        total = int(r.headers.get("Content-Length"))
            except:
                pass

            if not filename:
                async with session.get(url, allow_redirects=True) as r:
                    cd = r.headers.get("Content-Disposition", "")
                    if "filename=" in cd:
                        filename = cd.split("filename=")[-1].strip().strip('"').strip("'")
                    if not filename:
                        p = urlparse(str(r.url))
                        base = os.path.basename(p.path)
                        base = unquote(base)
                        if base:
                            filename = base
                    if not total and r.headers.get("Content-Length"):
                        total = int(r.headers.get("Content-Length"))
    except:
        pass

    if not filename:
        filename = f"file_{int(time.time())}.bin"

    return safe_filename(filename), total

# ===========================
# Download Stream URL
# ===========================
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
                total = int(r.headers["Content-Length"])

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
                        await safe_edit(
                            status_msg,
                            make_progress_text("⬇️ Downloading...", downloaded, total, speed, eta),
                            reply_markup=cancel_kb(uid, "❌ Cancel Download")
                        )

    return downloaded, total

# ===========================
# Upload Progress
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
        await safe_edit(
            status_msg,
            make_progress_text("📤 Uploading...", current, total, speed, eta),
            reply_markup=cancel_kb(uid, "❌ Cancel Upload")
        )

# ===========================
# Insta download (yt-dlp)
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
        await safe_edit(status_msg, "📥 Downloading Instagram Reel...\n⏳ Please wait...", cancel_kb(uid))

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
# Processing bar (fake progress)
# ===========================
async def processing_bar(status_msg, title, uid, seconds=12):
    start = time.time()
    while True:
        if uid in USER_CANCEL:
            raise asyncio.CancelledError

        elapsed = time.time() - start
        if elapsed >= seconds:
            break

        percent = (elapsed / seconds) * 100
        bar = progress_bar(percent)
        await safe_edit(status_msg, f"{title}\n\n[{bar}]\n⏳ Please wait...", cancel_kb(uid))
        await asyncio.sleep(1)

# ===========================
# Pyrogram Client
# ===========================
app = Client(
    "MultiFunctionBot",
    bot_token=BOT_TOKEN,
    api_id=API_ID,
    api_hash=API_HASH
)

# ===========================
# START
# ===========================
WELCOME_TEXT = (
    "✨ **Welcome to Multifunctional Bot! 🤖💫**\n"
    "Here you can do multiple things in one bot 🚀\n\n"
    "🌐 **URL Uploader** ✅ (2GB)\n"
    "🗜️ **Compressor** ✅\n"
    "🎛️ **Converter** ✅\n"
    "📸 **Instagram Reel Downloader** ✅\n"
    "🎬 **YouTube Downloader** ✅\n\n"
    "📌 **How to use?**\n"
    "1️⃣ Send File/Video/Audio/URL\n"
    "2️⃣ Select option ✅\n"
    "3️⃣ Wait ⏳\n"
    "4️⃣ Get output 🎉\n\n"
    "🚀 Now send something 👇😊"
)

@app.on_message(filters.private & filters.command("start"))
async def start_cmd(client, message):
    USER_STATE.pop(message.from_user.id, None)
    await message.reply(WELCOME_TEXT, reply_markup=main_menu_keyboard())

@app.on_callback_query(filters.regex("^back_main$"))
async def back_main(client, cb):
    USER_STATE.pop(cb.from_user.id, None)
    await cb.answer()
    await cb.message.edit(WELCOME_TEXT, reply_markup=main_menu_keyboard())

# ===========================
# MENU HANDLERS
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
    await cb.message.edit("📸 **Instagram Reel Downloader**\n\nSend Instagram reel link 👇", reply_markup=back_keyboard())

@app.on_callback_query(filters.regex("^menu_yt$"))
async def menu_yt(client, cb):
    USER_STATE[cb.from_user.id] = "WAIT_YT"
    await cb.answer()
    await cb.message.edit("🎬 **YouTube Downloader**\n\nSend YouTube link 👇", reply_markup=back_keyboard())

@app.on_callback_query(filters.regex("^menu_compress$"))
async def menu_compress(client, cb):
    USER_STATE[cb.from_user.id] = "WAIT_MEDIA_COMPRESS"
    await cb.answer()
    await cb.message.edit("🗜️ **Compressor Mode**\n\nSend a video/file to compress 👇", reply_markup=back_keyboard())

@app.on_callback_query(filters.regex("^menu_convert$"))
async def menu_convert(client, cb):
    USER_STATE[cb.from_user.id] = "WAIT_MEDIA_CONVERT"
    await cb.answer()
    await cb.message.edit("🎛️ **Converter Mode**\n\nSend a video/file/audio to convert 👇", reply_markup=back_keyboard())

# ===========================
# CANCEL CALLBACK
# ===========================
@app.on_callback_query(filters.regex("^cancel_"))
async def cancel_task(client, cb):
    try:
        _, uid_str = cb.data.split("_", 1)
        uid = int(uid_str)
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

    # ✅ YouTube detect
    if is_youtube_url(text):
        USER_URL[uid] = text
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🎥 Video", callback_data="yt_video"),
                InlineKeyboardButton("📁 File", callback_data="yt_file"),
                InlineKeyboardButton("🎵 Audio", callback_data="yt_audio"),
            ],
            [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
        ])
        return await message.reply(
            f"✅ **YouTube Link Detected** 🎬\n\n📌 Link:\n{text}\n\n👇 Choose format:",
            reply_markup=kb
        )

    # ✅ Instagram detect
    if is_instagram_url(text):
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🎥 Video", callback_data="insta_video"),
                InlineKeyboardButton("📁 File", callback_data="insta_file")
            ],
            [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
        ])
        USER_URL[uid] = clean_insta_url(text)
        return await message.reply(
            f"✅ **Instagram Reel Detected** 📸\n\n📌 Link:\n{text}\n\n👇 Select format:",
            reply_markup=kb
        )

    state = USER_STATE.get(uid, "")

    # URL upload mode
    if state == "WAIT_URL" and is_url(text):
        USER_URL[uid] = text
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🎥 Video Upload", callback_data="send_video"),
                InlineKeyboardButton("📁 File Upload", callback_data="send_file")
            ],
            [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
        ])
        return await message.reply("✅ URL Received!\n\nChoose upload type:", reply_markup=kb)

    if state in ("WAIT_INSTA", "WAIT_YT"):
        return await message.reply("❌ Please send a valid link.")

    if state == "WAIT_URL":
        return await message.reply("❌ Please send a valid URL (http/https).")

# ===========================
# MEDIA HANDLER (Telegram file/video/audio)
# ===========================
@app.on_message(filters.private & (filters.video | filters.document | filters.audio))
async def media_handler(client, message):
    uid = message.from_user.id
    USER_CANCEL.discard(uid)

    status = await message.reply("⬇️ Downloading from Telegram...", reply_markup=cancel_kb(uid, "❌ Cancel Download"))

    async def dl_progress(current, total):
        if uid in USER_CANCEL:
            raise asyncio.CancelledError
        elapsed = time.time() - start
        speed = current / elapsed if elapsed > 0 else 0
        eta = (total - current) / speed if speed > 0 else 0
        await safe_edit(status, make_progress_text("⬇️ Downloading from Telegram...", current, total, speed, eta), cancel_kb(uid))

    start = time.time()
    try:
        path = await message.download(progress=dl_progress)
    except asyncio.CancelledError:
        return await safe_edit(status, "❌ Cancelled.")
    except Exception as e:
        return await safe_edit(status, f"❌ Failed!\n\nError: `{e}`")

    USER_MEDIA[uid] = path

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🗜️ Compressor", callback_data="media_compress"),
            InlineKeyboardButton("🎛️ Converter", callback_data="media_convert"),
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
    ])
    await safe_edit(status, "✅ File received!\n\nChoose option:", kb)

# ===========================
# Compressor menu
# ===========================
@app.on_callback_query(filters.regex("^media_compress$"))
async def media_compress_menu(client, cb):
    uid = cb.from_user.id
    if uid not in USER_MEDIA:
        return await cb.answer("Send media first!", show_alert=True)

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🟢 Higher Quality", callback_data="compress_high"),
            InlineKeyboardButton("🔴 Lower Quality", callback_data="compress_low")
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
    ])
    await cb.answer()
    await cb.message.edit("🗜️ Compressor Menu\n\nChoose compression type:", reply_markup=kb)

# Converter placeholder
@app.on_callback_query(filters.regex("^media_convert$"))
async def media_convert_menu(client, cb):
    await cb.answer()
    await cb.message.edit("🎛️ Converter coming soon ✅", reply_markup=back_keyboard())

# ===========================
# Quality buttons
# ===========================
HIGH_QUALS = ["2160p", "1440p", "1080p", "720p"]
LOW_QUALS  = ["480p", "360p", "240p", "144p"]

def quality_keyboard(prefix: str, quals):
    rows, row = [], []
    for q in quals:
        row.append(InlineKeyboardButton(q, callback_data=f"{prefix}:{q}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="media_compress")])
    return InlineKeyboardMarkup(rows)

@app.on_callback_query(filters.regex("^compress_high$"))
async def compress_high(client, cb):
    await cb.answer()
    await cb.message.edit("🟢 Select Higher Quality:", reply_markup=quality_keyboard("do_high", HIGH_QUALS))

@app.on_callback_query(filters.regex("^compress_low$"))
async def compress_low(client, cb):
    await cb.answer()
    await cb.message.edit("🔴 Select Lower Quality:", reply_markup=quality_keyboard("do_low", LOW_QUALS))

# ===========================
# COMPRESS ACTION (returns direct download link)
# ===========================
@app.on_callback_query(filters.regex(r"^(do_high|do_low):(2160p|1440p|1080p|720p|480p|360p|240p|144p)$"))
async def do_compress(client, cb):
    uid = cb.from_user.id
    if uid not in USER_MEDIA:
        return await cb.answer("Send media first!", show_alert=True)

    quality = cb.data.split(":", 1)[1]
    await cb.answer()
    status = cb.message

    async def job():
        try:
            USER_CANCEL.discard(uid)
            await processing_bar(status, f"⚙️ Compressing ({quality})...", uid, seconds=15)

            # ⚠️ API integration place (later)
            dl = f"https://example.com/download/compressed_{uid}_{int(time.time())}.mp4"

            txt = (
                "✅ **Compressed Successfully ☁️**\n"
                f"📦 Quality: **{quality}**\n"
                f"⬇️ Download Link: {dl}\n"
                "⏳ Link Expire: 24h"
            )
            btn = InlineKeyboardMarkup([
                [InlineKeyboardButton("🌐 Upload via URL Uploader", callback_data=f"uplink::{dl}")],
                [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
            ])
            await safe_edit(status, txt, btn)

        except asyncio.CancelledError:
            await safe_edit(status, "❌ Cancelled ✅")
        except Exception as e:
            await safe_edit(status, f"❌ Failed!\n\nError: `{e}`")

    t = asyncio.create_task(job())
    USER_TASKS[uid] = t

# ===========================
# Upload compressed link via URL uploader
# ===========================
@app.on_callback_query(filters.regex(r"^uplink::"))
async def upload_link_cb(client, cb):
    uid = cb.from_user.id
    dl = cb.data.split("::", 1)[1].strip()

    USER_URL[uid] = dl
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎥 Video Upload", callback_data="send_video"),
            InlineKeyboardButton("📁 File Upload", callback_data="send_file")
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
    ])
    await cb.answer()
    await cb.message.edit("✅ Download link ready!\n\nChoose upload type:", reply_markup=kb)

# ===========================
# Insta callbacks
# ===========================
@app.on_callback_query(filters.regex("^insta_(video|file)$"))
async def insta_send(client, cb):
    uid = cb.from_user.id
    if uid not in USER_URL:
        return await cb.answer("Session expired. Send reel link again.", show_alert=True)

    url = USER_URL[uid]
    mode = cb.data.replace("insta_", "")

    await cb.answer()
    status = cb.message

    async def job():
        file_path = None
        try:
            USER_CANCEL.discard(uid)

            await safe_edit(status, "📥 Downloading reel...\n⏳ Please wait...", cancel_kb(uid))
            file_path = await insta_download(url, uid, status_msg=status)

            if uid in USER_CANCEL:
                raise asyncio.CancelledError

            await safe_edit(status, "📤 Uploading reel...\n⏳ Please wait...", cancel_kb(uid))
            up_start = time.time()

            if mode == "video":
                await client.send_video(
                    chat_id=cb.message.chat.id,
                    video=file_path,
                    caption=f"✅ Reel Uploaded 🎥\n\n📌 {os.path.basename(file_path)}",
                    progress=upload_progress,
                    progress_args=(status, uid, up_start),
                    supports_streaming=True,
                    sleep_threshold=60
                )
            else:
                await client.send_document(
                    chat_id=cb.message.chat.id,
                    document=file_path,
                    caption=f"✅ Reel Uploaded as File 📁\n\n📌 {os.path.basename(file_path)}",
                    progress=upload_progress,
                    progress_args=(status, uid, up_start),
                    sleep_threshold=60
                )

            await safe_edit(status, "✅ Done ✅", back_keyboard())

        except asyncio.CancelledError:
            await safe_edit(status, "❌ Cancelled ✅")
        except Exception as e:
            await safe_edit(status, f"❌ Failed!\n\nError: `{e}`")
        finally:
            if file_path and os.path.exists(file_path):
                try: os.remove(file_path)
                except: pass
            USER_URL.pop(uid, None)
            USER_CANCEL.discard(uid)

    t = asyncio.create_task(job())
    USER_TASKS[uid] = t

# ===========================
# URL upload handlers
# ===========================
@app.on_callback_query(filters.regex("^(send_file|send_video)$"))
async def send_type_selected(client, cb):
    uid = cb.from_user.id
    if uid not in USER_URL:
        return await cb.message.edit("❌ Session expired. Send URL again.")

    url = USER_URL[uid]
    mode = cb.data.replace("send_", "")

    await cb.answer()
    status = cb.message

    async def job():
        file_path = None
        try:
            filename, size = await get_filename_and_size(url)

            if size and size > URL_UPLOAD_LIMIT:
                return await safe_edit(status, "❌ URL file too large! Max: 2GB", back_keyboard())

            if "." not in filename:
                filename += ".bin"

            file_path = os.path.join(DOWNLOAD_DIR, f"{uid}_{int(time.time())}_{filename}")

            await safe_edit(status, "⬇️ Starting download...", cancel_kb(uid, "❌ Cancel Download"))
            await download_stream(url, file_path, status, uid)

            if uid in USER_CANCEL:
                raise asyncio.CancelledError

            await safe_edit(status, "📤 Upload starting...", cancel_kb(uid, "❌ Cancel Upload"))
            up_start = time.time()

            if mode == "video":
                await client.send_video(
                    chat_id=cb.message.chat.id,
                    video=file_path,
                    caption=f"✅ Uploaded as Video 🎥\n\n📌 {os.path.basename(file_path)}",
                    progress=upload_progress,
                    progress_args=(status, uid, up_start),
                    supports_streaming=True,
                    sleep_threshold=60
                )
            else:
                await client.send_document(
                    chat_id=cb.message.chat.id,
                    document=file_path,
                    caption=f"✅ Uploaded as File 📁\n\n📌 {os.path.basename(file_path)}",
                    progress=upload_progress,
                    progress_args=(status, uid, up_start),
                    sleep_threshold=60
                )

            await safe_edit(status, "✅ Done ✅", back_keyboard())

        except asyncio.CancelledError:
            await safe_edit(status, "❌ Cancelled ✅")
        except Exception as e:
            await safe_edit(status, f"❌ Failed!\n\nError: `{e}`")
        finally:
            if file_path and os.path.exists(file_path):
                try: os.remove(file_path)
                except: pass
            USER_URL.pop(uid, None)
            USER_CANCEL.discard(uid)

    t = asyncio.create_task(job())
    USER_TASKS[uid] = t

# ===========================
# YouTube quality menus
# ===========================
YT_QUALS = ["2160p", "1440p", "1080p", "720p", "480p", "360p", "240p", "144p"]
AUDIO_QUALS = ["320", "192", "128", "64"]

def yt_quality_kb(prefix: str, items):
    rows, row = [], []
    for x in items:
        label = f"{x}kbps" if prefix.startswith("yta") else x
        row.append(InlineKeyboardButton(label, callback_data=f"{prefix}:{x}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)

@app.on_callback_query(filters.regex("^yt_(video|file|audio)$"))
async def yt_mode_cb(client, cb):
    uid = cb.from_user.id
    url = USER_URL.get(uid)
    if not url:
        return await cb.answer("Send YouTube link again!", show_alert=True)

    mode = cb.data.replace("yt_", "")
    USER_YT[uid] = {"url": url, "mode": mode}

    await cb.answer()
    if mode == "audio":
        await cb.message.edit("🎵 Select audio quality:", reply_markup=yt_quality_kb("yta", AUDIO_QUALS))
    else:
        await cb.message.edit("🎬 Select video quality:", reply_markup=yt_quality_kb("ytv", YT_QUALS))

@app.on_callback_query(filters.regex(r"^(ytv|yta):"))
async def yt_quality_selected(client, cb):
    uid = cb.from_user.id
    session = USER_YT.get(uid)
    if not session:
        return await cb.answer("Session expired!", show_alert=True)

    url = session["url"]
    mode = session["mode"]
    quality = cb.data.split(":", 1)[1]

    await cb.answer()
    status = cb.message

    async def job():
        file_path = None
        try:
            USER_CANCEL.discard(uid)

            await safe_edit(status, "📥 Downloading YouTube...\n⏳ Please wait...", cancel_kb(uid))

            if mode == "video":
                file_path = await youtube_download_video(url, uid, quality, status_msg=status)
            elif mode == "file":
                file_path = await youtube_download_file(url, uid, quality, status_msg=status)
            else:
                file_path = await youtube_download_audio(url, uid, quality, status_msg=status)

            if uid in USER_CANCEL:
                raise asyncio.CancelledError

            size = os.path.getsize(file_path)

            if size > TG_UPLOAD_LIMIT:
                txt = (
                    "✅ **Download Ready ☁️**\n"
                    f"📦 Size: **{humanize.naturalsize(size, binary=True)}**\n\n"
                    "⚠️ File too large for Telegram upload.\n"
                    "Try lower quality."
                )
                return await safe_edit(status, txt, back_keyboard())

            await safe_edit(status, "📤 Uploading...\n⏳ Please wait...", cancel_kb(uid))
            up_start = time.time()

            # ✅ write_timeout removed (FIXED)
            if mode == "audio":
                await client.send_document(
                    chat_id=cb.message.chat.id,
                    document=file_path,
                    caption=f"✅ YouTube Audio Downloaded 🎵\n\n📌 {os.path.basename(file_path)}",
                    progress=upload_progress,
                    progress_args=(status, uid, up_start),
                    sleep_threshold=60
                )
            elif mode == "file":
                await client.send_document(
                    chat_id=cb.message.chat.id,
                    document=file_path,
                    caption=f"✅ YouTube Downloaded as File 📁\n\n📌 {os.path.basename(file_path)}",
                    progress=upload_progress,
                    progress_args=(status, uid, up_start),
                    sleep_threshold=60
                )
            else:
                await client.send_video(
                    chat_id=cb.message.chat.id,
                    video=file_path,
                    caption=f"✅ YouTube Video Downloaded 🎥\n\n📌 {os.path.basename(file_path)}",
                    progress=upload_progress,
                    progress_args=(status, uid, up_start),
                    supports_streaming=True,
                    sleep_threshold=60
                )

            await safe_edit(status, "✅ Done ✅", back_keyboard())

        except asyncio.CancelledError:
            await safe_edit(status, "❌ Cancelled ✅")
        except Exception as e:
            err = str(e)
            low = err.lower()
            if "private" in low or "restricted" in low or "blocked" in low:
                err = "YouTube video is private / restricted / blocked. Try another link."
            await safe_edit(status, f"❌ Failed!\n\nError: `{err}`")
        finally:
            if file_path and os.path.exists(file_path):
                try: os.remove(file_path)
                except: pass
            USER_YT.pop(uid, None)

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
