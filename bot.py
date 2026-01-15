import os
import re
import time
import json
import zipfile
import asyncio
import aiohttp
import subprocess
from urllib.parse import urlparse, unquote

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import BOT_TOKEN, API_ID, API_HASH, DOWNLOAD_DIR


# -------------------------
# Limits
# -------------------------
MAX_URL_SIZE = 2 * 1024 * 1024 * 1024      # ✅ 2GB URL uploader
MAX_COMPRESS_SIZE = 700 * 1024 * 1024      # ✅ 700MB compressor
MAX_CONVERT_SIZE = 500 * 1024 * 1024       # ✅ 500MB converter


# -------------------------
# Storage
# -------------------------
USER_URL = {}
USER_TASKS = {}
USER_CANCEL = set()
LAST_MEDIA = {}  # uid -> {"type": "video"|"file"|"audio", "path": "...", "size": int}


# -------------------------
# Helpers
# -------------------------
def ensure_dir(p):
    os.makedirs(p, exist_ok=True)

def clean_file(p):
    if p and os.path.exists(p):
        try:
            os.remove(p)
        except:
            pass

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
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    i = 0
    n = float(num_bytes)
    while n >= 1024 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    return f"{n:.2f} {units[i]}"

def make_circle_bar(percent: float, slots: int = 14):
    percent = max(0, min(100, percent))
    filled = int((percent / 100) * slots)

    if percent <= 0:
        icon = "⚪"
    elif percent < 34:
        icon = "🔴"
    elif percent < 67:
        icon = "🟠"
    elif percent < 100:
        icon = "🟡"
    else:
        icon = "🟢"

    return f"[{icon * filled}{'⚪' * (slots - filled)}]"

def make_progress_text(title, done, total, speed, eta):
    percent = (done / total * 100) if total else 0
    bar = make_circle_bar(percent)
    s_str = naturalsize(int(speed)) + "/s" if speed else "0 B/s"

    return (
        f"✨ **{title}**\n\n"
        f"{bar}\n\n"
        f"📊 Progress: **{percent:.2f}%**\n"
        f"📦 Size: **{naturalsize(done)} / {naturalsize(total) if total else 'Unknown'}**\n"
        f"⚡ Speed: **{s_str}**\n"
        f"⏳ ETA: **{format_time(eta)}**"
    )

def calc_reduction(old_bytes: int, new_bytes: int):
    if not old_bytes or not new_bytes:
        return 0.0
    red = (1 - (new_bytes / old_bytes)) * 100
    return red if red > 0 else 0


# -------------------------
# Video Meta + Thumb
# -------------------------
def get_video_meta(path: str):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,duration", "-of", "json", path],
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
    cmd = ["ffmpeg", "-y", "-ss", str(ss), "-i", input_path,
           "-frames:v", "1", "-vf", "scale=640:-1", "-q:v", "2", out_thumb]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    await proc.wait()
    return os.path.exists(out_thumb)

async def send_video_with_meta(client, chat_id, video_path, caption):
    thumb = os.path.splitext(video_path)[0] + "_thumb.jpg"
    try:
        await gen_thumbnail(video_path, thumb)
        dur, w, h = get_video_meta(video_path)

        return await client.send_video(
            chat_id=chat_id,
            video=video_path,
            caption=caption,
            supports_streaming=True,
            duration=dur if dur else None,
            width=w if w else None,
            height=h if h else None,
            thumb=thumb if os.path.exists(thumb) else None
        )
    finally:
        clean_file(thumb)


# -------------------------
# Telegram progress (Stable)
# -------------------------
async def tg_download_progress(current, total, status_msg, uid, start_time):
    if uid in USER_CANCEL:
        raise asyncio.CancelledError

    elapsed = time.time() - start_time
    speed = current / elapsed if elapsed > 0 else 0
    eta = (total - current) / speed if total and speed > 0 else 0

    now = time.time()
    if not hasattr(status_msg, "_last_edit"):
        status_msg._last_edit = 0

    # ✅ Stable: edit every 4 sec
    if now - status_msg._last_edit > 4:
        status_msg._last_edit = now
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])
        await status_msg.edit(
            make_progress_text("⬇️ Downloading (Telegram)", current, total, speed, eta),
            reply_markup=kb
        )

async def upload_progress(current, total, status_msg, uid, start_time):
    if uid in USER_CANCEL:
        raise asyncio.CancelledError

    elapsed = time.time() - start_time
    speed = current / elapsed if elapsed > 0 else 0
    eta = (total - current) / speed if total and speed > 0 else 0

    now = time.time()
    if not hasattr(status_msg, "_last_edit"):
        status_msg._last_edit = 0

    if now - status_msg._last_edit > 4:
        status_msg._last_edit = now
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])
        await status_msg.edit(
            make_progress_text("📤 Uploading", current, total, speed, eta),
            reply_markup=kb
        )


# -------------------------
# FFmpeg Progress
# -------------------------
def parse_ffmpeg_time(line: str):
    if "time=" not in line:
        return None
    try:
        t = line.split("time=")[-1].split(" ")[0].strip()
        hh, mm, ss = t.split(":")
        return int(hh) * 3600 + int(mm) * 60 + float(ss)
    except:
        return None

async def ffmpeg_with_progress(cmd, status_msg, uid, title: str, total_duration: int):
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])
    start = time.time()
    last = 0

    await status_msg.edit(f"⚙️ **{title}**\n\n⏳ Please wait...", reply_markup=kb)

    while True:
        if uid in USER_CANCEL:
            try:
                proc.kill()
            except:
                pass
            raise asyncio.CancelledError

        line = await proc.stderr.readline()
        if not line:
            break

        text = line.decode("utf-8", errors="ignore")
        sec = parse_ffmpeg_time(text)
        if sec is None or total_duration <= 0:
            continue

        percent = min(100.0, (sec / total_duration) * 100)
        elapsed = time.time() - start
        speed = percent / elapsed if elapsed > 0 else 0
        eta = (100 - percent) / speed if speed > 0 else 0

        if time.time() - last > 2:
            last = time.time()
            await status_msg.edit(make_progress_text(title, percent, 100, 1, eta), reply_markup=kb)

    await proc.wait()
    return proc.returncode


# -------------------------
# URL Download
# -------------------------
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

            if total and total > MAX_URL_SIZE:
                raise Exception("URL file too large (max 2GB)")

            ensure_dir(os.path.dirname(file_path))
            with open(file_path, "wb") as f:
                async for chunk in r.content.iter_chunked(1024 * 256):
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
                        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])
                        await status_msg.edit(
                            make_progress_text("⬇️ Downloading (URL)", downloaded, total, speed, eta),
                            reply_markup=kb
                        )


# -------------------------
# ZIP file compress
# -------------------------
async def compress_file_zip(input_path: str, out_zip: str):
    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.write(input_path, arcname=os.path.basename(input_path))
    return os.path.exists(out_zip)


# -------------------------
# UI Keyboards
# -------------------------
QUALITY_MAP = {
    "2160": (3840, 2160),
    "1440": (2560, 1440),
    "1080": (1920, 1080),
    "720":  (1280, 720),
    "480":  (854, 480),
    "360":  (640, 360),
    "240":  (426, 240),
    "144":  (256, 144),
}

def kb_main_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🗜 Compressor", callback_data="menu_compress"),
            InlineKeyboardButton("👑 Converter", callback_data="menu_convert")
        ]
    ])

def kb_compress_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎥 Video Compress", callback_data="compress_video_menu"),
            InlineKeyboardButton("📁 File Compress (ZIP)", callback_data="compress_file_zip")
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
    ])

def kb_converter_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎥➡️🎵 Video → Audio", callback_data="conv_v_mp3"),
            InlineKeyboardButton("📁➡️🎥 File → MP4", callback_data="conv_f_mp4")
        ],
        [
            InlineKeyboardButton("🎥➡️📁 Video → File", callback_data="conv_v_file"),
            InlineKeyboardButton("🎥➡️🎬 Video → MP4", callback_data="conv_v_mp4")
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
    ])


# -------------------------
# Bot init
# -------------------------
app = Client(
    "UrlUploaderBot",
    bot_token=BOT_TOKEN,
    api_id=API_ID,
    api_hash=API_HASH
)


# -------------------------
# /start
# -------------------------
@app.on_message(filters.private & filters.command("start"))
async def start_cmd(client, message):
    await message.reply(
        "✨ Welcome to Multifunctional Bot! 🤖💫\n"
        "Here you can do multiple things in one bot 🚀\n\n"
        "🌐 URL Uploader\n"
        "➜ Send any direct link and I will upload it for you instantly ✅\n\n"
        "🗜️ Compressor\n"
        "➜ Reduce file/video size easily without hassle ⚡\n"
        "⚠️ Compression Limit: 700MB\n\n"
        "🎛️ Converter\n"
        "➜ Convert your files into different formats (mp4 / mp3 / mkv etc.) 🎬🎵\n"
        "⚠️ Conversion Limit: 500MB\n\n"
        "📌 How to use?\n"
        "1️⃣ Send a File / Video / Audio / URL\n"
        "2️⃣ Select your needed option ✅\n"
        "3️⃣ Wait for processing ⏳\n"
        "4️⃣ Get your output 🎉\n\n"
        "💡 Use /help for all commands & guide 🛠️\n"
        "🚀 Now send something to start 👇😊"
    )


# -------------------------
# Cancel
# -------------------------
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


# -------------------------
# Back buttons
# -------------------------
@app.on_callback_query(filters.regex("^back_main$"))
async def back_main(client, cb):
    await cb.message.edit("✅ Choose option:", reply_markup=kb_main_menu())


# -------------------------
# URL receive
# -------------------------
@app.on_message(filters.private & filters.text)
async def url_handler(client, message):
    uid = message.from_user.id
    text = message.text.strip()

    if text.startswith("/"):
        return

    if is_url(text):
        USER_URL[uid] = text
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📁 File", callback_data="send_file"),
                InlineKeyboardButton("🎥 Video", callback_data="send_video")
            ]
        ])
        return await message.reply("✅ URL Received!\n\n👇 Select upload type:", reply_markup=kb)

    return await message.reply("❌ Send a valid URL or send a media file.")


# -------------------------
# Media receive
# -------------------------
@app.on_message(filters.private & filters.media)
async def media_handler(client, message):
    uid = message.from_user.id

    if uid in USER_TASKS and not USER_TASKS[uid].done():
        return await message.reply("⚠️ One process already running. Please wait or cancel.")

    USER_CANCEL.discard(uid)

    media_type, size = None, 0
    if message.video:
        media_type = "video"
        size = message.video.file_size or 0
    elif message.document:
        media_type = "file"
        size = message.document.file_size or 0
    elif message.audio:
        media_type = "audio"
        size = message.audio.file_size or 0
    else:
        return await message.reply("❌ Unsupported media type.")

    if size > MAX_COMPRESS_SIZE:
        return await message.reply("❌ File too large! Max 700MB")

    status = await message.reply("⬇️ Starting Telegram download...")
    start = time.time()

    async def job():
        local_path = None
        try:
            local_path = await message.download(
                file_name=DOWNLOAD_DIR,
                block_size=1024 * 512,  # ✅ FIX
                progress=tg_download_progress,
                progress_args=(status, uid, start)
            )

            LAST_MEDIA[uid] = {"type": media_type, "path": local_path, "size": size}
            await status.edit("✅ Media received.\n👇 Choose option:", reply_markup=kb_main_menu())

        except Exception as e:
            await status.edit(f"❌ Failed!\n\nError: `{e}`")

    t = asyncio.create_task(job())
    USER_TASKS[uid] = t


# -------------------------
# Menu open
# -------------------------
@app.on_callback_query(filters.regex("^menu_compress$"))
async def menu_compress(client, cb):
    await cb.message.edit("🗜 Select compression type:", reply_markup=kb_compress_menu())

@app.on_callback_query(filters.regex("^menu_convert$"))
async def menu_convert(client, cb):
    await cb.message.edit("👑 Converter Menu:", reply_markup=kb_converter_menu())


# -------------------------
# Compressor: Video menu (simple)
# -------------------------
@app.on_callback_query(filters.regex("^compress_video_menu$"))
async def compress_video_menu(client, cb):
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📺 720p", callback_data="q_720"),
            InlineKeyboardButton("📺 480p", callback_data="q_480"),
            InlineKeyboardButton("📉 360p", callback_data="q_360"),
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="menu_compress")]
    ])
    await cb.message.edit("🎥 Select quality:", reply_markup=kb)


# -------------------------
# Compressor action
# -------------------------
@app.on_callback_query(filters.regex(r"^q_\d+$"))
async def compress_video(client, cb):
    uid = cb.from_user.id
    media = LAST_MEDIA.get(uid)
    if not media or media["type"] != "video":
        return await cb.answer("❌ Send a video first", show_alert=True)

    in_path = media["path"]
    q = cb.data.split("_", 1)[1]
    out_path = os.path.splitext(in_path)[0] + f"_{q}p.mp4"

    status = await cb.message.reply("⚙️ Starting compression...")
    dur, _, _ = get_video_meta(in_path)
    dur = dur or 60

    async def job():
        try:
            old_size = os.path.getsize(in_path)
            cmd = [
                "ffmpeg", "-y",
                "-i", in_path,
                "-vf", f"scale={QUALITY_MAP[q][0]}:{QUALITY_MAP[q][1]}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                "-c:a", "aac", "-b:a", "96k",
                "-movflags", "+faststart", "-pix_fmt", "yuv420p",
                out_path
            ]

            rc = await ffmpeg_with_progress(cmd, status, uid, f"Compressing to {q}p", dur)
            if rc != 0 or not os.path.exists(out_path):
                raise Exception("Compression failed")

            new_size = os.path.getsize(out_path)
            reduced = calc_reduction(old_size, new_size)

            await send_video_with_meta(
                client,
                cb.message.chat.id,
                out_path,
                caption=(
                    f"✅ Compression Finished 🗜\n\n"
                    f"📺 Quality: {q}p\n"
                    f"📦 Original: {naturalsize(old_size)}\n"
                    f"📉 New: {naturalsize(new_size)}\n"
                    f"💯 Reduced: {reduced:.2f}%"
                )
            )

            await status.edit("✅ Completed Successfully 🎉")

        except Exception as e:
            await status.edit(f"❌ Failed!\n\nError: `{e}`")
        finally:
            clean_file(out_path)
            USER_CANCEL.discard(uid)

    asyncio.create_task(job())


# -------------------------
# File compressor ZIP
# -------------------------
@app.on_callback_query(filters.regex("^compress_file_zip$"))
async def compress_file(client, cb):
    uid = cb.from_user.id
    media = LAST_MEDIA.get(uid)
    if not media:
        return await cb.answer("❌ Send file first", show_alert=True)

    in_path = media["path"]
    out_zip = os.path.splitext(in_path)[0] + "_compressed.zip"
    status = await cb.message.reply("📦 Compressing file to ZIP...")

    async def job():
        try:
            old_size = os.path.getsize(in_path)
            await compress_file_zip(in_path, out_zip)

            new_size = os.path.getsize(out_zip)
            reduced = calc_reduction(old_size, new_size)

            await client.send_document(
                chat_id=cb.message.chat.id,
                document=out_zip,
                caption=(
                    f"✅ File Compressed (ZIP) 📦\n\n"
                    f"📦 Original: {naturalsize(old_size)}\n"
                    f"📉 New: {naturalsize(new_size)}\n"
                    f"💯 Reduced: {reduced:.2f}%"
                )
            )
            await status.edit("✅ Completed Successfully 🎉")
        except Exception as e:
            await status.edit(f"❌ Failed!\n\nError: `{e}`")
        finally:
            clean_file(out_zip)

    asyncio.create_task(job())


# -------------------------
# Converter: video->mp3
# -------------------------
@app.on_callback_query(filters.regex("^conv_v_mp3$"))
async def conv_v_mp3(client, cb):
    uid = cb.from_user.id
    media = LAST_MEDIA.get(uid)
    if not media or media["type"] != "video":
        return await cb.answer("❌ Send video first", show_alert=True)

    if media["size"] > MAX_CONVERT_SIZE:
        return await cb.answer("❌ Convert max 500MB", show_alert=True)

    in_path = media["path"]
    out_path = os.path.splitext(in_path)[0] + ".mp3"
    status = await cb.message.reply("⚙️ Converting Video → Audio (MP3)...")

    async def job():
        try:
            dur, _, _ = get_video_meta(in_path)
            dur = dur or 60

            cmd = ["ffmpeg", "-y", "-i", in_path, "-vn", "-c:a", "libmp3lame", "-b:a", "128k", out_path]
            rc = await ffmpeg_with_progress(cmd, status, uid, "Converting to MP3", dur)
            if rc != 0 or not os.path.exists(out_path):
                raise Exception("MP3 conversion failed")

            await client.send_audio(cb.message.chat.id, out_path, caption="✅ Video → Audio (MP3) 🎵")
            await status.edit("✅ Completed Successfully 🎉")
        except Exception as e:
            await status.edit(f"❌ Failed!\n\nError: `{e}`")
        finally:
            clean_file(out_path)

    asyncio.create_task(job())


# -------------------------
# Converter: file->mp4
# -------------------------
@app.on_callback_query(filters.regex("^conv_f_mp4$"))
async def conv_f_mp4(client, cb):
    uid = cb.from_user.id
    media = LAST_MEDIA.get(uid)
    if not media:
        return await cb.answer("❌ Send file first", show_alert=True)

    if media["size"] > MAX_CONVERT_SIZE:
        return await cb.answer("❌ Convert max 500MB", show_alert=True)

    in_path = media["path"]
    out_path = os.path.splitext(in_path)[0] + "_file.mp4"
    status = await cb.message.reply("⚙️ Converting File → MP4...")

    async def job():
        try:
            dur, _, _ = get_video_meta(in_path)
            dur = dur or 60

            cmd = [
                "ffmpeg", "-y", "-i", in_path,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                out_path
            ]

            rc = await ffmpeg_with_progress(cmd, status, uid, "Converting File → MP4", dur)
            if rc != 0 or not os.path.exists(out_path):
                raise Exception("MP4 conversion failed")

            await send_video_with_meta(client, cb.message.chat.id, out_path, caption="✅ File → MP4 🎬")
            await status.edit("✅ Completed Successfully 🎉")
        except Exception as e:
            await status.edit(f"❌ Failed!\n\nError: `{e}`")
        finally:
            clean_file(out_path)

    asyncio.create_task(job())


# -------------------------
# Converter: video->file
# -------------------------
@app.on_callback_query(filters.regex("^conv_v_file$"))
async def conv_v_file(client, cb):
    uid = cb.from_user.id
    media = LAST_MEDIA.get(uid)
    if not media or media["type"] != "video":
        return await cb.answer("❌ Send video first", show_alert=True)

    if media["size"] > MAX_CONVERT_SIZE:
        return await cb.answer("❌ Convert max 500MB", show_alert=True)

    await client.send_document(cb.message.chat.id, media["path"], caption="✅ Video → File 📁")
    await cb.answer("✅ Done")


# -------------------------
# Converter: video->mp4
# -------------------------
@app.on_callback_query(filters.regex("^conv_v_mp4$"))
async def conv_v_mp4(client, cb):
    uid = cb.from_user.id
    media = LAST_MEDIA.get(uid)
    if not media or media["type"] != "video":
        return await cb.answer("❌ Send video first", show_alert=True)

    if media["size"] > MAX_CONVERT_SIZE:
        return await cb.answer("❌ Convert max 500MB", show_alert=True)

    in_path = media["path"]
    if in_path.lower().endswith(".mp4"):
        return await cb.answer("Already MP4 ✅", show_alert=True)

    out_path = os.path.splitext(in_path)[0] + "_converted.mp4"
    status = await cb.message.reply("⚙️ Converting Video → MP4...")

    async def job():
        try:
            dur, _, _ = get_video_meta(in_path)
            dur = dur or 60

            cmd = [
                "ffmpeg", "-y", "-i", in_path,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                out_path
            ]
            rc = await ffmpeg_with_progress(cmd, status, uid, "Converting to MP4", dur)
            if rc != 0 or not os.path.exists(out_path):
                raise Exception("MP4 conversion failed")

            await send_video_with_meta(client, cb.message.chat.id, out_path, caption="✅ Video → MP4 🎬")
            await status.edit("✅ Completed Successfully 🎉")
        except Exception as e:
            await status.edit(f"❌ Failed!\n\nError: `{e}`")
        finally:
            clean_file(out_path)

    asyncio.create_task(job())


# -------------------------
# URL Upload Selection
# -------------------------
@app.on_callback_query(filters.regex("^(send_file|send_video)$"))
async def send_type_selected(client, cb):
    uid = cb.from_user.id
    if uid not in USER_URL:
        return await cb.message.edit("❌ Session expired. Send URL again.")

    url = USER_URL[uid]
    mode = cb.data.replace("send_", "")

    await cb.answer()
    await cb.message.edit(f"✅ Selected: **{mode.upper()}**")

    status = await cb.message.reply("⚙️ Starting...")

    async def job():
        file_path = None
        try:
            filename, total = await get_filename_and_size(url)
            if total and total > MAX_URL_SIZE:
                return await status.edit("❌ URL file too large! Max: 2GB")

            if "." not in filename:
                filename += ".bin"

            file_path = os.path.join(DOWNLOAD_DIR, f"{uid}_{int(time.time())}_{filename}")

            await download_stream(url, file_path, status, uid)

            if mode == "video":
                await status.edit("📤 Uploading video...")
                await send_video_with_meta(client, cb.message.chat.id, file_path, caption="✅ URL Uploaded 🎥")
            else:
                up_start = time.time()
                await client.send_document(
                    chat_id=cb.message.chat.id,
                    document=file_path,
                    caption="✅ URL Uploaded 📁",
                    progress=upload_progress,
                    progress_args=(status, uid, up_start)
                )

            await cb.message.reply("✅ Choose option:", reply_markup=kb_main_menu())
            await status.edit("✅ Completed Successfully 🎉")
            await asyncio.sleep(2)
            await status.delete()

        except Exception as e:
            await status.edit(f"❌ Failed!\n\nError: `{e}`")
        finally:
            USER_URL.pop(uid, None)
            USER_CANCEL.discard(uid)
            clean_file(file_path)

    t = asyncio.create_task(job())
    USER_TASKS[uid] = t


# -------------------------
# Main
# -------------------------
if __name__ == "__main__":
    ensure_dir(DOWNLOAD_DIR)

    if not BOT_TOKEN or not API_ID or not API_HASH:
        print("❌ Please set BOT_TOKEN, API_ID, API_HASH in env!")
        raise SystemExit

    print("✅ Bot started...")
    app.run()
