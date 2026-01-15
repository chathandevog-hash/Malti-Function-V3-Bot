import os
import re
import time
import json
import math
import shutil
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
MAX_COMPRESS_SIZE = 2 * 1024 * 1024 * 1024 # ✅ allow attempt up to 2GB (server dependent)
MAX_CONVERT_SIZE = 500 * 1024 * 1024       # ✅ 500MB converter (stable)

AUTO_SPLIT_THRESHOLD = 500 * 1024 * 1024   # ✅ 500MB => auto split ON for compression

# -------------------------
# Storage
# -------------------------
USER_URL = {}
USER_TASKS = {}     # uid -> asyncio task
USER_CANCEL = set() # uid cancel

LAST_MEDIA = {}     # uid -> {"type": "video"|"file"|"audio", "path": "...", "size": int}
UI_STATUS_MSG = {}  # uid -> message (single message UI)

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

def clean_display_name(name: str):
    """
    show clean name like: Leo_Hdmovie4U
    """
    base = os.path.splitext(name)[0]
    base = unquote(base)
    base = re.sub(r"[^a-zA-Z0-9]+", "_", base).strip("_")
    if len(base) > 60:
        base = base[:60].rstrip("_")
    return base or f"file_{int(time.time())}"

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
    if red < 0:
        red = 0
    return red

async def safe_edit(msg, text, reply_markup=None):
    try:
        await msg.edit(text, reply_markup=reply_markup)
    except:
        pass

async def get_or_create_status(message, uid):
    if uid in UI_STATUS_MSG:
        return UI_STATUS_MSG[uid]
    status = await message.reply("⏳ Processing...")
    UI_STATUS_MSG[uid] = status
    return status

def one_task_guard(uid):
    return (uid in USER_TASKS) and (not USER_TASKS[uid].done())

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
        clean_file(thumb_path)

# -------------------------
# Telegram progress
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

    if now - status_msg._last_edit > 2.5:
        status_msg._last_edit = now
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])
        await safe_edit(status_msg, make_progress_text("⬇️ Downloading", current, total, speed, eta), kb)

async def upload_progress(current, total, status_msg, uid, start_time):
    if uid in USER_CANCEL:
        raise asyncio.CancelledError

    elapsed = time.time() - start_time
    speed = current / elapsed if elapsed > 0 else 0
    eta = (total - current) / speed if speed > 0 else 0

    now = time.time()
    if not hasattr(status_msg, "_last_edit"):
        status_msg._last_edit = 0

    if now - status_msg._last_edit > 2.5:
        status_msg._last_edit = now
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])
        await safe_edit(status_msg, make_progress_text("📤 Uploading", current, total, speed, eta), kb)

# -------------------------
# FFmpeg progress (duration based)
# -------------------------
def parse_ffmpeg_time(line: str):
    if "time=" not in line:
        return None
    try:
        t = line.split("time=")[-1].split(" ")[0].strip()
        hh, mm, ss = t.split(":")
        sec = float(ss)
        return int(hh) * 3600 + int(mm) * 60 + sec
    except:
        return None

async def ffmpeg_with_progress(cmd, status_msg, uid, title: str, total_duration: int):
    start = time.time()
    last_edit = 0

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])
    await safe_edit(status_msg, f"⚙️ **{title}**\n\n⏳ Please wait...", kb)

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

        if time.time() - last_edit > 2:
            last_edit = time.time()
            await safe_edit(status_msg, make_progress_text(title, percent, 100, 1, eta), kb)

    await proc.wait()
    return proc.returncode

# -------------------------
# URL helpers + downloader
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
                        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Download", callback_data=f"cancel_{uid}")]])
                        await safe_edit(status_msg, make_progress_text("⬇️ Downloading", downloaded, total, speed, eta), kb)

# -------------------------
# Split + Merge (Compression)
# -------------------------
async def ffmpeg_split_video(input_path: str, parts_dir: str, uid: int, status_msg):
    """
    time-based split to reduce load
    """
    ensure_dir(parts_dir)
    size = os.path.getsize(input_path)

    # dynamic segment time
    # bigger file -> smaller parts
    segment_time = 600  # 10 min
    if size > (1200 * 1024 * 1024):
        segment_time = 420  # 7 min
    if size > (1700 * 1024 * 1024):
        segment_time = 300  # 5 min

    out_pattern = os.path.join(parts_dir, "part_%03d.mp4")

    dur, _, _ = get_video_meta(input_path)
    dur_guess = dur or 600

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-map", "0:v:0?",
        "-map", "0:a:0?",
        "-c", "copy",
        "-f", "segment",
        "-reset_timestamps", "1",
        "-segment_time", str(segment_time),
        out_pattern
    ]

    # show progress (approx based on full duration)
    await ffmpeg_with_progress(cmd, status_msg, uid, "Splitting Video", dur_guess)

    # collect parts
    parts = []
    for fn in sorted(os.listdir(parts_dir)):
        if fn.startswith("part_") and fn.endswith(".mp4"):
            parts.append(os.path.join(parts_dir, fn))
    if not parts:
        raise Exception("Split failed: no parts created")
    return parts

async def ffmpeg_concat_videos(parts: list, out_path: str, uid: int, status_msg):
    """
    concat mp4 parts to final mp4
    """
    if not parts:
        raise Exception("No parts to merge")

    list_file = os.path.join(os.path.dirname(out_path), "concat_list.txt")
    with open(list_file, "w", encoding="utf-8") as f:
        for p in parts:
            f.write(f"file '{p}'\n")

    # merge is usually fast -> fake duration 60
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", out_path]
    await ffmpeg_with_progress(cmd, status_msg, uid, "Merging Parts", 60)

    clean_file(list_file)
    if not os.path.exists(out_path):
        raise Exception("Merge failed: output not found")

# -------------------------
# Compression tools
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

async def compress_part_to_quality(part_path: str, out_path: str, q: str, uid: int, status_msg):
    dur, _, _ = get_video_meta(part_path)
    dur_guess = dur or 300

    w, h = QUALITY_MAP[q]
    cmd = [
        "ffmpeg", "-y",
        "-i", part_path,
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

    rc = await ffmpeg_with_progress(cmd, status_msg, uid, f"Compressing Part ({q}p)", dur_guess)
    if rc != 0 or not os.path.exists(out_path):
        raise Exception("Part compression failed")

# -------------------------
# File compressor (ZIP)
# -------------------------
async def compress_file_zip(input_path: str, out_zip: str):
    ensure_dir(os.path.dirname(out_zip))
    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.write(input_path, arcname=os.path.basename(input_path))
    return os.path.exists(out_zip)

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

def kb_compress_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎥 Video Compress", callback_data="compress_video_menu"),
            InlineKeyboardButton("📁 File Compress", callback_data="compress_file_zip")
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
    ])

def kb_converter_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎥➡️🎵 Video → MP3", callback_data="conv_v_mp3"),
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
# Start
# -------------------------
@app.on_message(filters.private & filters.command("start"))
async def start_cmd(client, message):
    uid = message.from_user.id
    UI_STATUS_MSG.pop(uid, None)

    await message.reply(
        "✨ Welcome to Multifunctional Bot! 🤖💫\n"
        "Here you can do multiple things in one bot 🚀\n\n"
        "🌐 URL Uploader\n"
        "➜ Send any direct link and I will upload it for you instantly ✅\n"
        "⚠️ URL Upload Limit: 2GB\n\n"
        "🗜️ Compressor\n"
        "➜ Reduce file/video size easily without hassle ⚡\n"
        "✅ Auto Split enabled for > 500MB 🔥\n\n"
        "🎛️ Converter\n"
        "➜ Convert files into formats (mp4/mp3 etc.) 🎬🎵\n"
        "⚠️ Conversion Limit: 500MB\n\n"
        "📌 How to use?\n"
        "1️⃣ Send a File / Video / Audio / URL\n"
        "2️⃣ Select your needed option ✅\n"
        "3️⃣ Wait for processing ⏳\n"
        "4️⃣ Get your output 🎉\n\n"
        "🚀 Now send something to start 👇😊",
        reply_markup=kb_main_menu()
    )

# -------------------------
# Back buttons
# -------------------------
@app.on_callback_query(filters.regex("^back_main$"))
async def back_main(client, cb):
    await cb.message.edit("✅ Choose option:", reply_markup=kb_main_menu())

@app.on_callback_query(filters.regex("^back_compress$"))
async def back_compress(client, cb):
    await cb.message.edit("🗜 Choose Compression Type:", reply_markup=kb_compress_menu())

@app.on_callback_query(filters.regex("^back_convert$"))
async def back_convert(client, cb):
    await cb.message.edit("👑 Converter Menu\n👇 Choose conversion type:", reply_markup=kb_converter_menu())

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
    uid = message.from_user.id
    text = message.text.strip()

    if text.startswith("/"):
        return

    if not is_url(text):
        return await message.reply("❌ Send a direct URL (http/https) or send a media file.")

    if one_task_guard(uid):
        return await message.reply("⚠️ One process already running. Please wait or cancel.")

    USER_URL[uid] = text
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📁 File", callback_data="send_file"),
            InlineKeyboardButton("🎥 Video", callback_data="send_video")
        ]
    ])
    await message.reply("✅ URL Received!\n\n👇 Select upload type:", reply_markup=kb)

# -------------------------
# Media received
# -------------------------
@app.on_message(filters.private & filters.media)
async def file_received(client, message):
    uid = message.from_user.id

    if one_task_guard(uid):
        return await message.reply("⚠️ One process already running. Please wait or cancel.")

    USER_CANCEL.discard(uid)

    media_type = None
    size = 0

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
        return await message.reply(
            f"❌ File too large for processing!\n\nMax allowed: 2GB\nYour file: {naturalsize(size)}"
        )

    status = await get_or_create_status(message, uid)
    start_time = time.time()

    async def job():
        try:
            await safe_edit(status, "⬇️ Starting Telegram download...")

            local_path = await message.download(
                file_name=DOWNLOAD_DIR,
                progress=tg_download_progress,
                progress_args=(status, uid, start_time)
            )

            LAST_MEDIA[uid] = {"type": media_type, "path": local_path, "size": size}

            await safe_edit(status, "✅ Media received.\n👇 Choose option:", reply_markup=kb_main_menu())

        except Exception as e:
            await safe_edit(status, f"❌ Download failed!\n\nError: `{e}`")

    t = asyncio.create_task(job())
    USER_TASKS[uid] = t

# -------------------------
# Menu callbacks
# -------------------------
@app.on_callback_query(filters.regex("^menu_compress$"))
async def menu_compress(client, cb):
    await cb.message.edit("🗜 Choose Compression Type:", reply_markup=kb_compress_menu())

@app.on_callback_query(filters.regex("^menu_convert$"))
async def menu_convert(client, cb):
    await cb.message.edit("👑 Converter Menu\n👇 Choose conversion type:", reply_markup=kb_converter_menu())

# -------------------------
# Compress menu
# -------------------------
@app.on_callback_query(filters.regex("^compress_video_menu$"))
async def compress_video_menu(client, cb):
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🟢 Higher Quality", callback_data="compress_high"),
            InlineKeyboardButton("🔴 Lower Quality", callback_data="compress_low")
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="back_compress")]
    ])
    await cb.message.edit("🎥 Select Video Compression:", reply_markup=kb)

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
        [InlineKeyboardButton("🔙 Back", callback_data="compress_video_menu")]
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
        [InlineKeyboardButton("🔙 Back", callback_data="compress_video_menu")]
    ])
    await cb.message.edit("📉 Select Lower Quality:", reply_markup=kb)

# -------------------------
# Video compress action (AUTO SPLIT)
# -------------------------
@app.on_callback_query(filters.regex(r"^q_\d+$"))
async def quality_selected(client, cb):
    uid = cb.from_user.id
    media = LAST_MEDIA.get(uid)

    if not media or media["type"] != "video":
        return await cb.answer("❌ Send a video first.", show_alert=True)

    in_path = media["path"]
    status = await get_or_create_status(cb.message, uid)

    q = cb.data.split("_", 1)[1]

    async def job():
        out_final = None
        parts_dir = None
        try:
            USER_CANCEL.discard(uid)
            old_size = os.path.getsize(in_path)

            # ✅ AUTO SPLIT if > 500MB
            if old_size > AUTO_SPLIT_THRESHOLD:
                parts_dir = os.path.join(DOWNLOAD_DIR, f"parts_{uid}_{int(time.time())}")
                await safe_edit(status, "⚡ Big file detected (>500MB)\n✅ Auto Split Mode ON 🔥")

                parts = await ffmpeg_split_video(in_path, parts_dir, uid, status)

                out_parts = []
                for i, p in enumerate(parts):
                    if uid in USER_CANCEL:
                        raise asyncio.CancelledError

                    out_p = os.path.join(parts_dir, f"out_{i:03d}.mp4")
                    await compress_part_to_quality(p, out_p, q, uid, status)
                    out_parts.append(out_p)

                out_final = os.path.splitext(in_path)[0] + f"_{q}p_SPLIT.mp4"
                await ffmpeg_concat_videos(out_parts, out_final, uid, status)

            else:
                # normal compress
                out_final = os.path.splitext(in_path)[0] + f"_{q}p.mp4"
                dur, _, _ = get_video_meta(in_path)
                dur_guess = dur or 60

                w, h = QUALITY_MAP[q]
                cmd = [
                    "ffmpeg", "-y",
                    "-i", in_path,
                    "-vf", f"scale={w}:{h}",
                    "-c:v", "libx264",
                    "-preset", "veryfast",
                    "-crf", "28",
                    "-c:a", "aac",
                    "-b:a", "96k",
                    "-movflags", "+faststart",
                    "-pix_fmt", "yuv420p",
                    out_final
                ]
                rc = await ffmpeg_with_progress(cmd, status, uid, f"Compressing to {q}p", dur_guess)
                if rc != 0 or not os.path.exists(out_final):
                    raise Exception("Compression failed")

            new_size = os.path.getsize(out_final)
            reduced = calc_reduction(old_size, new_size)

            await send_video_with_meta(
                client,
                cb.message.chat.id,
                out_final,
                caption=(
                    f"✅ Compression Finished 🗜\n\n"
                    f"📺 Quality: {q}p\n"
                    f"📦 Original: {naturalsize(old_size)}\n"
                    f"📉 New: {naturalsize(new_size)}\n"
                    f"💯 Reduced: {reduced:.2f}%"
                )
            )

            await safe_edit(status, "✅ Done ✅", reply_markup=kb_main_menu())

        except asyncio.CancelledError:
            await safe_edit(status, "❌ Cancelled ✅", reply_markup=kb_main_menu())
        except Exception as e:
            await safe_edit(status, f"❌ Failed!\n\nError: `{e}`", reply_markup=kb_main_menu())
        finally:
            # cleanup
            if parts_dir and os.path.exists(parts_dir):
                try:
                    shutil.rmtree(parts_dir, ignore_errors=True)
                except:
                    pass
            clean_file(out_final)
            USER_CANCEL.discard(uid)

    t = asyncio.create_task(job())
    USER_TASKS[uid] = t

# -------------------------
# File compress ZIP
# -------------------------
@app.on_callback_query(filters.regex("^compress_file_zip$"))
async def file_compress_zip_cb(client, cb):
    uid = cb.from_user.id
    media = LAST_MEDIA.get(uid)

    if not media:
        return await cb.answer("❌ Send file first.", show_alert=True)

    in_path = media["path"]
    if not os.path.exists(in_path):
        return await cb.answer("❌ File missing.", show_alert=True)

    size = os.path.getsize(in_path)
    if size > MAX_COMPRESS_SIZE:
        return await cb.answer("❌ Over 2GB not allowed.", show_alert=True)

    out_zip = os.path.splitext(in_path)[0] + "_compressed.zip"
    status = await get_or_create_status(cb.message, uid)

    async def job():
        try:
            USER_CANCEL.discard(uid)

            kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])
            await safe_edit(status, "📦 Compressing file (ZIP)...", kb)

            old_size = os.path.getsize(in_path)
            await compress_file_zip(in_path, out_zip)

            if uid in USER_CANCEL:
                raise asyncio.CancelledError

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

            await safe_edit(status, "✅ Done ✅", reply_markup=kb_main_menu())

        except asyncio.CancelledError:
            await safe_edit(status, "❌ Cancelled ✅", reply_markup=kb_main_menu())
        except Exception as e:
            await safe_edit(status, f"❌ Failed!\n\nError: `{e}`", reply_markup=kb_main_menu())
        finally:
            clean_file(out_zip)
            USER_CANCEL.discard(uid)

    t = asyncio.create_task(job())
    USER_TASKS[uid] = t

# -------------------------
# Converter (NO split mode)
# -------------------------
@app.on_callback_query(filters.regex("^conv_v_mp3$"))
async def conv_v_mp3(client, cb):
    uid = cb.from_user.id
    media = LAST_MEDIA.get(uid)

    if not media or media["type"] != "video":
        return await cb.answer("❌ Send video first.", show_alert=True)

    in_path = media["path"]
    if os.path.getsize(in_path) > MAX_CONVERT_SIZE:
        return await cb.answer("❌ Converter limit 500MB.", show_alert=True)

    out_path = os.path.splitext(in_path)[0] + ".mp3"
    status = await get_or_create_status(cb.message, uid)

    async def job():
        try:
            USER_CANCEL.discard(uid)
            dur, _, _ = get_video_meta(in_path)
            dur_guess = dur or 60

            cmd = ["ffmpeg", "-y", "-i", in_path, "-vn", "-c:a", "libmp3lame", "-b:a", "128k", out_path]
            rc = await ffmpeg_with_progress(cmd, status, uid, "Converting to MP3", dur_guess)
            if rc != 0 or not os.path.exists(out_path):
                raise Exception("MP3 conversion failed")

            await client.send_audio(cb.message.chat.id, out_path, caption="✅ Video → MP3 🎵")
            await safe_edit(status, "✅ Done ✅", reply_markup=kb_main_menu())

        except asyncio.CancelledError:
            await safe_edit(status, "❌ Cancelled ✅", reply_markup=kb_main_menu())
        except Exception as e:
            await safe_edit(status, f"❌ Failed!\n\nError: `{e}`", reply_markup=kb_main_menu())
        finally:
            clean_file(out_path)
            USER_CANCEL.discard(uid)

    t = asyncio.create_task(job())
    USER_TASKS[uid] = t

@app.on_callback_query(filters.regex("^conv_v_file$"))
async def conv_v_file(client, cb):
    uid = cb.from_user.id
    media = LAST_MEDIA.get(uid)

    if not media or media["type"] != "video":
        return await cb.answer("❌ Send video first.", show_alert=True)

    in_path = media["path"]
    if os.path.getsize(in_path) > MAX_CONVERT_SIZE:
        return await cb.answer("❌ Converter limit 500MB.", show_alert=True)

    await client.send_document(cb.message.chat.id, in_path, caption="✅ Video → File 📁")
    await cb.answer("✅ Done")

@app.on_callback_query(filters.regex("^conv_v_mp4$"))
async def conv_v_mp4(client, cb):
    uid = cb.from_user.id
    media = LAST_MEDIA.get(uid)

    if not media or media["type"] != "video":
        return await cb.answer("❌ Send video first.", show_alert=True)

    in_path = media["path"]
    if os.path.getsize(in_path) > MAX_CONVERT_SIZE:
        return await cb.answer("❌ Converter limit 500MB.", show_alert=True)

    if in_path.lower().endswith(".mp4"):
        return await cb.answer("Already MP4 ✅", show_alert=True)

    out_path = os.path.splitext(in_path)[0] + "_converted.mp4"
    status = await get_or_create_status(cb.message, uid)

    async def job():
        try:
            USER_CANCEL.discard(uid)
            dur, _, _ = get_video_meta(in_path)
            dur_guess = dur or 60

            cmd = [
                "ffmpeg", "-y",
                "-i", in_path,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                "-pix_fmt", "yuv420p",
                out_path
            ]
            rc = await ffmpeg_with_progress(cmd, status, uid, "Converting to MP4", dur_guess)
            if rc != 0 or not os.path.exists(out_path):
                raise Exception("MP4 conversion failed")

            await send_video_with_meta(client, cb.message.chat.id, out_path, caption="✅ Video → MP4 🎬")
            await safe_edit(status, "✅ Done ✅", reply_markup=kb_main_menu())

        except asyncio.CancelledError:
            await safe_edit(status, "❌ Cancelled ✅", reply_markup=kb_main_menu())
        except Exception as e:
            await safe_edit(status, f"❌ Failed!\n\nError: `{e}`", reply_markup=kb_main_menu())
        finally:
            clean_file(out_path)
            USER_CANCEL.discard(uid)

    t = asyncio.create_task(job())
    USER_TASKS[uid] = t

@app.on_callback_query(filters.regex("^conv_f_mp4$"))
async def conv_f_mp4(client, cb):
    uid = cb.from_user.id
    media = LAST_MEDIA.get(uid)

    if not media:
        return await cb.answer("❌ Send file first.", show_alert=True)

    in_path = media["path"]
    if os.path.getsize(in_path) > MAX_CONVERT_SIZE:
        return await cb.answer("❌ Converter limit 500MB.", show_alert=True)

    out_path = os.path.splitext(in_path)[0] + "_file.mp4"
    status = await get_or_create_status(cb.message, uid)

    async def job():
        try:
            USER_CANCEL.discard(uid)
            dur, _, _ = get_video_meta(in_path)
            dur_guess = dur or 60

            cmd = [
                "ffmpeg", "-y",
                "-i", in_path,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                "-pix_fmt", "yuv420p",
                out_path
            ]
            rc = await ffmpeg_with_progress(cmd, status, uid, "Converting File → MP4", dur_guess)
            if rc != 0 or not os.path.exists(out_path):
                raise Exception("MP4 conversion failed")

            await send_video_with_meta(client, cb.message.chat.id, out_path, caption="✅ File → MP4 🎬")
            await safe_edit(status, "✅ Done ✅", reply_markup=kb_main_menu())

        except asyncio.CancelledError:
            await safe_edit(status, "❌ Cancelled ✅", reply_markup=kb_main_menu())
        except Exception as e:
            await safe_edit(status, f"❌ Failed!\n\nError: `{e}`", reply_markup=kb_main_menu())
        finally:
            clean_file(out_path)
            USER_CANCEL.discard(uid)

    t = asyncio.create_task(job())
    USER_TASKS[uid] = t

# -------------------------
# URL Upload Selection
# -------------------------
@app.on_callback_query(filters.regex("^(send_file|send_video)$"))
async def send_type_selected(client, cb):
    uid = cb.from_user.id

    if uid not in USER_URL:
        return await cb.message.edit("❌ Session expired. Send URL again.")

    if one_task_guard(uid):
        return await cb.answer("⚠️ One process running already!", show_alert=True)

    url = USER_URL[uid]
    mode = cb.data.replace("send_", "")

    await cb.answer()
    await cb.message.edit(f"✅ Selected: **{mode.upper()}**")

    status = await get_or_create_status(cb.message, uid)

    async def job():
        file_path = None
        mp4_out = None
        try:
            USER_CANCEL.discard(uid)

            filename, total = await get_filename_and_size(url)
            if total and total > MAX_URL_SIZE:
                return await safe_edit(status, "❌ URL file too large! Max: 2GB", reply_markup=kb_main_menu())

            if "." not in filename:
                filename += ".bin"

            file_path = os.path.join(DOWNLOAD_DIR, f"{uid}_{int(time.time())}_{filename}")

            await safe_edit(status, "⬇️ Starting download...")
            await download_stream(url, file_path, status, uid)

            upload_path = file_path

            # url -> video must be mp4
            if mode == "video":
                if not file_path.lower().endswith(".mp4"):
                    mp4_out = os.path.splitext(file_path)[0] + "_mp4.mp4"
                    dur, _, _ = get_video_meta(file_path)
                    dur_guess = dur or 60

                    cmd = [
                        "ffmpeg", "-y",
                        "-i", file_path,
                        "-map", "0:v:0?",
                        "-map", "0:a:0?",
                        "-c:v", "libx264",
                        "-preset", "veryfast",
                        "-crf", "23",
                        "-c:a", "aac",
                        "-b:a", "128k",
                        "-movflags", "+faststart",
                        "-pix_fmt", "yuv420p",
                        mp4_out
                    ]
                    await ffmpeg_with_progress(cmd, status, uid, "Converting to MP4", dur_guess)
                    if not os.path.exists(mp4_out):
                        raise Exception("MP4 conversion failed!")
                    upload_path = mp4_out

                # ✅ clean name show
                clean_name = clean_display_name(os.path.basename(upload_path))
                size_bytes = os.path.getsize(upload_path)

                await safe_edit(status, "📤 Uploading video...")
                await send_video_with_meta(
                    client,
                    cb.message.chat.id,
                    upload_path,
                    caption=(
                        "✅ URL Uploaded 🎥\n\n"
                        f"📌 Name: `{clean_name}`\n"
                        f"📦 Size: **{naturalsize(size_bytes)}**"
                    )
                )

            else:
                clean_name = clean_display_name(os.path.basename(upload_path))
                size_bytes = os.path.getsize(upload_path)

                up_start = time.time()
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Upload", callback_data=f"cancel_{uid}")]])
                await safe_edit(status, "📤 Uploading...", kb)

                await client.send_document(
                    chat_id=cb.message.chat.id,
                    document=upload_path,
                    caption=(
                        "✅ URL Uploaded 📁\n\n"
                        f"📌 Name: `{clean_name}`\n"
                        f"📦 Size: **{naturalsize(size_bytes)}**"
                    ),
                    progress=upload_progress,
                    progress_args=(status, uid, up_start)
                )

            await safe_edit(status, "✅ Done ✅", reply_markup=kb_main_menu())

        except asyncio.CancelledError:
            await safe_edit(status, "❌ Cancelled ✅", reply_markup=kb_main_menu())
        except Exception as e:
            await safe_edit(status, f"❌ Failed!\n\nError: `{e}`", reply_markup=kb_main_menu())
        finally:
            USER_URL.pop(uid, None)
            USER_TASKS.pop(uid, None)
            USER_CANCEL.discard(uid)

            for p in [file_path, mp4_out]:
                clean_file(p)

    t = asyncio.create_task(job())
    USER_TASKS[uid] = t

# -------------------------
# Main
# -------------------------
if __name__ == "__main__":
    if not BOT_TOKEN or not API_ID or not API_HASH:
        print("❌ Please set BOT_TOKEN, API_ID, API_HASH in env!")
        raise SystemExit

    ensure_dir(DOWNLOAD_DIR)
    print("✅ Bot started...")
    app.run()
