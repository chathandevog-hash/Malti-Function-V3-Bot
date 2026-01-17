import os
import re
import time
import json
import asyncio
import aiohttp
import humanize
import subprocess
from urllib.parse import urlparse, unquote

from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton


# ==========================
# LIMITS
# ==========================
URL_UPLOAD_LIMIT = 2 * 1024 * 1024 * 1024  # 2GB
CHUNK_SIZE = 1024 * 256


# ==========================
# UTILS
# ==========================
def is_url(text: str):
    return (text or "").startswith("http://") or (text or "").startswith("https://")

def naturalsize(num_bytes: int):
    if num_bytes is None:
        return "Unknown"
    if num_bytes <= 0:
        return "0 B"
    return humanize.naturalsize(num_bytes, binary=True)

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


# ==========================
# ✅ Video Metadata (duration/width/height)
# ==========================
def get_video_meta(path: str):
    """
    returns (duration:int, width:int, height:int)
    """
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration",
            "-of", "json",
            path
        ]
        out = subprocess.check_output(cmd).decode("utf-8", errors="ignore")
        data = json.loads(out)

        duration = int(float(data.get("format", {}).get("duration", 0) or 0))
        streams = data.get("streams", []) or []
        width = int(streams[0].get("width", 0) or 0) if streams else 0
        height = int(streams[0].get("height", 0) or 0) if streams else 0

        return duration, width, height
    except:
        return 0, 0, 0


# ==========================
# ✅ Smooth Streaming Fix (Seek Bug)
# ==========================
async def mp4_faststart(input_path: str, status_msg=None):
    """
    FastStart fix. No re-encode.
    """
    if not input_path.lower().endswith(".mp4"):
        return input_path

    out_path = os.path.splitext(input_path)[0] + "_fast.mp4"
    if os.path.exists(out_path):
        return out_path

    if status_msg:
        await safe_edit(status_msg, "⚡ Optimizing (FastStart)...\n⏳ Please wait...")

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-c", "copy",
        "-movflags", "+faststart",
        out_path
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL
    )
    await proc.wait()

    if os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
        return out_path

    return input_path


async def mp4_streaming_fix(input_path: str, status_msg=None):
    """
    ✅ Strong fix (Re-encode): Keyframes + FastStart
    prevents seek/back jumping to start in Telegram
    """
    if not input_path.lower().endswith(".mp4"):
        return input_path

    out_path = os.path.splitext(input_path)[0] + "_stream.mp4"
    if os.path.exists(out_path):
        return out_path

    if status_msg:
        await safe_edit(status_msg, "⚡ Fixing Smooth Streaming...\n(Keyframes + FastStart)\n⏳ Please wait...")

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-g", "48",
        "-keyint_min", "48",
        "-sc_threshold", "0",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        out_path
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL
    )
    await proc.wait()

    if os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
        return out_path

    return input_path


# ==========================
# ✅ HD THUMBNAIL (middle frame)
# ==========================
def get_video_duration(path: str):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error",
             "-select_streams", "v:0",
             "-show_entries", "stream=duration",
             "-of", "json", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        data = json.loads(r.stdout)
        d = float(data["streams"][0].get("duration") or 0)
        return int(d)
    except:
        return 0


async def gen_thumbnail(input_path: str, out_thumb: str):
    """
    ✅ HD thumbnail 1280px width from middle frame
    """
    dur = get_video_duration(input_path)
    ss = dur // 2 if dur and dur > 6 else 3

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(ss),
        "-i", input_path,
        "-frames:v", "1",
        "-vf", "scale=1280:-1",
        "-q:v", "1",
        out_thumb
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL
    )
    await proc.wait()

    return os.path.exists(out_thumb) and os.path.getsize(out_thumb) > 8000


# ==========================
# URL META
# ==========================
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


# ==========================
# DOWNLOAD STREAM
# ==========================
async def download_stream(url, file_path, status_msg, uid, USER_CANCEL):
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
                        kb = InlineKeyboardMarkup([
                            [InlineKeyboardButton("❌ Cancel Download", callback_data=f"cancel_{uid}")]
                        ])
                        await safe_edit(
                            status_msg,
                            make_progress_text("⬇️ Downloading", downloaded, total, speed, eta),
                            kb
                        )


# ==========================
# UPLOAD PROGRESS
# ==========================
async def upload_progress(current, total, status_msg, uid, start_time, USER_CANCEL):
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
            make_progress_text("📤 Uploading", current, total, speed, eta),
            kb
        )


# ==========================
# FLOW
# ==========================
URL_STATE = {}  # uid -> url

async def url_flow(client, message, url: str):
    if not is_url(url):
        return await message.reply("❌ Valid URL send cheyyu (http/https).")

    uid = message.from_user.id
    URL_STATE[uid] = url

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎥 Video Upload", callback_data="url_send_video"),
            InlineKeyboardButton("📁 File Upload", callback_data="url_send_file")
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
    ])
    await message.reply("✅ URL Received!\n\n👇 Select upload type:", reply_markup=kb)


async def url_callback_router(client, cb, USER_TASKS, USER_CANCEL, get_or_create_status, main_menu_keyboard, DOWNLOAD_DIR):
    uid = cb.from_user.id
    data = cb.data

    if data not in ["url_send_video", "url_send_file"]:
        return

    if uid not in URL_STATE:
        return await cb.message.edit("❌ Session expired. Send URL again.", reply_markup=main_menu_keyboard())

    url = URL_STATE[uid]
    mode = "video" if data.endswith("video") else "file"

    await cb.answer()
    status = await get_or_create_status(cb.message, uid)

    async def job():
        file_path = None
        thumb = None
        fixed_path = None
        try:
            USER_CANCEL.discard(uid)
            kb_cancel = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])

            fname, _ = await get_filename_and_size(url)
            fname_clean = clean_display_name(fname)

            file_path = os.path.join(DOWNLOAD_DIR, f"{uid}_{int(time.time())}_{fname}")

            await safe_edit(status, "⬇️ Starting download...", kb_cancel)
            await download_stream(url, file_path, status, uid, USER_CANCEL)

            if uid in USER_CANCEL:
                raise asyncio.CancelledError

            # ✅ streaming / seek fix
            fixed_path = await mp4_faststart(file_path, status_msg=status)
            if fixed_path.lower().endswith(".mp4"):
                fixed_path = await mp4_streaming_fix(fixed_path, status_msg=status)

            upload_file = fixed_path if fixed_path else file_path

            size = os.path.getsize(upload_file)
            up_start = time.time()

            if mode == "video":
                thumb = os.path.splitext(upload_file)[0] + "_thumb.jpg"
                try:
                    await gen_thumbnail(upload_file, thumb)
                except:
                    thumb = None

                # ✅ metadata
                duration, width, height = get_video_meta(upload_file)
                meta_args = {}
                if duration > 0: meta_args["duration"] = duration
                if width > 0: meta_args["width"] = width
                if height > 0: meta_args["height"] = height

                await safe_edit(status, "📤 Uploading...", kb_cancel)
                await client.send_video(
                    chat_id=cb.message.chat.id,
                    video=upload_file,
                    caption=f"✅ Uploaded 🎥\n\n📌 `{fname_clean}`\n📦 {naturalsize(size)}",
                    supports_streaming=True,
                    thumb=thumb if thumb and os.path.exists(thumb) else None,
                    progress=upload_progress,
                    progress_args=(status, uid, up_start, USER_CANCEL),
                    **meta_args
                )
            else:
                await safe_edit(status, "📤 Uploading...", kb_cancel)
                await client.send_document(
                    chat_id=cb.message.chat.id,
                    document=upload_file,
                    caption=f"✅ Uploaded 📁\n\n📌 `{fname_clean}`\n📦 {naturalsize(size)}",
                    progress=upload_progress,
                    progress_args=(status, uid, up_start, USER_CANCEL)
                )

            await safe_edit(status, "✅ Done ✅", reply_markup=main_menu_keyboard())

        except asyncio.CancelledError:
            await safe_edit(status, "❌ Cancelled ✅", reply_markup=main_menu_keyboard())
        except Exception as e:
            await safe_edit(status, f"❌ Failed!\n\nError: `{e}`", reply_markup=main_menu_keyboard())
        finally:
            URL_STATE.pop(uid, None)
            USER_CANCEL.discard(uid)

            for p in [thumb, fixed_path, file_path]:
                try:
                    if p and os.path.exists(p):
                        os.remove(p)
                except:
                    pass

    USER_TASKS[uid] = asyncio.create_task(job())
