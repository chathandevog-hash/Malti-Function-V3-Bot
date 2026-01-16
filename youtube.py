import os
import re
import time
import asyncio
import humanize

from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")

YT_REGEX = re.compile(
    r"(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)[A-Za-z0-9_\-]+"
)

# Cancel set from bot.py
try:
    from bot import USER_CANCEL
except:
    USER_CANCEL = set()

def is_youtube_url(text: str) -> bool:
    return bool(YT_REGEX.search(text or ""))

def clean_youtube_url(text: str) -> str:
    return (text or "").strip()

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

def make_progress_text(title, downloaded, total, speed, eta):
    percent = (downloaded / total * 100) if total else 0
    bar_len = 18
    filled = int(bar_len * percent / 100) if total else 0
    bar = "🟢" * filled + "⚪" * (bar_len - filled)

    d_str = humanize.naturalsize(downloaded, binary=True)
    t_str = humanize.naturalsize(total, binary=True) if total else "Unknown"
    s_str = humanize.naturalsize(speed, binary=True) + "/s" if speed else "0 B/s"

    return (
        f"{title}\n\n"
        f"{bar}\n"
        f"📊 **{percent:.2f}%**\n"
        f"📦 {d_str} / {t_str}\n"
        f"⚡ {s_str}\n"
        f"⏳ ETA: {format_time(eta)}"
    )

def _yt_quality_format(q: str):
    h = int(q.replace("p", ""))
    return f"bv*[height<={h}]+ba/b[height<={h}]/best"

async def youtube_download_video(url: str, uid: int, quality: str, status_msg=None):
    """
    YouTube video download with real progress bar.
    """
    import subprocess

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    url = clean_youtube_url(url)
    outtmpl = os.path.join(DOWNLOAD_DIR, f"yt_{uid}_{int(time.time())}.%(ext)s")
    fmt = _yt_quality_format(quality)

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--newline",
        "--progress",
        "--retries", "5",
        "--socket-timeout", "30",
        "-f", fmt,
        "--merge-output-format", "mp4",
        "-o", outtmpl,
        url
    ]

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])

    if status_msg:
        try:
            await status_msg.edit(f"📥 Downloading YouTube ({quality})...\n⏳ Please wait...", reply_markup=kb)
        except:
            pass

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )

    start_time = time.time()
    last_edit = 0

    progress_re = re.compile(
        r"\[download\]\s+(?P<pct>\d+(\.\d+)?)%\s+of\s+(?P<total>[\d\.]+[KMG]iB)\s+at\s+(?P<speed>[\d\.]+[KMG]iB/s)\s+ETA\s+(?P<eta>[\d:]+)"
    )

    def to_bytes(x):
        x = x.strip()
        val = float(re.findall(r"[\d\.]+", x)[0])
        if "KiB" in x:
            return int(val * 1024)
        if "MiB" in x:
            return int(val * 1024 * 1024)
        if "GiB" in x:
            return int(val * 1024 * 1024 * 1024)
        return int(val)

    downloaded = 0
    total = 0

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

        text = line.decode(errors="ignore").strip()
        low = text.lower()

        if "private video" in low or "restricted" in low or "sign in" in low:
            try:
                proc.kill()
            except:
                pass
            raise Exception("YouTube video is private / restricted / blocked. Try another link.")

        m = progress_re.search(text)
        if m:
            pct = float(m.group("pct"))
            total = to_bytes(m.group("total"))
            downloaded = int(total * pct / 100)

            elapsed = time.time() - start_time
            speed = downloaded / elapsed if elapsed > 0 else 0
            eta = (total - downloaded) / speed if speed > 0 else 0

            if status_msg and time.time() - last_edit > 2:
                last_edit = time.time()
                try:
                    await status_msg.edit(
                        make_progress_text("📥 Downloading YouTube...", downloaded, total, speed, eta),
                        reply_markup=kb
                    )
                except:
                    pass

    await proc.wait()

    if proc.returncode != 0:
        raise Exception("YouTube download failed. Try another link.")

    mp4_path = outtmpl.replace("%(ext)s", "mp4")
    if os.path.exists(mp4_path):
        return mp4_path

    files = [f for f in os.listdir(DOWNLOAD_DIR) if f.startswith(f"yt_{uid}_") and f.endswith(".mp4")]
    if not files:
        raise Exception("Downloaded MP4 not found.")
    files.sort(key=lambda x: os.path.getmtime(os.path.join(DOWNLOAD_DIR, x)), reverse=True)
    return os.path.join(DOWNLOAD_DIR, files[0])
