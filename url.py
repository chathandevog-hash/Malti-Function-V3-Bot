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

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")

URL_UPLOAD_LIMIT = 2 * 1024 * 1024 * 1024  # 2GB
CHUNK_SIZE = 1024 * 256

URL_STATE = {}  # uid -> url


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


# ✅ upload progress bar
async def upload_progress(current, total, status_msg, uid, start_time, USER_CANCEL: set):
    if uid in USER_CANCEL:
        raise asyncio.CancelledError

    elapsed = time.time() - start_time
    speed = current / elapsed if elapsed > 0 else 0
    eta = (total - current) / speed if speed > 0 else 0

    now = time.time()
    if not hasattr(status_msg, "_last_edit"):
        status_msg._last_edit = 0

    if now - status_msg._last_edit > 3:
        status_msg._last_edit = now
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Upload", callback_data=f"cancel_{uid}")]])
        await safe_edit(status_msg, make_progress_text("📤 Uploading...", current, total, speed, eta), kb)


async def download_stream(url, file_path, status_msg, uid, USER_CANCEL: set):
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

            kb0 = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Download", callback_data=f"cancel_{uid}")]])
            await safe_edit(status_msg, make_progress_text("⬇️ Downloading...", 0, total, 0, 0), kb0)

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

                    if time.time() - last_edit > 3:
                        last_edit = time.time()
                        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Download", callback_data=f"cancel_{uid}")]])
                        await safe_edit(status_msg, make_progress_text("⬇️ Downloading...", downloaded, total, speed, eta), kb)


def ffprobe_duration(path: str) -> float:
    """✅ format duration is more accurate than stream duration"""
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nk=1:nw=1", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        return float(p.stdout.strip() or "0")
    except:
        return 0.0


def ffprobe_video_info(path: str):
    try:
        p = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "json",
                path
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        data = json.loads(p.stdout)
        stream = data["streams"][0]
        w = int(stream.get("width") or 0)
        h = int(stream.get("height") or 0)
        dur = int(ffprobe_duration(path) or 0)
        return dur, w, h
    except:
        return 0, 0, 0


async def gen_thumbnail(input_path: str, out_thumb: str):
    dur, _, _ = ffprobe_video_info(input_path)
    ss = int(dur // 2) if dur and dur > 8 else 3
    cmd = ["ffmpeg", "-y", "-ss", str(ss), "-i", input_path, "-frames:v", "1", "-vf", "scale=640:-1", "-q:v", "2", out_thumb]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()
    return os.path.exists(out_thumb)


def parse_ffmpeg_time_to_seconds(t: str):
    try:
        parts = t.split(":")
        if len(parts) != 3:
            return 0.0
        h = float(parts[0])
        m = float(parts[1])
        s = float(parts[2])
        return h * 3600 + m * 60 + s
    except:
        return 0.0


# ✅ Fix seek reset issue (GENPTS + avoid_negative_ts + faststart + keyframes)
async def fix_streaming_seek(input_path: str, status_msg, uid, USER_CANCEL: set):
    if not input_path.lower().endswith(".mp4"):
        return input_path

    out_path = os.path.splitext(input_path)[0] + "_stream.mp4"

    total_duration = ffprobe_duration(input_path)

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])
    await safe_edit(status_msg, "⚡ Fixing Streaming (Seek Fix)...\n⏳ Please wait...", kb)

    cmd = [
        "ffmpeg", "-y",
        "-fflags", "+genpts",
        "-i", input_path,
        "-avoid_negative_ts", "make_zero",
        "-max_muxing_queue_size", "1024",
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
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )

    last_edit = 0
    last_sec = 0.0

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

        try:
            s = line.decode(errors="ignore")
        except:
            s = ""

        if "time=" in s:
            m = re.search(r"time=(\d+:\d+:\d+\.?\d*)", s)
            if m:
                last_sec = parse_ffmpeg_time_to_seconds(m.group(1))

        if time.time() - last_edit > 6:
            last_edit = time.time()
            pct = 0
            if total_duration and total_duration > 0:
                pct = max(0, min(100, (last_sec / total_duration) * 100))
            await safe_edit(status_msg, f"⚡ Fixing Streaming...\n\n{make_circle_bar(pct)}\n\n📊 **{pct:.2f}%**", kb)

    await proc.wait()

    if proc.returncode != 0 or not os.path.exists(out_path):
        return input_path

    try:
        os.remove(input_path)
    except:
        pass

    return out_path


# ==========================
# PUBLIC API FOR bot.py
# ==========================
async def url_flow(client, message, url: str):
    uid = message.from_user.id
    URL_STATE[uid] = url

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎥 Video Upload", callback_data="url_send_video"),
         InlineKeyboardButton("📁 File Upload", callback_data="url_send_file")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
    ])
    await message.reply("✅ URL Detected 🌐\n\n👇 Choose upload type:", reply_markup=kb)


async def url_callback_router(client, cb, USER_TASKS, USER_CANCEL, get_or_create_status, main_menu_keyboard, DOWNLOAD_DIR):
    uid = cb.from_user.id
    data = cb.data

    if uid not in URL_STATE:
        return await cb.message.edit("❌ Session expired. Send URL again.", reply_markup=main_menu_keyboard())

    url = URL_STATE[uid]
    mode = "video" if data == "url_send_video" else "file"

    await cb.answer()
    status = await get_or_create_status(cb.message, uid)

    async def job():
        file_path = None
        thumb = None
        try:
            USER_CANCEL.discard(uid)

            fname, _ = await get_filename_and_size(url)
            name_clean = clean_display_name(fname)

            file_path = os.path.join(DOWNLOAD_DIR, f"url_{uid}_{int(time.time())}_{fname}")

            await safe_edit(status, "⬇️ Starting download...")
            await download_stream(url, file_path, status, uid, USER_CANCEL)

            if uid in USER_CANCEL:
                raise asyncio.CancelledError

            upload_path = file_path

            # ✅ FORCE fix for mp4 seek
            if mode == "video" and file_path.lower().endswith(".mp4"):
                upload_path = await fix_streaming_seek(file_path, status, uid, USER_CANCEL)

            size = os.path.getsize(upload_path)
            up_start = time.time()

            if mode == "video":
                thumb = os.path.splitext(upload_path)[0] + "_thumb.jpg"
                try:
                    await gen_thumbnail(upload_path, thumb)
                except:
                    thumb = None

                dur, w, h = ffprobe_video_info(upload_path)

                args = {}
                if dur > 0: args["duration"] = dur
                if w > 0: args["width"] = w
                if h > 0: args["height"] = h

                await safe_edit(status, "📤 Upload Starting...")
                await client.send_video(
                    chat_id=cb.message.chat.id,
                    video=upload_path,
                    caption=f"✅ Uploaded 🎥\n\n📌 `{name_clean}`\n📦 {naturalsize(size)}",
                    supports_streaming=True,
                    thumb=thumb if thumb and os.path.exists(thumb) else None,
                    progress=upload_progress,
                    progress_args=(status, uid, up_start, USER_CANCEL),
                    **args
                )
            else:
                await safe_edit(status, "📤 Upload Starting...")
                await client.send_document(
                    chat_id=cb.message.chat.id,
                    document=upload_path,
                    caption=f"✅ Uploaded 📁\n\n📌 `{name_clean}`\n📦 {naturalsize(size)}",
                    progress=upload_progress,
                    progress_args=(status, uid, up_start, USER_CANCEL)
                )

            await safe_edit(status, "✅ Done ✅", reply_markup=main_menu_keyboard())

        except asyncio.CancelledError:
            await safe_edit(status, "❌ Cancelled ✅", reply_markup=main_menu_keyboard())
        except Exception as e:
            await safe_edit(status, f"❌ URL Upload Failed!\n\nError: `{e}`", reply_markup=main_menu_keyboard())
        finally:
            URL_STATE.pop(uid, None)
            USER_CANCEL.discard(uid)

            for p in [thumb, file_path]:
                try:
                    if p and os.path.exists(p):
                        os.remove(p)
                except:
                    pass

    USER_TASKS[uid] = asyncio.create_task(job())
