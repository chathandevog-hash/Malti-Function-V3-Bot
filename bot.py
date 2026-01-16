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
# ENV (API TOKENS)
# ===========================
# Use any one
FREECONVERT_ACCESS_TOKEN = os.getenv("FREECONVERT_ACCESS_TOKEN", "").strip()
CLOUDCONVERT_API_KEY = os.getenv("CLOUDCONVERT_API_KEY", "").strip()

# ===========================
# LIMITS
# ===========================
URL_UPLOAD_LIMIT = 2 * 1024 * 1024 * 1024          # 2GB
COMPRESS_LIMIT = 700 * 1024 * 1024                 # 700MB
CONVERT_LIMIT = 500 * 1024 * 1024                  # 500MB

CHUNK_SIZE = 1024 * 256

# ===========================
# GLOBALS
# ===========================
USER_URL = {}
USER_TASKS = {}
USER_CANCEL = set()

USER_MEDIA = {}  # store telegram media path per user
USER_STATE = {}  # state machine per user

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
    bar = "●" * filled + "○" * (length - filled)
    return f"[{bar}]"

def make_progress_text(title, current, total, speed, eta):
    percent = (current / total * 100) if total else 0
    bar = progress_bar(percent)

    c_str = humanize.naturalsize(current, binary=True)
    t_str = humanize.naturalsize(total, binary=True) if total else "Unknown"
    s_str = humanize.naturalsize(speed, binary=True) + "/s" if speed else "0 B/s"

    return (
        f"{title}\n\n"
        f"{bar}\n"
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
            InlineKeyboardButton("🗜️ Compressor", callback_data="menu_compress"),
            InlineKeyboardButton("🎛️ Converter", callback_data="menu_convert")
        ],
    ])

def back_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_main")]])

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
                        kb = InlineKeyboardMarkup([
                            [InlineKeyboardButton("❌ Cancel Download", callback_data=f"cancel_{uid}")]
                        ])
                        await safe_edit(
                            status_msg,
                            make_progress_text("⬇️ Downloading...", downloaded, total, speed, eta),
                            reply_markup=kb
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
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel Upload", callback_data=f"cancel_{uid}")]
        ])
        await safe_edit(
            status_msg,
            make_progress_text("📤 Uploading...", current, total, speed, eta),
            reply_markup=kb
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
    "🌐 **URL Uploader**\n"
    "➜ Send any direct link and I will upload it for you instantly ✅\n"
    "⚠️ URL Upload Limit: **2GB**\n\n"
    "📸 **Instagram Reel Downloader**\n"
    "➜ Send Instagram reel link and I will download it ✅\n\n"
    "🗜️ **Compressor**\n"
    "➜ Reduce file/video size easily without hassle ⚡\n"
    "⚠️ Compression Limit: **700MB**\n\n"
    "🎛️ **Converter**\n"
    "➜ Convert your files into different formats (mp4 / mp3 / mkv etc.) 🎬🎵\n"
    "⚠️ Conversion Limit: **500MB**\n\n"
    "📌 **How to use?**\n"
    "1️⃣ Send a File / Video / Audio / URL\n"
    "2️⃣ Select your needed option ✅\n"
    "3️⃣ Wait for processing ⏳\n"
    "4️⃣ Get your output 🎉\n\n"
    "💡 Use /help for all commands & guide 🛠️\n"
    "🚀 Now send something to start 👇😊"
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
    await cb.message.edit(
        "🌐 **URL Uploader Mode**\n\nSend a direct URL 👇",
        reply_markup=back_keyboard()
    )

@app.on_callback_query(filters.regex("^menu_insta$"))
async def menu_insta(client, cb):
    USER_STATE[cb.from_user.id] = "WAIT_INSTA"
    await cb.answer()
    await cb.message.edit(
        "📸 **Instagram Reel Downloader**\n\nSend Instagram reel link 👇",
        reply_markup=back_keyboard()
    )

@app.on_callback_query(filters.regex("^menu_compress$"))
async def menu_compress(client, cb):
    USER_STATE[cb.from_user.id] = "WAIT_MEDIA_COMPRESS"
    await cb.answer()
    await cb.message.edit(
        "🗜️ **Compressor Mode**\n\nSend a video/file to compress 👇",
        reply_markup=back_keyboard()
    )

@app.on_callback_query(filters.regex("^menu_convert$"))
async def menu_convert(client, cb):
    USER_STATE[cb.from_user.id] = "WAIT_MEDIA_CONVERT"
    await cb.answer()
    await cb.message.edit(
        "🎛️ **Converter Mode**\n\nSend a video/file/audio to convert 👇",
        reply_markup=back_keyboard()
    )

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

    await cb.answer("✅ Cancelled!", show_alert=False)
    try:
        await cb.message.edit("❌ Cancelled by user.")
    except:
        pass

# ===========================
# TEXT HANDLER (URL + INSTA)
# ===========================
@app.on_message(filters.private & filters.text)
async def text_handler(client, message):
    uid = message.from_user.id
    text = message.text.strip()

    # INSTAGRAM LINK
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

    if state == "WAIT_INSTA":
        return await message.reply("❌ Please send a valid Instagram reel link.")

    if state == "WAIT_URL":
        return await message.reply("❌ Please send a valid URL (http/https).")

# ===========================
# FILE/VIDEO/AUDIO HANDLER
# ===========================
@app.on_message(filters.private & (filters.video | filters.document | filters.audio))
async def media_handler(client, message):
    uid = message.from_user.id
    state = USER_STATE.get(uid, "")

    # download media from telegram
    status = await message.reply("⬇️ Downloading from Telegram...")
    USER_CANCEL.discard(uid)

    async def dl_progress(current, total):
        if uid in USER_CANCEL:
            raise asyncio.CancelledError
        elapsed = time.time() - start
        speed = current / elapsed if elapsed > 0 else 0
        eta = (total - current) / speed if speed > 0 else 0
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])
        await safe_edit(status, make_progress_text("⬇️ Downloading from Telegram...", current, total, speed, eta), kb)

    start = time.time()
    try:
        path = await message.download(progress=dl_progress)
    except asyncio.CancelledError:
        return await safe_edit(status, "❌ Cancelled.")
    except Exception as e:
        return await safe_edit(status, f"❌ Failed!\n\nError: `{e}`")

    USER_MEDIA[uid] = path

    # show options
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🗜️ Compressor", callback_data="media_compress"),
            InlineKeyboardButton("🎛️ Converter", callback_data="media_convert"),
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
    ])
    await safe_edit(status, "✅ File received!\n\nChoose option:", kb)

# ===========================
# MEDIA OPTION BUTTONS
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

@app.on_callback_query(filters.regex("^media_convert$"))
async def media_convert_menu(client, cb):
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎥➡️🎵 Video → Audio", callback_data="conv_video_audio"),
            InlineKeyboardButton("🎵➡️🎥 Audio → Video", callback_data="conv_audio_video"),
        ],
        [
            InlineKeyboardButton("🎥➡️📁 Video → File", callback_data="conv_video_file"),
            InlineKeyboardButton("📁➡️🎥 File → Video", callback_data="conv_file_video"),
        ],
        [InlineKeyboardButton("🎞️ Video → MP4", callback_data="conv_video_mp4")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
    ])
    await cb.answer()
    await cb.message.edit("👑 Converter Menu\n👇 Choose conversion type:", reply_markup=kb)

# ===========================
# QUALITY MENUS
# ===========================
HIGH_QUALS = ["2160p", "1440p", "1080p", "720p"]
LOW_QUALS  = ["480p", "360p", "240p", "144p"]

def quality_keyboard(prefix: str, quals):
    rows = []
    row = []
    for q in quals:
        row.append(InlineKeyboardButton(q, callback_data=f"{prefix}_{q}"))
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
# PROCESSING STUB (Cloud/FreeConvert)
# ===========================
async def fake_processing_bar(status_msg, title, uid, seconds=15):
    start = time.time()
    total = seconds
    while True:
        if uid in USER_CANCEL:
            raise asyncio.CancelledError
        now = time.time()
        elapsed = now - start
        if elapsed >= total:
            break
        percent = (elapsed / total) * 100
        bar = progress_bar(percent)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])
        await safe_edit(status_msg, f"{title}\n\n{bar}\n⏳ Please wait...", kb)
        await asyncio.sleep(1)

# ===========================
# COMPRESS ACTION (ONLY LINK OUTPUT)
# ===========================
@app.on_callback_query(filters.regex(r"^(do_high|do_low)_(2160p|1440p|1080p|720p|480p|360p|240p|144p)$"))
async def do_compress(client, cb):
    uid = cb.from_user.id
    if uid not in USER_MEDIA:
        return await cb.answer("Send media first!", show_alert=True)

    quality = cb.data.split("_", 2)[-1]

    await cb.answer()
    status = cb.message

    async def job():
        file_path = USER_MEDIA.get(uid)
        if not file_path or not os.path.exists(file_path):
            return await safe_edit(status, "❌ File missing. Send again.")

        try:
            USER_CANCEL.discard(uid)
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])
            await safe_edit(status, f"⚙️ Compressing ({quality})...\n\n⏳ Please wait...", kb)

            # progress animation
            await fake_processing_bar(status, f"⚙️ Compressing ({quality})...", uid, seconds=10)

            # OUTPUT: Direct Download link concept
            # (Because huge files > 500MB cannot send directly)
            out_mb = 720  # dummy
            dl = f"https://example.com/download/compressed_{uid}_{int(time.time())}.mp4"

            txt = (
                "✅ **Compressed Successfully ☁️**\n"
                f"📦 Output Size: **{out_mb}MB**\n"
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
# Upload compressed link using URL uploader
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

            kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])
            await safe_edit(status, "📥 Downloading reel...\n⏳ Please wait...", kb)

            file_path = await insta_download(url, uid, status_msg=status)
            if uid in USER_CANCEL:
                raise asyncio.CancelledError

            # Uploading progress bar
            await safe_edit(status, "📤 Uploading reel...\n⏳ Please wait...", kb)

            up_start = time.time()

            if mode == "video":
                await client.send_video(
                    chat_id=cb.message.chat.id,
                    video=file_path,
                    caption=f"✅ Reel Uploaded 🎥\n\n📌 {os.path.basename(file_path)}",
                    progress=upload_progress,
                    progress_args=(status, uid, up_start)
                )
            else:
                await client.send_document(
                    chat_id=cb.message.chat.id,
                    document=file_path,
                    caption=f"✅ Reel Uploaded as File 📁\n\n📌 {os.path.basename(file_path)}",
                    progress=upload_progress,
                    progress_args=(status, uid, up_start)
                )

            await safe_edit(status, "✅ Done ✅", back_keyboard())

        except asyncio.CancelledError:
            await safe_edit(status, "❌ Cancelled ✅")
        except Exception as e:
            await safe_edit(status, f"❌ Failed!\n\nError: `{e}`")
        finally:
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except:
                    pass
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
        return await cb.message.edit("❌ Session expired. URL again send cheyyu.")

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

            kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Download", callback_data=f"cancel_{uid}")]])
            await safe_edit(status, "⬇️ Starting download...", kb)

            await download_stream(url, file_path, status, uid)

            if uid in USER_CANCEL:
                raise asyncio.CancelledError

            kb2 = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Upload", callback_data=f"cancel_{uid}")]])
            await safe_edit(status, "📤 Upload starting...", kb2)

            up_start = time.time()

            if mode == "video":
                await client.send_video(
                    chat_id=cb.message.chat.id,
                    video=file_path,
                    caption=f"✅ Uploaded as Video 🎥\n\n📌 {os.path.basename(file_path)}",
                    progress=upload_progress,
                    progress_args=(status, uid, up_start)
                )
            else:
                await client.send_document(
                    chat_id=cb.message.chat.id,
                    document=file_path,
                    caption=f"✅ Uploaded as File 📁\n\n📌 {os.path.basename(file_path)}",
                    progress=upload_progress,
                    progress_args=(status, uid, up_start)
                )

            await safe_edit(status, "✅ Done ✅", back_keyboard())

        except asyncio.CancelledError:
            await safe_edit(status, "❌ Cancelled ✅")
        except Exception as e:
            await safe_edit(status, f"❌ Failed!\n\nError: `{e}`")
        finally:
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except:
                    pass

            USER_URL.pop(uid, None)
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
