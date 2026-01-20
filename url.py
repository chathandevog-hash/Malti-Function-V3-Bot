import os
import re
import time
import asyncio
import aiohttp
import humanize
import subprocess
from urllib.parse import urlparse, unquote

from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# -------------------------
# Config
# -------------------------
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")
URL_UPLOAD_LIMIT = 2 * 1024 * 1024 * 1024  # ✅ 2GB
CHUNK_SIZE = 1024 * 256

URL_STATE = {}              # uid -> url
PROGRESS_LAST_EDIT = {}     # uid -> last edit time


# -------------------------
# Utils
# -------------------------
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


# ✅ UI same bar style (unchanged)
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
    if not total or total <= 0:
        bar = make_circle_bar(0)
        speed_str = naturalsize(int(speed)) + "/s" if speed else "0 B/s"
        return (
            f"✨ **{title}**\n\n"
            f"{bar}\n\n"
            f"📊 Progress: **-- %**\n"
            f"📦 Downloaded: **{naturalsize(done)}**\n"
            f"⚡ Speed: **{speed_str}**\n"
            f"⏳ ETA: **{format_time(eta)}**"
        )

    percent = (done / total * 100)
    bar = make_circle_bar(percent)
    speed_str = naturalsize(int(speed)) + "/s" if speed else "0 B/s"
    return (
        f"✨ **{title}**\n\n"
        f"{bar}\n\n"
        f"📊 Progress: **{percent:.2f}%**\n"
        f"📦 Size: **{naturalsize(done)} / {naturalsize(total)}**\n"
        f"⚡ Speed: **{speed_str}**\n"
        f"⏳ ETA: **{format_time(eta)}**"
    )


async def safe_edit(msg, text, reply_markup=None):
    try:
        await msg.edit(text, reply_markup=reply_markup)
    except:
        pass


# -------------------------
# URL meta
# -------------------------
async def get_filename_and_size(url: str):
    filename = None
    total = 0
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, allow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as r:
                if r.headers.get("Content-Length"):
                    total = int(r.headers.get("Content-Length") or 0)

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


# -------------------------
# FFMPEG
# -------------------------
def _ffmpeg_exists():
    try:
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["ffprobe", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except:
        return False


def ffprobe_video_info(path: str):
    """
    ✅ OLD FEATURE: needed for Telegram streaming + resume
    returns: (duration, width, height)
    """
    if not _ffmpeg_exists():
        return (0, 0, 0)

    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path
        ]
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode(errors="ignore").strip().splitlines()
        # output order can vary => parse safely
        dur = 0
        w = 0
        h = 0
        for line in out:
            line = line.strip()
            if "." in line and dur == 0:
                try:
                    dur = int(float(line))
                except:
                    pass
            else:
                try:
                    v = int(line)
                    if w == 0:
                        w = v
                    elif h == 0:
                        h = v
                except:
                    pass
        return (dur, w, h)
    except:
        return (0, 0, 0)


def fix_streaming_seek(input_path: str):
    """
    🔥 OLD BEST FEATURE:
    Telegram resume/seek fix (re-encode with keyframes + genpts + faststart)
    Works more than remux.
    """
    if not _ffmpeg_exists():
        return input_path

    out_path = input_path + "_seekfix.mp4"

    # ✅ Very important flags for Telegram seek/resume
    cmd = [
        "ffmpeg", "-y",
        "-fflags", "+genpts",
        "-i", input_path,
        "-avoid_negative_ts", "make_zero",
        "-map", "0",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-g", "48",
        "-keyint_min", "48",
        "-sc_threshold", "0",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        out_path
    ]

    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        try:
            os.remove(input_path)
        except:
            pass
        return out_path

    return input_path


def generate_middle_thumbnail(video_path: str):
    if not _ffmpeg_exists():
        return None
    try:
        thumb = video_path + "_thumb.jpg"

        duration = 0.0
        try:
            p = subprocess.check_output([
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path
            ]).decode().strip()
            duration = float(p) if p else 0.0
        except:
            duration = 0.0

        middle = duration / 2 if duration > 2 else 1
        cmd = ["ffmpeg", "-y", "-ss", str(middle), "-i", video_path, "-vframes", "1", "-q:v", "4", thumb]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        if os.path.exists(thumb) and os.path.getsize(thumb) > 0:
            return thumb
    except:
        pass
    return None


# -------------------------
# PROGRESS
# -------------------------
async def upload_progress(current, total, status_msg, uid, start_time, USER_CANCEL: set):
    if uid in USER_CANCEL:
        raise asyncio.CancelledError

    elapsed = time.time() - start_time
    speed = current / elapsed if elapsed > 0 else 0
    eta = (total - current) / speed if speed > 0 else 0

    now = time.time()
    last = PROGRESS_LAST_EDIT.get(uid, 0)
    if now - last > 3:
        PROGRESS_LAST_EDIT[uid] = now
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Upload", callback_data=f"cancel_{uid}")]])
        await safe_edit(status_msg, make_progress_text("📤 Uploading...", current, total, speed, eta), kb)


async def download_stream(url, file_path, status_msg, uid, USER_CANCEL: set):
    """
    ✅ NEW: Fix stuck with stall timeout detector
    """
    USER_CANCEL.discard(uid)

    timeout = aiohttp.ClientTimeout(sock_connect=30, sock_read=30, total=None)
    headers = {"User-Agent": "Mozilla/5.0"}

    downloaded = 0
    start_time = time.time()
    last_edit = 0
    total = 0

    last_chunk_time = time.time()

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, allow_redirects=True, headers=headers) as r:
            if r.status != 200:
                raise Exception(f"HTTP {r.status}")

            ctype = (r.headers.get("Content-Type") or "").lower()
            if "text/html" in ctype:
                raise Exception("URL is not a direct file link (HTML page detected)")

            if r.headers.get("Content-Length"):
                total = int(r.headers.get("Content-Length") or 0)

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
                        if time.time() - last_chunk_time > 60:
                            raise Exception("Download stalled (no data). Try again.")
                        continue

                    last_chunk_time = time.time()
                    f.write(chunk)
                    downloaded += len(chunk)

                    elapsed = time.time() - start_time
                    speed = downloaded / elapsed if elapsed > 0 else 0
                    eta = (total - downloaded) / speed if total and speed > 0 else 0

                    if time.time() - last_edit > 3:
                        last_edit = time.time()
                        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Download", callback_data=f"cancel_{uid}")]])
                        await safe_edit(status_msg, make_progress_text("⬇️ Downloading...", downloaded, total, speed, eta), kb)


# -------------------------
# PUBLIC API
# -------------------------
async def url_flow(client, message, url: str):
    uid = message.from_user.id
    URL_STATE[uid] = url

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎥 Video Upload (MP4)", callback_data="url_send_video"),
            InlineKeyboardButton("📁 File Upload", callback_data="url_send_file")
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
    ])

    try:
        await message.edit_text("✅ URL Detected 🌐\n\n👇 Choose upload type:", reply_markup=kb)
    except:
        await message.reply("✅ URL Detected 🌐\n\n👇 Choose upload type:", reply_markup=kb)


async def url_callback_router(
    client,
    cb,
    USER_TASKS,
    USER_CANCEL,
    get_or_create_status,
    main_menu_keyboard,
    DOWNLOAD_DIR
):
    uid = cb.from_user.id
    data = cb.data

    if uid not in URL_STATE:
        return await cb.message.edit("❌ Session expired. Send URL again.", reply_markup=main_menu_keyboard())

    url = URL_STATE[uid]
    mode = "video" if data == "url_send_video" else "file"

    await cb.answer("⏳ Processing...", show_alert=False)

    status = await get_or_create_status(cb.message, uid)
    await safe_edit(status, "⏳ Processing started...\n\n⬇️ Preparing download...")
    await asyncio.sleep(0.2)

    async def job():
        file_path = None
        thumb_path = None

        try:
            USER_CANCEL.discard(uid)

            fname, _ = await get_filename_and_size(url)
            name_clean = clean_display_name(fname)
            file_path = os.path.join(DOWNLOAD_DIR, f"url_{uid}_{int(time.time())}_{fname}")

            # ✅ Download
            await download_stream(url, file_path, status, uid, USER_CANCEL)

            if uid in USER_CANCEL:
                raise asyncio.CancelledError

            size = os.path.getsize(file_path)

            # ✅ Video pipeline (OLD seek fix restore)
            dur = w = h = 0
            if mode == "video":
                await safe_edit(status, "🎥 Fixing Streaming + Seek/Resume...\n\n⏳ Please wait...")

                # 🔥 Restore old working logic
                file_path = fix_streaming_seek(file_path)

                dur, w, h = ffprobe_video_info(file_path)
                name_clean = clean_display_name(os.path.basename(file_path))

                await safe_edit(status, "🖼 Generating Thumbnail (Middle Frame)...\n\n⏳ Please wait...")
                thumb_path = generate_middle_thumbnail(file_path)

            # ✅ Upload
            up_start = time.time()
            if mode == "video":
                await safe_edit(status, "📤 Upload Starting (Video MP4)...")
                await client.send_video(
                    chat_id=cb.message.chat.id,
                    video=file_path,
                    thumb=thumb_path if thumb_path else None,
                    caption=f"✅ Uploaded 🎥\n\n📌 `{name_clean}`\n📦 {naturalsize(size)}",
                    supports_streaming=True,
                    duration=dur if dur else None,
                    width=w if w else None,
                    height=h if h else None,
                    progress=upload_progress,
                    progress_args=(status, uid, up_start, USER_CANCEL),
                )
            else:
                await safe_edit(status, "📤 Upload Starting (File)...")
                await client.send_document(
                    chat_id=cb.message.chat.id,
                    document=file_path,
                    caption=f"✅ Uploaded 📁\n\n📌 `{name_clean}`\n📦 {naturalsize(size)}",
                    progress=upload_progress,
                    progress_args=(status, uid, up_start, USER_CANCEL),
                )

            await safe_edit(status, "✅ Done ✅", reply_markup=main_menu_keyboard())

        except asyncio.CancelledError:
            await safe_edit(status, "❌ Cancelled ✅", reply_markup=main_menu_keyboard())

        except Exception as e:
            await safe_edit(status, f"❌ URL Upload Failed!\n\nError: `{e}`", reply_markup=main_menu_keyboard())

        finally:
            URL_STATE.pop(uid, None)
            USER_CANCEL.discard(uid)

            try:
                if thumb_path and os.path.exists(thumb_path):
                    os.remove(thumb_path)
            except:
                pass

            try:
                if file_path and os.path.exists(file_path):
                    os.remove(file_path)
            except:
                pass

    USER_TASKS[uid] = asyncio.create_task(job())
