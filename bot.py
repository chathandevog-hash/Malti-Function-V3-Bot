import os
import re
import time
import json
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
MAX_URL_SIZE = 2 * 1024 * 1024 * 1024   # ✅ 2GB URL uploader
MAX_PROC_SIZE = 500 * 1024 * 1024       # ✅ 500MB compress/convert safe limit

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
        return f"{h}h, {m}m"
    if m:
        return f"{m}m, {s}s"
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
    if percent < 0:
        percent = 0
    if percent > 100:
        percent = 100

    filled = int((percent / 100) * slots)

    if percent <= 0:
        fill_icon = "⚪"
    elif percent < 34:
        fill_icon = "🔴"
    elif percent < 67:
        fill_icon = "🟠"
    elif percent < 100:
        fill_icon = "🟡"
    else:
        fill_icon = "🟢"

    bar = fill_icon * filled + "⚪" * (slots - filled)
    return f"[{bar}]"


def make_progress_text(title, done, total, speed, eta):
    percent = (done / total * 100) if total else 0
    bar = make_circle_bar(percent)
    s_str = naturalsize(int(speed)) + "/s" if speed else "0 B/s"

    return (
        f"🚀 {title} ⚡\n\n"
        f"{bar}\n\n"
        f"⌛ Done: {percent:.2f}%\n"
        f"📦 Size: {naturalsize(done)} / {naturalsize(total) if total else 'Unknown'}\n"
        f"🚀 Speed: {s_str}\n"
        f"⏱ ETA: {format_time(eta)}"
    )


def calc_reduction(old_bytes: int, new_bytes: int):
    if not old_bytes or not new_bytes:
        return 0.0
    red = (1 - (new_bytes / old_bytes)) * 100
    if red < 0:
        red = 0
    return red


# -------------------------
# Video Meta + Thumb
# -------------------------
def get_video_meta(path: str):
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,duration",
                "-of", "json",
                path
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
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
    # ✅ middle scene thumbnail
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


async def send_video_with_meta(client, chat_id, video_path, caption):
    thumb_path = os.path.splitext(video_path)[0] + "_thumb.jpg"
    try:
        await gen_thumbnail(video_path, thumb_path)
        dur, w, h = get_video_meta(video_path)

        return await client.send_video(
            chat_id=chat_id,
            video=video_path,
            caption=caption,
            supports_streaming=True,
            duration=dur if dur else None,
            width=w if w else None,
            height=h if h else None,
            thumb=thumb_path if os.path.exists(thumb_path) else None,
        )
    finally:
        if os.path.exists(thumb_path):
            try:
                os.remove(thumb_path)
            except:
                pass


# -------------------------
# Progress callbacks
# -------------------------
async def tg_download_progress(current, total, status_msg, uid, start_time):
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
            [InlineKeyboardButton("❌ Cancel Download", callback_data=f"cancel_{uid}")]
        ])
        await status_msg.edit(
            make_progress_text("Downloading from Telegram", current, total, speed, eta),
            reply_markup=kb
        )


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
        await status_msg.edit(
            make_progress_text("Uploading", current, total, speed, eta),
            reply_markup=kb
        )


# -------------------------
# URL helpers
# -------------------------
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

            # ✅ URL limit 2GB
            if total and total > MAX_URL_SIZE:
                raise Exception("URL file too large (max 2GB)")

            os.makedirs(os.path.dirname(file_path), exist_ok=True)

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
                        kb = InlineKeyboardMarkup([
                            [InlineKeyboardButton("❌ Cancel Download", callback_data=f"cancel_{uid}")]
                        ])
                        await status_msg.edit(
                            make_progress_text("Downloading", downloaded, total, speed, eta),
                            reply_markup=kb
                        )

    return downloaded, total


# -------------------------
# FFmpeg tools
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


async def run_ffmpeg(cmd):
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL
    )
    await proc.wait()
    return proc.returncode


async def convert_to_mp4(input_path: str, out_path: str):
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-map", "0:v:0?",
        "-map", "0:a:0?",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        "-pix_fmt", "yuv420p",
        out_path
    ]
    return await run_ffmpeg(cmd)


async def compress_video(input_path: str, out_path: str, quality: str):
    w, h = QUALITY_MAP[quality]
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vf", f"scale={w}:{h}",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "28",
        "-c:a", "aac",
        "-b:a", "96k",
        "-movflags", "+faststart",
        "-pix_fmt", "yuv420p",
        out_path
    ]
    return await run_ffmpeg(cmd)


async def video_to_mp3(input_path: str, out_path: str):
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vn",
        "-c:a", "libmp3lame",
        "-b:a", "128k",
        out_path
    ]
    return await run_ffmpeg(cmd)


# -------------------------
# UI Menus
# -------------------------
def kb_main_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🗜 Compressor", callback_data="menu_compress"),
            InlineKeyboardButton("👑 Converter", callback_data="menu_convert")
        ]
    ])


def kb_back(to="back_main"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=to)]])


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
# Start Message
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
        "⚠️ Compression Limit: 500MB\n\n"
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
# URL handler
# -------------------------
@app.on_message(filters.private & filters.text)
async def url_handler(client, message):
    text = message.text.strip()
    uid = message.from_user.id

    if text.startswith("/"):
        return

    if is_url(text):
        if uid in USER_TASKS and not USER_TASKS[uid].done():
            return await message.reply("⚠️ One process already running. Please wait or cancel.")

        USER_URL[uid] = text
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📁 File", callback_data="send_file"),
                InlineKeyboardButton("🎥 Video", callback_data="send_video")
            ]
        ])
        return await message.reply("✅ URL Received!\n\n👇 Select upload type:", reply_markup=kb)

    return await message.reply("❌ Send a direct URL or send a media file.")


# -------------------------
# Telegram media received
# -------------------------
@app.on_message(filters.private & filters.media)
async def file_received(client, message):
    uid = message.from_user.id

    if uid in USER_TASKS and not USER_TASKS[uid].done():
        return await message.reply("⚠️ One process already running. Please wait or cancel.")

    USER_CANCEL.discard(uid)

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

    # ✅ Compressor/Converter safe limit
    if size > MAX_PROC_SIZE:
        return await message.reply(
            f"❌ Processing limit exceeded!\n\nMax: 500MB\nYour file: {size/(1024*1024):.2f}MB"
        )

    status = await message.reply("⬇️ Starting Telegram download...")
    start_time = time.time()

    async def job():
        try:
            local_path = await message.download(
                file_name=DOWNLOAD_DIR,
                progress=tg_download_progress,
                progress_args=(status, uid, start_time)
            )

            LAST_MEDIA[uid] = {"type": media_type, "path": local_path, "size": size}

            await status.edit("✅ Media received.\n👇 Choose option:", reply_markup=kb_main_menu())

        except asyncio.CancelledError:
            try:
                await status.edit("❌ Download Cancelled ✅")
            except:
                pass
        except Exception as e:
            try:
                await status.edit(f"❌ Telegram download failed!\n\nError: `{e}`")
            except:
                pass

    t = asyncio.create_task(job())
    USER_TASKS[uid] = t


# -------------------------
# Back buttons
# -------------------------
@app.on_callback_query(filters.regex("^back_main$"))
async def back_main(client, cb):
    await cb.message.edit("✅ Choose option:", reply_markup=kb_main_menu())


@app.on_callback_query(filters.regex("^back_convert$"))
async def back_convert(client, cb):
    await cb.message.edit("👑 Converter Menu\n👇 Choose conversion type:", reply_markup=kb_converter_menu())


@app.on_callback_query(filters.regex("^back_compress$"))
async def back_compress(client, cb):
    await cb.message.edit("🗜 Choose Compression Type:", reply_markup=kb_compress_menu())


# -------------------------
# Menus
# -------------------------
def kb_compress_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🟢 Compress Higher", callback_data="compress_high"),
            InlineKeyboardButton("🔴 Compress Lower", callback_data="compress_low")
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


@app.on_callback_query(filters.regex("^menu_compress$"))
async def menu_compress(client, cb):
    await cb.message.edit("🗜 Choose Compression Type:", reply_markup=kb_compress_menu())


@app.on_callback_query(filters.regex("^menu_convert$"))
async def menu_convert(client, cb):
    await cb.message.edit("👑 Converter Menu\n👇 Choose conversion type:", reply_markup=kb_converter_menu())


@app.on_callback_query(filters.regex("^compress_high$"))
async def compress_high(client, cb):
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📺 2160p", callback_data="q_2160"),
            InlineKeyboardButton("📺 1440p", callback_data="q_1440"),
            InlineKeyboardButton("📺 1080p", callback_data="q_1080"),
        ],
        [
            InlineKeyboardButton("📺 720p", callback_data="q_720"),
            InlineKeyboardButton("📺 480p", callback_data="q_480"),
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="back_compress")]
    ])
    await cb.message.edit("✨ Select Higher Quality:", reply_markup=kb)


@app.on_callback_query(filters.regex("^compress_low$"))
async def compress_low(client, cb):
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📉 360p", callback_data="q_360"),
            InlineKeyboardButton("📉 240p", callback_data="q_240"),
            InlineKeyboardButton("📉 144p", callback_data="q_144"),
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="back_compress")]
    ])
    await cb.message.edit("📉 Select Lower Quality:", reply_markup=kb)


# -------------------------
# Compressor Action
# -------------------------
@app.on_callback_query(filters.regex(r"^q_\d+$"))
async def quality_selected(client, cb):
    uid = cb.from_user.id
    media = LAST_MEDIA.get(uid)

    if not media or media["type"] != "video":
        return await cb.answer("Send a video first.", show_alert=True)

    q = cb.data.split("_", 1)[1]
    in_path = media["path"]

    status = await cb.message.reply("🗜 Compressing... Please wait ⏳")

    async def job():
        out_path = None
        try:
            old_size = os.path.getsize(in_path)
            out_path = os.path.splitext(in_path)[0] + f"_{q}p.mp4"

            rc = await compress_video(in_path, out_path, q)
            if rc != 0 or not os.path.exists(out_path):
                raise Exception("Compression failed!")

            new_size = os.path.getsize(out_path)
            reduced = calc_reduction(old_size, new_size)

            await send_video_with_meta(
                client,
                cb.message.chat.id,
                out_path,
                caption=(
                    f"✅ **Compression Finished** 🗜\n\n"
                    f"📺 Quality: **{q}p**\n"
                    f"📦 Original: **{naturalsize(old_size)}**\n"
                    f"📉 New: **{naturalsize(new_size)}**\n"
                    f"💯 Reduced: **{reduced:.2f}%**\n"
                    f"📌 {os.path.basename(out_path)}"
                )
            )

            await status.edit("✅ Done ✅")

        except Exception as e:
            await status.edit(f"❌ Failed!\n\nError: `{e}`")
        finally:
            if out_path and os.path.exists(out_path):
                try:
                    os.remove(out_path)
                except:
                    pass

    asyncio.create_task(job())


# -------------------------
# Converter Actions
# -------------------------
@app.on_callback_query(filters.regex("^conv_v_mp3$"))
async def conv_v_mp3(client, cb):
    uid = cb.from_user.id
    media = LAST_MEDIA.get(uid)
    if not media or media["type"] != "video":
        return await cb.answer("Send a video first.", show_alert=True)

    in_path = media["path"]
    status = await cb.message.reply("🎵 Converting Video → Audio (MP3)...")

    async def job():
        out_path = None
        try:
            out_path = os.path.splitext(in_path)[0] + ".mp3"
            rc = await video_to_mp3(in_path, out_path)
            if rc != 0 or not os.path.exists(out_path):
                raise Exception("Video to MP3 failed!")

            await client.send_audio(
                chat_id=cb.message.chat.id,
                audio=out_path,
                caption=f"✅ Video → Audio (MP3)\n🎵 {os.path.basename(out_path)}"
            )
            await status.edit("✅ Done ✅")

        except Exception as e:
            await status.edit(f"❌ Failed!\n\nError: `{e}`")
        finally:
            if out_path and os.path.exists(out_path):
                try:
                    os.remove(out_path)
                except:
                    pass

    asyncio.create_task(job())


@app.on_callback_query(filters.regex("^conv_f_mp4$"))
async def conv_f_mp4(client, cb):
    uid = cb.from_user.id
    media = LAST_MEDIA.get(uid)
    if not media:
        return await cb.answer("Send a file first.", show_alert=True)

    in_path = media["path"]
    status = await cb.message.reply("🎬 Converting File → MP4...")

    async def job():
        out_path = None
        try:
            out_path = os.path.splitext(in_path)[0] + "_file.mp4"
            rc = await convert_to_mp4(in_path, out_path)
            if rc != 0 or not os.path.exists(out_path):
                raise Exception("File to MP4 failed!")

            await send_video_with_meta(
                client,
                cb.message.chat.id,
                out_path,
                caption=f"✅ File → MP4\n🎬 {os.path.basename(out_path)}"
            )
            await status.edit("✅ Done ✅")

        except Exception as e:
            await status.edit(f"❌ Failed!\n\nError: `{e}`")
        finally:
            if out_path and os.path.exists(out_path):
                try:
                    os.remove(out_path)
                except:
                    pass

    asyncio.create_task(job())


@app.on_callback_query(filters.regex("^conv_v_file$"))
async def conv_v_file(client, cb):
    uid = cb.from_user.id
    media = LAST_MEDIA.get(uid)
    if not media or media["type"] != "video":
        return await cb.answer("Send a video first.", show_alert=True)

    await client.send_document(
        chat_id=cb.message.chat.id,
        document=media["path"],
        caption="✅ Video → File (Document) 📁"
    )
    await cb.answer("Done ✅")


@app.on_callback_query(filters.regex("^conv_v_mp4$"))
async def conv_v_mp4(client, cb):
    uid = cb.from_user.id
    media = LAST_MEDIA.get(uid)
    if not media or media["type"] != "video":
        return await cb.answer("Send a video first.", show_alert=True)

    in_path = media["path"]
    if in_path.lower().endswith(".mp4"):
        return await cb.answer("Already MP4 ✅", show_alert=True)

    status = await cb.message.reply("🎬 Converting Video → MP4...")

    async def job():
        out_path = None
        try:
            out_path = os.path.splitext(in_path)[0] + "_converted.mp4"
            rc = await convert_to_mp4(in_path, out_path)
            if rc != 0 or not os.path.exists(out_path):
                raise Exception("MP4 conversion failed!")

            await send_video_with_meta(
                client,
                cb.message.chat.id,
                out_path,
                caption=f"✅ Video → MP4\n🎬 {os.path.basename(out_path)}"
            )

            await status.edit("✅ Done ✅")

        except Exception as e:
            await status.edit(f"❌ Failed!\n\nError: `{e}`")
        finally:
            if out_path and os.path.exists(out_path):
                try:
                    os.remove(out_path)
                except:
                    pass

    asyncio.create_task(job())


# -------------------------
# URL Upload Selection
# -------------------------
@app.on_callback_query(filters.regex("^(send_file|send_video)$"))
async def send_type_selected(client, cb):
    uid = cb.from_user.id
    if uid not in USER_URL:
        return await cb.message.edit("❌ Session expired. Send URL again.")

    if uid in USER_TASKS and not USER_TASKS[uid].done():
        return await cb.message.reply("⚠️ One process already running. Please wait or cancel.")

    url = USER_URL[uid]
    mode = cb.data.replace("send_", "")

    await cb.answer()
    await cb.message.edit(f"✅ Selected: **{mode.upper()}**")

    status = await cb.message.reply("⚙️ Starting...")

    async def job():
        file_path = None
        mp4_out = None
        try:
            filename, total = await get_filename_and_size(url)

            # ✅ URL limit 2GB only
            if total and total > MAX_URL_SIZE:
                return await status.edit("❌ URL file too large! Max: 2GB")

            if "." not in filename:
                filename += ".bin"

            file_path = os.path.join(DOWNLOAD_DIR, f"{uid}_{int(time.time())}_{filename}")

            await status.edit("⬇️ Starting download...")
            await download_stream(url, file_path, status, uid)

            upload_path = file_path

            if mode == "video":
                if not file_path.lower().endswith(".mp4"):
                    mp4_out = os.path.splitext(file_path)[0] + "_mp4.mp4"
                    await status.edit("🎬 Converting to MP4...")
                    rc = await convert_to_mp4(file_path, mp4_out)
                    if rc != 0 or not os.path.exists(mp4_out):
                        raise Exception("MP4 conversion failed!")
                    upload_path = mp4_out

                await status.edit("📤 Uploading video...")
                await send_video_with_meta(
                    client,
                    cb.message.chat.id,
                    upload_path,
                    caption=f"✅ Uploaded as MP4 Video\n📌 {os.path.basename(upload_path)}"
                )

            else:
                up_start = time.time()
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Upload", callback_data=f"cancel_{uid}")]])
                await status.edit("📤 Uploading...", reply_markup=kb)

                await client.send_document(
                    chat_id=cb.message.chat.id,
                    document=upload_path,
                    caption=f"✅ Uploaded as File\n📌 {os.path.basename(upload_path)}",
                    progress=upload_progress,
                    progress_args=(status, uid, up_start)
                )

            await cb.message.reply("✅ Choose option:", reply_markup=kb_main_menu())

            await status.edit("✅ Done ✅")
            await asyncio.sleep(2)
            await status.delete()

        except Exception as e:
            try:
                await status.edit(f"❌ Failed!\n\nError: `{e}`")
            except:
                pass
        finally:
            USER_URL.pop(uid, None)
            USER_TASKS.pop(uid, None)
            USER_CANCEL.discard(uid)

            for p in [file_path, mp4_out]:
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except:
                        pass

    t = asyncio.create_task(job())
    USER_TASKS[uid] = t


# -------------------------
# Main
# -------------------------
if __name__ == "__main__":
    if not BOT_TOKEN or not API_ID or not API_HASH:
        print("❌ Please set BOT_TOKEN, API_ID, API_HASH in env!")
        raise SystemExit

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    print("✅ Bot started...")
    app.run()
