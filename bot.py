import os
import re
import time
import asyncio
import aiohttp
import subprocess
from urllib.parse import urlparse, unquote

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import BOT_TOKEN, API_ID, API_HASH, DOWNLOAD_DIR

# -------------------------
# Storage
# -------------------------
USER_URL = {}
USER_TASKS = {}
USER_CANCEL = set()

LAST_MEDIA = {}  # uid -> {"type": "video"|"file"|"audio", "path": "..."}

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
    # lightweight size formatter (no extra packages)
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

    done_str = f"{percent:.2f}%"
    d_str = naturalsize(done)
    t_str = naturalsize(total) if total else "Unknown"
    s_str = naturalsize(int(speed)) + "/s" if speed else "0 B/s"

    return (
        f"🚀 {title} ⚡\n\n"
        f"{bar}\n\n"
        f"⌛ Done: {done_str}\n"
        f"📦 Size: {d_str} / {t_str}\n"
        f"🚀 Speed: {s_str}\n"
        f"⏱ ETA: {format_time(eta)}"
    )


def make_ffmpeg_text(title, percent, out_size, total_size, speed_x, eta):
    bar = make_circle_bar(percent)
    return (
        f"🎬 {title}\n\n"
        f"{bar}\n\n"
        f"⌛ Done: {percent:.2f}%\n"
        f"📦 Size: {naturalsize(out_size)} / {naturalsize(total_size)}\n"
        f"⚡ Speed: {speed_x:.2f}x\n"
        f"⏱ ETA: {format_time(eta)}"
    )


def calc_reduction(old_bytes: int, new_bytes: int):
    if not old_bytes or not new_bytes:
        return 0.0
    if old_bytes <= 0:
        return 0.0
    red = (1 - (new_bytes / old_bytes)) * 100
    if red < 0:
        red = 0
    return red


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
# FFprobe duration (exact)
# -------------------------
def get_duration_seconds(path: str) -> float:
    """
    returns duration in seconds using ffprobe
    """
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        dur = float(result.stdout.strip())
        if dur > 0:
            return dur
    except:
        pass
    return 0.0


# -------------------------
# FFmpeg runner (exact %)
# -------------------------
async def run_ffmpeg_with_progress(cmd, status_msg, uid, title, in_path=None):
    """
    Uses ffmpeg -progress pipe:1 for real progress + duration based percent.
    """
    USER_CANCEL.discard(uid)

    duration = 0.0
    if in_path and os.path.exists(in_path):
        duration = get_duration_seconds(in_path)

    # total expected size unknown -> show Unknown until end
    total_size = os.path.getsize(in_path) if in_path and os.path.exists(in_path) else 0

    cmd2 = cmd[:] + ["-progress", "pipe:1", "-nostats"]

    proc = await asyncio.create_subprocess_exec(
        *cmd2,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL
    )

    out_time = 0.0
    out_size = 0
    speed_x = 0.0

    start_time = time.time()
    last_edit = 0

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]
    ])

    async def update(percent, eta):
        nonlocal last_edit
        if time.time() - last_edit < 2:
            return
        last_edit = time.time()
        text = make_ffmpeg_text(title, percent, out_size, total_size, speed_x, eta)
        try:
            await status_msg.edit(text, reply_markup=kb)
        except:
            pass

    try:
        while True:
            if uid in USER_CANCEL:
                try:
                    proc.kill()
                except:
                    pass
                raise asyncio.CancelledError

            line = await proc.stdout.readline()
            if not line:
                break

            line = line.decode("utf-8", errors="ignore").strip()
            if "=" not in line:
                continue

            k, v = line.split("=", 1)

            if k == "total_size":
                try:
                    out_size = int(v)
                except:
                    pass

            elif k == "speed":
                # like 1.25x
                try:
                    if v.endswith("x"):
                        speed_x = float(v[:-1])
                    else:
                        speed_x = float(v)
                except:
                    pass

            elif k == "out_time_ms":
                try:
                    out_time = int(v) / 1000000.0
                except:
                    out_time = 0.0

                if duration > 0:
                    percent = min(99.99, (out_time / duration) * 100)
                    remaining = max(duration - out_time, 0)
                    eta = int(remaining / speed_x) if speed_x > 0 else int(remaining)
                else:
                    # fallback smooth
                    percent = min(99.0, (out_time % 100))
                    eta = 0

                await update(percent, eta)

            elif k == "progress" and v == "end":
                await update(100.0, 0)
                break

        rc = await proc.wait()
        return rc

    except asyncio.CancelledError:
        try:
            await status_msg.edit("❌ Cancelled ✅")
        except:
            pass
        raise


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


async def convert_to_mp4(input_path: str, out_path: str, status_msg, uid):
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
    await status_msg.edit("🎬 Starting MP4 conversion...")
    return await run_ffmpeg_with_progress(cmd, status_msg, uid, "Converting to MP4", in_path=input_path)


async def compress_video(input_path: str, out_path: str, quality: str, status_msg, uid):
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
    await status_msg.edit(f"🗜 Starting compression {quality}p...")
    return await run_ffmpeg_with_progress(cmd, status_msg, uid, f"Compressing {quality}p", in_path=input_path)


async def video_to_mp3(input_path: str, out_path: str, status_msg, uid):
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vn",
        "-c:a", "libmp3lame",
        "-b:a", "128k",
        out_path
    ]
    await status_msg.edit("🎵 Starting MP3 conversion...")
    return await run_ffmpeg_with_progress(cmd, status_msg, uid, "Video → MP3", in_path=input_path)


async def mp3_to_mp4(input_path: str, out_path: str, status_msg, uid):
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", "color=c=black:s=1280x720:r=30",
        "-i", input_path,
        "-shortest",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "28",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        out_path
    ]
    await status_msg.edit("🎬 Starting MP4 conversion...")
    return await run_ffmpeg_with_progress(cmd, status_msg, uid, "MP3 → MP4", in_path=input_path)


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
# Start
# -------------------------
@app.on_message(filters.private & filters.command("start"))
async def start_cmd(client, message):
    await message.reply(
        "✅ **URL Uploader Bot**\n\n"
        "🌐 Send a direct URL\n"
        "Then choose:\n"
        "📁 File = Document\n"
        "🎥 Video = Convert MP4 + Video upload\n\n"
        "📌 You can also send any media directly.\n"
        "❌ Cancel supported ✅"
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

    # ignore commands
    if text.startswith("/"):
        return

    if is_url(text):
        USER_URL[message.from_user.id] = text
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📁 File", callback_data="send_file"),
                InlineKeyboardButton("🎥 Video", callback_data="send_video")
            ]
        ])
        return await message.reply("✅ URL Received!\n\n👇 Select upload type:", reply_markup=kb)

    return await message.reply("❌ Please send a valid URL or send a media file.")

# -------------------------
# Media received
# -------------------------
@app.on_message(filters.private & filters.media)
async def file_received(client, message):
    uid = message.from_user.id
    USER_CANCEL.discard(uid)

    if message.video:
        media_type = "video"
    elif message.document:
        media_type = "file"
    elif message.audio:
        media_type = "audio"
    else:
        return await message.reply("❌ Unsupported media type.")

    status = await message.reply("⬇️ Starting Telegram download...")
    start_time = time.time()

    try:
        local_path = await message.download(
            file_name=DOWNLOAD_DIR,
            progress=tg_download_progress,
            progress_args=(status, uid, start_time)
        )

        LAST_MEDIA[uid] = {"type": media_type, "path": local_path}

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🗜 Compressor", callback_data="menu_compress"),
                InlineKeyboardButton("👑 Converter", callback_data="menu_convert_royal")
            ]
        ])
        await status.edit("✅ Media received.\n👇 Choose option:", reply_markup=kb)

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

# -------------------------
# Compressor menus
# -------------------------
@app.on_callback_query(filters.regex("^menu_compress$"))
async def menu_compress(client, cb):
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🟢 Compress Higher", callback_data="compress_high"),
            InlineKeyboardButton("🔴 Compress Lower", callback_data="compress_low")
        ]
    ])
    await cb.message.edit("🗜 Choose Compression Type:", reply_markup=kb)

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
    ])
    await cb.message.edit("✨ Select Higher Quality:", reply_markup=kb)

@app.on_callback_query(filters.regex("^compress_low$"))
async def compress_low(client, cb):
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📉 360p", callback_data="q_360"),
            InlineKeyboardButton("📉 240p", callback_data="q_240"),
            InlineKeyboardButton("📉 144p", callback_data="q_144"),
        ]
    ])
    await cb.message.edit("📉 Select Lower Quality:", reply_markup=kb)

@app.on_callback_query(filters.regex(r"^q_\d+$"))
async def quality_selected(client, cb):
    uid = cb.from_user.id
    media = LAST_MEDIA.get(uid)

    if not media or media["type"] != "video":
        return await cb.answer("Send a video first.", show_alert=True)

    q = cb.data.split("_", 1)[1]
    in_path = media["path"]

    status = await cb.message.reply("🗜 Preparing compression...")

    async def job():
        out_path = None
        try:
            old_size = os.path.getsize(in_path)

            out_path = os.path.splitext(in_path)[0] + f"_{q}p.mp4"
            rc = await compress_video(in_path, out_path, q, status, uid)
            if rc != 0 or not os.path.exists(out_path):
                raise Exception("Compression failed!")

            new_size = os.path.getsize(out_path)
            reduced = calc_reduction(old_size, new_size)

            await status.edit("📤 Uploading compressed video...")
            await client.send_video(
                chat_id=cb.message.chat.id,
                video=out_path,
                caption=(
                    f"✅ **Compression Finished** 🗜\n\n"
                    f"📺 Quality: **{q}p**\n"
                    f"📦 Original: **{naturalsize(old_size)}**\n"
                    f"📉 New: **{naturalsize(new_size)}**\n"
                    f"💯 Reduced: **{reduced:.2f}%**\n"
                    f"📌 {os.path.basename(out_path)}"
                ),
                supports_streaming=True
            )
            await status.edit("✅ Done ✅")

        except asyncio.CancelledError:
            pass
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
# Converter Royal Menu
# -------------------------
@app.on_callback_query(filters.regex("^menu_convert_royal$"))
async def menu_convert_royal(client, cb):
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎥➡️🎵 Video → MP3", callback_data="conv_v_mp3"),
            InlineKeyboardButton("🎧➡️🎬 Audio → Video", callback_data="conv_f_vid")
        ],
        [
            InlineKeyboardButton("🎥➡️📁 Video → File", callback_data="conv_v_file"),
            InlineKeyboardButton("🎥➡️🎬 Video → MP4", callback_data="conv_v_mp4")
        ]
    ])
    await cb.message.edit("👑 Converter Menu\n👇 Choose conversion type:", reply_markup=kb)

@app.on_callback_query(filters.regex("^conv_v_mp3$"))
async def conv_v_mp3(client, cb):
    uid = cb.from_user.id
    media = LAST_MEDIA.get(uid)
    if not media or media["type"] != "video":
        return await cb.answer("Send a video first.", show_alert=True)

    in_path = media["path"]
    status = await cb.message.reply("🎵 Preparing...")

    async def job():
        out_path = None
        try:
            out_path = os.path.splitext(in_path)[0] + ".mp3"
            rc = await video_to_mp3(in_path, out_path, status, uid)
            if rc != 0 or not os.path.exists(out_path):
                raise Exception("Video to MP3 failed!")

            await status.edit("📤 Uploading audio...")
            await client.send_audio(
                chat_id=cb.message.chat.id,
                audio=out_path,
                caption=f"✅ Video → MP3\n🎵 {os.path.basename(out_path)}"
            )
            await status.edit("✅ Done ✅")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            await status.edit(f"❌ Failed!\n\nError: `{e}`")
        finally:
            if out_path and os.path.exists(out_path):
                try:
                    os.remove(out_path)
                except:
                    pass

    asyncio.create_task(job()

)

@app.on_callback_query(filters.regex("^conv_f_vid$"))
async def conv_f_vid(client, cb):
    uid = cb.from_user.id
    media = LAST_MEDIA.get(uid)
    if not media:
        return await cb.answer("Send a media file first.", show_alert=True)

    in_path = media["path"]
    if not in_path.lower().endswith(".mp3"):
        return await cb.answer("Send MP3 (audio) to convert into video.", show_alert=True)

    status = await cb.message.reply("🎬 Preparing...")

    async def job():
        out_path = None
        try:
            out_path = os.path.splitext(in_path)[0] + "_audio.mp4"
            rc = await mp3_to_mp4(in_path, out_path, status, uid)
            if rc != 0 or not os.path.exists(out_path):
                raise Exception("Audio to Video failed!")

            await status.edit("📤 Uploading video...")
            await client.send_video(
                chat_id=cb.message.chat.id,
                video=out_path,
                caption=f"✅ Audio → Video\n🎬 {os.path.basename(out_path)}",
                supports_streaming=True
            )
            await status.edit("✅ Done ✅")
        except asyncio.CancelledError:
            pass
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

    status = await cb.message.reply("🎬 Preparing...")

    async def job():
        out_path = None
        try:
            out_path = os.path.splitext(in_path)[0] + "_converted.mp4"
            rc = await convert_to_mp4(in_path, out_path, status, uid)
            if rc != 0 or not os.path.exists(out_path):
                raise Exception("MP4 conversion failed!")

            await status.edit("📤 Uploading MP4...")
            await client.send_video(
                chat_id=cb.message.chat.id,
                video=out_path,
                caption=f"✅ Video → MP4\n🎬 {os.path.basename(out_path)}",
                supports_streaming=True
            )
            await status.edit("✅ Done ✅")

        except asyncio.CancelledError:
            pass
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
# URL -> file/video selection
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
        out_mp4 = None

        try:
            filename, _ = await get_filename_and_size(url)
            if "." not in filename:
                filename += ".bin"

            file_path = os.path.join(DOWNLOAD_DIR, f"{uid}_{int(time.time())}_{filename}")

            await status.edit("⬇️ Starting download...")
            await download_stream(url, file_path, status, uid)

            if uid in USER_CANCEL:
                return

            upload_path = file_path

            if mode == "video" and not file_path.lower().endswith(".mp4"):
                out_mp4 = os.path.splitext(file_path)[0] + "_mp4.mp4"
                rc = await convert_to_mp4(file_path, out_mp4, status, uid)
                if rc != 0 or not os.path.exists(out_mp4):
                    raise Exception("MP4 conversion failed!")
                upload_path = out_mp4

            LAST_MEDIA[uid] = {"type": mode, "path": upload_path}

            await status.edit("📤 Upload starting...")
            up_start = time.time()
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel Upload", callback_data=f"cancel_{uid}")]
            ])
            await status.edit("📤 Uploading...", reply_markup=kb)

            if mode == "video":
                await client.send_video(
                    chat_id=cb.message.chat.id,
                    video=upload_path,
                    caption=f"✅ Uploaded as Video 🎥\n📌 {os.path.basename(upload_path)}",
                    supports_streaming=True,
                    progress=upload_progress,
                    progress_args=(status, uid, up_start)
                )
            else:
                await client.send_document(
                    chat_id=cb.message.chat.id,
                    document=upload_path,
                    caption=f"✅ Uploaded as File 📁\n📌 {os.path.basename(upload_path)}",
                    progress=upload_progress,
                    progress_args=(status, uid, up_start)
                )

            kb2 = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🗜 Compressor", callback_data="menu_compress"),
                    InlineKeyboardButton("👑 Converter", callback_data="menu_convert_royal")
                ]
            ])
            await cb.message.reply("✅ Choose option:", reply_markup=kb2)

            await status.edit("✅ Done ✅")
            await asyncio.sleep(2)
            await status.delete()

        except asyncio.CancelledError:
            pass
        except Exception as e:
            try:
                await status.edit(f"❌ Failed!\n\nError: `{e}`")
            except:
                pass
        finally:
            USER_URL.pop(uid, None)
            USER_TASKS.pop(uid, None)
            USER_CANCEL.discard(uid)

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
