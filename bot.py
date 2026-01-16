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
# FreeConvert API (Optional)
# -------------------------
# Render Env: FREECONVERT_ACCESS_TOKEN
FREECONVERT_ACCESS_TOKEN = os.getenv("FREECONVERT_ACCESS_TOKEN", "").strip()
FREECONVERT_BASE = "https://api.freeconvert.com/v1"

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
LAST_MEDIA = {}          # uid -> {"type": "...", "path": "...", "size": int}
UI_STATUS_MSG = {}       # uid -> status message object (single message UI)


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


def calc_reduction(old_bytes: int, new_bytes: int):
    if not old_bytes or not new_bytes:
        return 0.0
    red = (1 - (new_bytes / old_bytes)) * 100
    if red < 0:
        red = 0
    return red


async def get_or_create_status(message, uid):
    if uid in UI_STATUS_MSG:
        return UI_STATUS_MSG[uid]
    status = await message.reply("⏳ Processing...")
    UI_STATUS_MSG[uid] = status
    return status


async def safe_edit(msg, text, reply_markup=None):
    try:
        await msg.edit(text, reply_markup=reply_markup)
    except:
        pass


def busy(uid: int) -> bool:
    return uid in USER_TASKS and not USER_TASKS[uid].done()


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
# Progress callbacks (Telegram)
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
# FFmpeg processing progress
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
# FreeConvert API Compressor (Upload -> Compress -> Export URL)
# -------------------------
async def freeconvert_request(session: aiohttp.ClientSession, method: str, url: str, token: str, json_data=None):
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    if json_data is not None:
        headers["Content-Type"] = "application/json"

    async with session.request(method, url, headers=headers, json=json_data) as r:
        try:
            data = await r.json()
        except:
            text = await r.text()
            raise Exception(f"FreeConvert API invalid response: {text[:200]}")

        if r.status >= 400:
            raise Exception(f"FreeConvert API Error ({r.status}): {data}")
        return data


async def freeconvert_create_job(session: aiohttp.ClientSession, token: str, input_format: str, output_format: str):
    payload = {
        "tasks": {
            "import-1": {
                "operation": "import/upload"
            },
            "compress-1": {
                "operation": "compress",
                "input": "import-1",
                "input_format": input_format,
                "output_format": output_format
            },
            "export-1": {
                "operation": "export/url",
                "input": ["compress-1"]
            }
        }
    }
    return await freeconvert_request(session, "POST", f"{FREECONVERT_BASE}/process/jobs", token, payload)


def freeconvert_find_task(job_json: dict, task_name: str):
    tasks = job_json.get("tasks") or job_json.get("data", {}).get("tasks")
    if isinstance(tasks, dict):
        return tasks.get(task_name)
    if isinstance(tasks, list):
        for t in tasks:
            if t.get("name") == task_name:
                return t
    return None


async def freeconvert_get_job(session: aiohttp.ClientSession, token: str, job_id: str):
    return await freeconvert_request(session, "GET", f"{FREECONVERT_BASE}/process/jobs/{job_id}", token)


async def freeconvert_wait_finished(session: aiohttp.ClientSession, token: str, job_id: str, status_msg=None, uid=None):
    start = time.time()
    while True:
        if uid and uid in USER_CANCEL:
            raise asyncio.CancelledError

        job = await freeconvert_get_job(session, token, job_id)
        status = job.get("status") or job.get("data", {}).get("status")
        status = (status or "").lower()

        if status_msg:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])
            await safe_edit(status_msg, f"☁️ FreeConvert Processing...\n\n⏳ {int(time.time()-start)}s", kb)

        if status in ["finished", "completed", "success"]:
            return job
        if status in ["failed", "error"]:
            raise Exception("FreeConvert job failed")

        await asyncio.sleep(3)


async def freeconvert_upload_file(session: aiohttp.ClientSession, import_task: dict, file_path: str, status_msg=None, uid=None):
    """
    FreeConvert import/upload task usually returns upload URL + fields.
    We'll support both possible structures.
    """
    if uid and uid in USER_CANCEL:
        raise asyncio.CancelledError

    # Try common structures
    result = import_task.get("result") or import_task.get("data", {}).get("result") or {}

    upload_url = result.get("upload_url") or result.get("url")
    fields = result.get("fields") or result.get("parameters") or {}

    if not upload_url:
        # Some APIs return under `form`
        form = result.get("form") or {}
        upload_url = form.get("url")
        fields = form.get("fields") or form.get("parameters") or fields

    if not upload_url:
        raise Exception(f"FreeConvert import task missing upload url: {import_task}")

    data = aiohttp.FormData()
    for k, v in fields.items():
        data.add_field(k, str(v))

    # file must be last
    with open(file_path, "rb") as f:
        data.add_field("file", f, filename=os.path.basename(file_path), content_type="application/octet-stream")
        async with session.post(upload_url, data=data) as r:
            if r.status >= 400:
                txt = await r.text()
                raise Exception(f"FreeConvert upload failed: HTTP {r.status} {txt[:200]}")

    if status_msg:
        await safe_edit(status_msg, "✅ Uploaded to FreeConvert ☁️")


def freeconvert_export_url(job_json: dict):
    tasks = job_json.get("tasks") or job_json.get("data", {}).get("tasks")
    if isinstance(tasks, dict):
        export = tasks.get("export-1")
        if not export:
            return None
        res = export.get("result") or {}
        files = res.get("files") or []
        if files:
            return files[0].get("url")
        return res.get("url")

    if isinstance(tasks, list):
        for t in tasks:
            if t.get("operation") == "export/url":
                res = t.get("result") or {}
                files = res.get("files") or []
                if files:
                    return files[0].get("url")
    return None


async def freeconvert_compress_and_send(client, chat_id: int, input_path: str, status_msg, uid: int,
                                       input_format: str = "mp4", output_format: str = "mp4"):
    if not FREECONVERT_ACCESS_TOKEN:
        raise Exception("FREECONVERT_ACCESS_TOKEN not set in Render env")

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None)) as session:
        await safe_edit(status_msg, "☁️ FreeConvert: Creating job...")
        job = await freeconvert_create_job(session, FREECONVERT_ACCESS_TOKEN, input_format, output_format)

        job_id = job.get("id") or job.get("data", {}).get("id")
        if not job_id:
            raise Exception(f"FreeConvert job_id missing: {job}")

        import_task = freeconvert_find_task(job, "import-1")
        if not import_task:
            job_full = await freeconvert_get_job(session, FREECONVERT_ACCESS_TOKEN, job_id)
            import_task = freeconvert_find_task(job_full, "import-1")
        if not import_task:
            raise Exception("FreeConvert import task missing")

        await safe_edit(status_msg, "☁️ FreeConvert: Uploading file...")
        await freeconvert_upload_file(session, import_task, input_path, status_msg, uid)

        done = await freeconvert_wait_finished(session, FREECONVERT_ACCESS_TOKEN, job_id, status_msg, uid)

        url = freeconvert_export_url(done)
        if not url:
            raise Exception("FreeConvert export URL missing")

        out_path = os.path.join(DOWNLOAD_DIR, f"freeconvert_{uid}_{int(time.time())}.{output_format}")
        await safe_edit(status_msg, "⬇️ Downloading compressed output...")
        await download_stream(url, out_path, status_msg, uid)

        await safe_edit(status_msg, "📤 Uploading to Telegram...")
        up_start = time.time()
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])
        await safe_edit(status_msg, "📤 Uploading...", reply_markup=kb)

        if output_format.lower() in ["mp4", "mkv", "mov", "webm"]:
            await client.send_video(
                chat_id=chat_id,
                video=out_path,
                caption="✅ Compressed via FreeConvert ☁️",
                supports_streaming=True,
                progress=upload_progress,
                progress_args=(status_msg, uid, up_start)
            )
        else:
            await client.send_document(
                chat_id=chat_id,
                document=out_path,
                caption="✅ Compressed via FreeConvert ☁️",
                progress=upload_progress,
                progress_args=(status_msg, uid, up_start)
            )

        clean_file(out_path)


# -------------------------
# URL download (aiohttp)
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
                raise Exception("❌ URL file too large (max 2GB)")

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
                        await safe_edit(
                            status_msg,
                            make_progress_text("⬇️ Downloading", downloaded, total, speed, eta),
                            reply_markup=kb
                        )


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


# -------------------------
# File compressor (ZIP fallback)
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
        [InlineKeyboardButton("🌐 URL Uploader", callback_data="menu_url")],
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

    text = (
        "✨ Welcome to Multifunctional Bot! 🤖💫\n"
        "Here you can do multiple things in one bot 🚀\n\n"
        "🌐 URL Uploader\n"
        "➜ Send any direct link and I will upload it for you instantly ✅\n"
        "⚡ URL Limit: 2GB\n\n"
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
        "🚀 Now send something to start 👇😊"
    )

    await message.reply(text, reply_markup=kb_main_menu())


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
# URL message
# -------------------------
@app.on_message(filters.private & filters.text)
async def url_handler(client, message):
    text = message.text.strip()
    uid = message.from_user.id

    if text.startswith("/"):
        return

    # ✅ URL Flow
    if is_url(text):
        if busy(uid):
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
# Media received (Telegram)
# -------------------------
@app.on_message(filters.private & filters.media)
async def file_received(client, message):
    uid = message.from_user.id

    if busy(uid):
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

    if size > MAX_URL_SIZE:
        return await message.reply(f"❌ Too large!\nMax: 2GB\nYour file: {naturalsize(size)}")

    status = await get_or_create_status(message, uid)
    start_time = time.time()

    async def job():
        local_path = None
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
@app.on_callback_query(filters.regex("^menu_url$"))
async def menu_url(client, cb):
    await cb.answer("✅")
    await cb.message.reply("🌐 Send direct URL now ✅")


@app.on_callback_query(filters.regex("^menu_compress$"))
async def menu_compress(client, cb):
    await cb.message.edit("🗜 Choose Compression Type:", reply_markup=kb_compress_menu())


@app.on_callback_query(filters.regex("^menu_convert$"))
async def menu_convert(client, cb):
    await cb.message.edit("👑 Converter Menu\n👇 Choose conversion type:", reply_markup=kb_converter_menu())


# -------------------------
# Video compress menu
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
# Video compressor action
# -------------------------
@app.on_callback_query(filters.regex(r"^q_\d+$"))
async def quality_selected(client, cb):
    uid = cb.from_user.id
    media = LAST_MEDIA.get(uid)

    if not media or media["type"] != "video":
        return await cb.answer("❌ Send a video first.", show_alert=True)

    if media["size"] > MAX_COMPRESS_SIZE:
        return await cb.answer("❌ Over limit 700MB!", show_alert=True)

    q = cb.data.split("_", 1)[1]
    in_path = media["path"]

    status = await get_or_create_status(cb.message, uid)

    async def job():
        out_path = None
        try:
            USER_CANCEL.discard(uid)

            old_size = os.path.getsize(in_path)
            out_path = os.path.splitext(in_path)[0] + f"_{q}p.mp4"

            dur, _, _ = get_video_meta(in_path)
            dur = dur or 60

            # ✅ Try FreeConvert API first (low memory). If fails, fallback to local ffmpeg.
            try:
                if FREECONVERT_ACCESS_TOKEN:
                    await freeconvert_compress_and_send(
                        client,
                        cb.message.chat.id,
                        in_path,
                        status,
                        uid,
                        input_format="mp4",
                        output_format="mp4"
                    )
                else:
                    raise Exception("FreeConvert token not set")
            except Exception as api_err:
                await safe_edit(status, f"⚠️ FreeConvert Failed, using local FFmpeg...\n\nReason: `{api_err}`")

                cmd = [
                    "ffmpeg", "-y",
                    "-i", in_path,
                    "-vf", f"scale={QUALITY_MAP[q][0]}:{QUALITY_MAP[q][1]}",
                    "-c:v", "libx264",
                    "-preset", "veryfast",
                    "-crf", "28",
                    "-c:a", "aac",
                    "-b:a", "96k",
                    "-movflags", "+faststart",
                    "-pix_fmt", "yuv420p",
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
# File Compress (API -> ZIP fallback)
# -------------------------
@app.on_callback_query(filters.regex("^compress_file_zip$"))
async def file_compress_zip(client, cb):
    uid = cb.from_user.id
    media = LAST_MEDIA.get(uid)
    if not media:
        return await cb.answer("❌ Send file first.", show_alert=True)

    if media["size"] > MAX_COMPRESS_SIZE:
        return await cb.answer("❌ Over limit 700MB!", show_alert=True)

    in_path = media["path"]
    out_zip = os.path.splitext(in_path)[0] + "_compressed.zip"
    status = await get_or_create_status(cb.message, uid)

    async def job():
        try:
            USER_CANCEL.discard(uid)
            old_size = os.path.getsize(in_path)

            kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])
            await safe_edit(status, "📦 Compressing file...", reply_markup=kb)

            # ✅ Try FreeConvert API first (low memory). If fails, fallback to ZIP.
            try:
                if not FREECONVERT_ACCESS_TOKEN:
                    raise Exception("FreeConvert token not set")

                ext = os.path.splitext(in_path)[1].lower().replace(".", "")
                if not ext:
                    ext = "bin"

                await freeconvert_compress_and_send(
                    client,
                    cb.message.chat.id,
                    in_path,
                    status,
                    uid,
                    input_format=ext,
                    output_format=ext
                )

                await safe_edit(status, "✅ Done ✅", reply_markup=kb_main_menu())
                return

            except Exception as api_err:
                await safe_edit(status, f"⚠️ FreeConvert Failed, using ZIP...\n\nReason: `{api_err}`", reply_markup=kb)

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
# Converter actions
# -------------------------
@app.on_callback_query(filters.regex("^conv_v_mp3$"))
async def conv_v_mp3(client, cb):
    uid = cb.from_user.id
    media = LAST_MEDIA.get(uid)

    if not media or media["type"] != "video":
        return await cb.answer("❌ Send video first.", show_alert=True)

    if media["size"] > MAX_CONVERT_SIZE:
        return await cb.answer("❌ Convert limit 500MB.", show_alert=True)

    in_path = media["path"]
    out_path = os.path.splitext(in_path)[0] + ".mp3"
    status = await get_or_create_status(cb.message, uid)

    async def job():
        try:
            USER_CANCEL.discard(uid)
            dur, _, _ = get_video_meta(in_path)
            dur = dur or 60

            cmd = ["ffmpeg", "-y", "-i", in_path, "-vn", "-c:a", "libmp3lame", "-b:a", "128k", out_path]
            rc = await ffmpeg_with_progress(cmd, status, uid, "Converting to MP3", dur)
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

    if media["size"] > MAX_CONVERT_SIZE:
        return await cb.answer("❌ Convert limit 500MB.", show_alert=True)

    await client.send_document(cb.message.chat.id, media["path"], caption="✅ Video → File 📁")
    await cb.answer("✅ Done")


@app.on_callback_query(filters.regex("^conv_v_mp4$"))
async def conv_v_mp4(client, cb):
    uid = cb.from_user.id
    media = LAST_MEDIA.get(uid)

    if not media or media["type"] != "video":
        return await cb.answer("❌ Send video first.", show_alert=True)

    if media["size"] > MAX_CONVERT_SIZE:
        return await cb.answer("❌ Convert limit 500MB.", show_alert=True)

    in_path = media["path"]
    if in_path.lower().endswith(".mp4"):
        return await cb.answer("Already MP4 ✅", show_alert=True)

    out_path = os.path.splitext(in_path)[0] + "_converted.mp4"
    status = await get_or_create_status(cb.message, uid)

    async def job():
        try:
            USER_CANCEL.discard(uid)
            dur, _, _ = get_video_meta(in_path)
            dur = dur or 60

            cmd = [
                "ffmpeg", "-y",
                "-i", in_path,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                out_path
            ]
            rc = await ffmpeg_with_progress(cmd, status, uid, "Converting to MP4", dur)
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

    if media["size"] > MAX_CONVERT_SIZE:
        return await cb.answer("❌ Convert limit 500MB.", show_alert=True)

    in_path = media["path"]
    out_path = os.path.splitext(in_path)[0] + "_file.mp4"
    status = await get_or_create_status(cb.message, uid)

    async def job():
        try:
            USER_CANCEL.discard(uid)
            dur, _, _ = get_video_meta(in_path)
            dur = dur or 60

            cmd = [
                "ffmpeg", "-y",
                "-i", in_path,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                out_path
            ]
            rc = await ffmpeg_with_progress(cmd, status, uid, "Converting File → MP4", dur)
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

    if busy(uid):
        return await cb.answer("⚠️ One process running already!", show_alert=True)

    url = USER_URL[uid]
    mode = cb.data.replace("send_", "")

    await cb.answer()
    await cb.message.edit(f"✅ Selected: **{mode.upper()}**")

    status = await get_or_create_status(cb.message, uid)

    async def job():
        file_path = None
        try:
            USER_CANCEL.discard(uid)

            filename, total = await get_filename_and_size(url)
            if total and total > MAX_URL_SIZE:
                return await safe_edit(status, "❌ URL too large! Max 2GB.", reply_markup=kb_main_menu())

            if "." not in filename:
                filename += ".bin"

            file_path = os.path.join(DOWNLOAD_DIR, f"{uid}_{int(time.time())}_{filename}")
            await download_stream(url, file_path, status, uid)

            if uid in USER_CANCEL:
                raise asyncio.CancelledError

            size_bytes = os.path.getsize(file_path)
            clean_name = clean_display_name(os.path.basename(file_path))

            if mode == "video":
                await send_video_with_meta(
                    client, cb.message.chat.id, file_path,
                    caption=f"✅ URL Uploaded 🎥\n\n📌 Name: `{clean_name}`\n📦 Size: **{naturalsize(size_bytes)}**"
                )
            else:
                up_start = time.time()
                await client.send_document(
                    chat_id=cb.message.chat.id,
                    document=file_path,
                    caption=f"✅ URL Uploaded 📁\n\n📌 Name: `{clean_name}`\n📦 Size: **{naturalsize(size_bytes)}**",
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
            clean_file(file_path)
            USER_CANCEL.discard(uid)

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
