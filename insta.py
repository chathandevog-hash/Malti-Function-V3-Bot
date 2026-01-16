import os
import re
import time
import asyncio
import subprocess
import humanize

from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")

INSTA_REGEX = re.compile(r"(https?://(www\.)?instagram\.com/(reel|p)/[A-Za-z0-9_\-]+)")

# -------------------------
# Cancel system (bot.py-il USER_CANCEL undenkil ath use cheyyam)
# -------------------------
try:
    from bot import USER_CANCEL   # if exists
except:
    USER_CANCEL = set()


def is_instagram_url(text: str) -> bool:
    return bool(INSTA_REGEX.search(text or ""))


def clean_insta_url(text: str) -> str:
    m = INSTA_REGEX.search(text or "")
    return m.group(1) if m else (text or "").strip()


def format_time(seconds: float):
    if seconds <= 0:
        return "0s"
    m, s = divmod(int(seconds), 60)
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
    bar = "⚪" * filled + "⚫" * (bar_len - filled)

    d_str = humanize.naturalsize(downloaded, binary=True)
    t_str = humanize.naturalsize(total, binary=True) if total else "Unknown"
    s_str = humanize.naturalsize(speed, binary=True) + "/s" if speed else "0 B/s"

    return (
        f"{title}\n\n"
        f"`{bar}`  **{percent:.2f}%**\n"
        f"📦 Size: {d_str} / {t_str}\n"
        f"⚡ Speed: {s_str}\n"
        f"⏳ ETA: {format_time(eta)}"
    )


async def insta_download(url: str, uid: int, status_msg=None):
    """
    Download Instagram reel using yt-dlp with real progress bar.
    Returns mp4 filepath.
    """
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    url = clean_insta_url(url)
    outtmpl = os.path.join(DOWNLOAD_DIR, f"insta_{uid}_{int(time.time())}.%(ext)s")

    cmd = [
        "yt-dlp",
        "-f", "bv*+ba/best",
        "--merge-output-format", "mp4",
        "-o", outtmpl,
        "--newline",   # IMPORTANT: progress lines separately
        url
    ]

    start_time = time.time()
    last_edit = 0

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]
    ])

    if status_msg:
        try:
            await status_msg.edit("📥 Instagram Reel downloading...\n⏳ Please wait...", reply_markup=kb)
        except:
            pass

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )

    downloaded = 0
    total = 0

    # yt-dlp output parsing
    progress_re = re.compile(
        r"\[download\]\s+(?P<pct>\d+(\.\d+)?)%\s+of\s+(?P<total>[\d\.]+[KMG]iB)\s+at\s+(?P<speed>[\d\.]+[KMG]iB/s)\s+ETA\s+(?P<eta>[\d:]+)"
    )

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

        m = progress_re.search(text)
        if m:
            pct = float(m.group("pct"))

            # Convert total to bytes
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

            total = to_bytes(m.group("total"))
            downloaded = int(total * pct / 100)

            elapsed = time.time() - start_time
            speed = downloaded / elapsed if elapsed > 0 else 0
            eta = (total - downloaded) / speed if speed > 0 else 0

            if status_msg and time.time() - last_edit > 2:
                last_edit = time.time()
                try:
                    await status_msg.edit(
                        make_progress_text("📥 Downloading Reel...", downloaded, total, speed, eta),
                        reply_markup=kb
                    )
                except:
                    pass

    await proc.wait()

    if proc.returncode != 0:
        raise Exception("Insta download failed. Try again later.")

    mp4_path = outtmpl.replace("%(ext)s", "mp4")
    if os.path.exists(mp4_path):
        if status_msg:
            try:
                await status_msg.edit("✅ Reel Downloaded Successfully 🎉", reply_markup=kb)
            except:
                pass
        return mp4_path

    # fallback scan
    files = [f for f in os.listdir(DOWNLOAD_DIR) if f.startswith(f"insta_{uid}_") and f.endswith(".mp4")]
    if not files:
        raise Exception("Downloaded MP4 not found.")
    files.sort(key=lambda x: os.path.getmtime(os.path.join(DOWNLOAD_DIR, x)), reverse=True)
    return os.path.join(DOWNLOAD_DIR, files[0])
