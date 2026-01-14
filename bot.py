import os
import re
import time
import asyncio
import aiohttp
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

    done_mib = done / (1024 * 1024)
    total_mib = (total / (1024 * 1024)) if total else 0

    if total:
        size_str = f"{done_mib:.1f} MiB of {total_mib:.2f} MiB"
    else:
        size_str = f"{done_mib:.1f} MiB of Unknown"

    speed_mib = (speed / (1024 * 1024)) if speed else 0
    speed_str = f"{speed_mib:.2f} MiB/s"

    eta_str = format_time(eta)

    return (
        f"🚀 {title} ⚡\n\n"
        f"{bar}\n\n"
        f"⌛ Done: {done_str}\n"
        f"🖇️ Size: {size_str}\n"
        f"🚀 Speed: {speed_str}\n"
        f"⏱ ETA: {eta_str}"
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
                            [InlineKeyboardButton("❌ Cancel Download ❌", callback_data=f"cancel_{uid}")]
                        ])
                        await status_msg.edit(
                            make_progress_text("Downloading", downloaded, total, speed, eta),
                            reply_markup=kb
                        )

    return downloaded, total


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
            [InlineKeyboardButton("❌ Cancel Upload ❌", callback_data=f"cancel_{uid}")]
        ])
        await status_msg.edit(
            make_progress_text("Uploading", current, total, speed, eta),
            reply_markup=kb
        )


# -------------------------
# Compression
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
    rc = await run_ffmpeg(cmd)
    return rc


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
    rc = await run_ffmpeg(cmd)
    return rc


async def video_to_mp3(input_path: str, out_path: str):
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vn",
        "-c:a", "libmp3lame",
        "-b:a", "128k",
        out_path
    ]
    rc = await run_ffmpeg(cmd)
    return rc


async def mp3_to_mp4(input_path: str, out_path: str):
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
    rc = await run_ffmpeg(cmd)
    return rc


# -------------------------
# Bot Init
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
        "✅ URL Uploader Bot\n\n"
        "➡️ Send a direct URL\n"
        "Then choose:\n"
        "📁 File = Document\n"
        "🎥 Video = Convert MP4 + Video upload\n\n"
        "You can also send any file/video directly ✅\n"
        "Cancel supported ✅"
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
# URL Handler
# -------------------------
@app.on_message(filters.private & filters.text)
async def url_handler(client, message):
    text = message.text.strip()

    # ✅ ignore commands
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
        return await message.reply("✅ URL Received!\n\nSelect upload type:", reply_markup=kb)

    return await message.reply("❌ Send a direct URL or send a file/video.")

# -------------------------
# ✅ FIX: ANY media catch (mkv/mp4/document/audio everything)
# -------------------------
@app.on_message(filters.private & filters.media)
async def file_received(client, message):
    uid = message.from_user.id

    if message.video:
        media_type = "video"
    elif message.document:
        media_type = "file"
    elif message.audio:
        media_type = "audio"
    else:
        return await message.reply("❌ Unsupported media type.")

    status = await message.reply("⬇️ Downloading from Telegram...")
    local_path = await message.download(file_name=DOWNLOAD_DIR)

    LAST_MEDIA[uid] = {"type": media_type, "path": local_path}

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🗜 Compressor", callback_data="menu_compress"),
            InlineKeyboardButton("🔄 Converter", callback_data="menu_convert")
        ]
    ])
    await status.edit("✅ Media received.\nChoose option:", reply_markup=kb)

# -------------------------
# Menus
# -------------------------
@app.on_callback_query(filters.regex("^menu_compress$"))
async def menu_compress(client, cb):
    uid = cb.from_user.id
    if uid not in LAST_MEDIA:
        return await cb.answer("No media found.", show_alert=True)

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
            InlineKeyboardButton("2160p", callback_data="q_2160"),
            InlineKeyboardButton("1440p", callback_data="q_1440"),
            InlineKeyboardButton("1080p", callback_data="q_1080"),
        ],
        [
            InlineKeyboardButton("720p", callback_data="q_720"),
            InlineKeyboardButton("480p", callback_data="q_480"),
        ],
    ])
    await cb.message.edit("🗜 Select Higher Quality:", reply_markup=kb)

@app.on_callback_query(filters.regex("^compress_low$"))
async def compress_low(client, cb):
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("360p", callback_data="q_360"),
            InlineKeyboardButton("240p", callback_data="q_240"),
            InlineKeyboardButton("144p", callback_data="q_144"),
        ]
    ])
    await cb.message.edit("🗜 Select Lower Quality:", reply_markup=kb)

@app.on_callback_query(filters.regex("^menu_convert$"))
async def menu_convert(client, cb):
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎥 Video ➜ MP3", callback_data="v_to_mp3"),
            InlineKeyboardButton("🎵 MP3 ➜ MP4", callback_data="mp3_to_mp4")
        ]
    ])
    await cb.message.edit("🔄 Converter Options:", reply_markup=kb)

# -------------------------
# URL selection
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
        mp4_out = None

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

            if mode == "video":
                if not file_path.lower().endswith(".mp4"):
                    mp4_out = os.path.splitext(file_path)[0] + "_mp4.mp4"
                    await status.edit("🎬 Converting to MP4...")
                    rc = await convert_to_mp4(file_path, mp4_out)
                    if rc != 0 or not os.path.exists(mp4_out):
                        raise Exception("MP4 conversion failed!")
                    upload_path = mp4_out

            LAST_MEDIA[uid] = {"type": mode, "path": upload_path}

            await status.edit("📤 Upload starting...")
            up_start = time.time()

            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel Upload ❌", callback_data=f"cancel_{uid}")]
            ])
            await status.edit("📤 Uploading...", reply_markup=kb)

            if mode == "video":
                await client.send_video(
                    chat_id=cb.message.chat.id,
                    video=upload_path,
                    caption=f"✅ Uploaded as MP4 Video\n\n📌 {os.path.basename(upload_path)}",
                    supports_streaming=True,
                    progress=upload_progress,
                    progress_args=(status, uid, up_start)
                )
            else:
                await client.send_document(
                    chat_id=cb.message.chat.id,
                    document=upload_path,
                    caption=f"✅ Uploaded as File\n\n📌 {os.path.basename(upload_path)}",
                    progress=upload_progress,
                    progress_args=(status, uid, up_start)
                )

            kb2 = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🗜 Compressor", callback_data="menu_compress"),
                    InlineKeyboardButton("🔄 Converter", callback_data="menu_convert")
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
# Compression quality select
# -------------------------
@app.on_callback_query(filters.regex(r"^q_\d+$"))
async def quality_selected(client, cb):
    uid = cb.from_user.id
    if uid not in LAST_MEDIA:
        return await cb.answer("No media found.", show_alert=True)

    media = LAST_MEDIA[uid]
    if media["type"] != "video":
        return await cb.answer("Compression only for video.", show_alert=True)

    q = cb.data.split("_", 1)[1]
    in_path = media["path"]

    status = await cb.message.reply(f"🗜 Compressing to {q}p...\nPlease wait...")

    async def job():
        out_path = None
        try:
            base = os.path.splitext(in_path)[0]
            out_path = f"{base}_{q}p.mp4"

            rc = await compress_video(in_path, out_path, q)
            if rc != 0 or not os.path.exists(out_path):
                raise Exception("Compression failed!")

            await client.send_video(
                chat_id=cb.message.chat.id,
                video=out_path,
                caption=f"✅ Compressed to {q}p\n\n📌 {os.path.basename(out_path)}",
                supports_streaming=True
            )

            await status.edit("✅ Compression done ✅")
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
# Converter
# -------------------------
@app.on_callback_query(filters.regex("^v_to_mp3$"))
async def cb_v_to_mp3(client, cb):
    uid = cb.from_user.id
    if uid not in LAST_MEDIA:
        return await cb.answer("No media found.", show_alert=True)

    media = LAST_MEDIA[uid]
    if media["type"] != "video":
        return await cb.answer("Send a video first.", show_alert=True)

    in_path = media["path"]
    status = await cb.message.reply("🎵 Converting Video ➜ MP3...")

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
                caption=f"✅ Video ➜ MP3\n\n📌 {os.path.basename(out_path)}"
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


@app.on_callback_query(filters.regex("^mp3_to_mp4$"))
async def cb_mp3_to_mp4(client, cb):
    uid = cb.from_user.id
    if uid not in LAST_MEDIA:
        return await cb.answer("No media found.", show_alert=True)

    media = LAST_MEDIA[uid]
    in_path = media["path"]

    if not in_path.lower().endswith(".mp3"):
        return await cb.answer("Send MP3 file first.", show_alert=True)

    status = await cb.message.reply("🎬 Converting MP3 ➜ MP4...")

    async def job():
        out_path = None
        try:
            out_path = os.path.splitext(in_path)[0] + "_audio.mp4"
            rc = await mp3_to_mp4(in_path, out_path)
            if rc != 0 or not os.path.exists(out_path):
                raise Exception("MP3 to MP4 failed!")

            await client.send_video(
                chat_id=cb.message.chat.id,
                video=out_path,
                caption=f"✅ MP3 ➜ MP4\n\n📌 {os.path.basename(out_path)}",
                supports_streaming=True
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
# Main
# -------------------------
if __name__ == "__main__":
    if not BOT_TOKEN or not API_ID or not API_HASH:
        print("❌ Please set BOT_TOKEN, API_ID, API_HASH in env!")
        raise SystemExit

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    print("✅ Bot started...")
    app.run()
