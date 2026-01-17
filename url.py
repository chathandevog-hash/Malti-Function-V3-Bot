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
# URL INFO
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


# ==========================
# VIDEO META (for exact preview)
# ==========================
def ffprobe_video_info(path: str):
    """
    returns (duration_sec, w, h)
    """
    try:
        p = subprocess.run(
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
        data = json.loads(p.stdout)
        stream = data["streams"][0]
        w = int(stream.get("width") or 0)
        h = int(stream.get("height") or 0)
        duration = float(stream.get("duration") or 0)
        return int(duration), w, h
    except:
        return 0, 0, 0


async def gen_thumbnail(input_path: str, out_thumb: str):
    dur, _, _ = ffprobe_video_info(input_path)
    ss = dur // 2 if dur and dur > 8 else 3  # ✅ middle frame
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


async def fix_faststart(input_path: str, status_msg=None):
    """
    ✅ Fix streaming seek issue: move moov atom to beginning
    """
    if not input_path.lower().endswith(".mp4"):
        return input_path

    out_path = os.path.splitext(input_path)[0] + "_fast.mp4"

    if status_msg:
        await safe_edit(status_msg, "⚡ Fixing Streaming (Keyframes + FastStart)...\n⏳ Please wait...")

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

    if os.path.exists(out_path):
        try:
            os.remove(input_path)
        except:
            pass
        return out_path

    return input_path
