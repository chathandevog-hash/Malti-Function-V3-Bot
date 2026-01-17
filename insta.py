import os
import re
import time
import json
import asyncio
import subprocess
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")

INSTA_REGEX = re.compile(r"(https?://(www\.)?instagram\.com/(reel|p)/[A-Za-z0-9_\-]+)")

# Will be imported from bot.py
try:
    from bot import USER_CANCEL
except:
    USER_CANCEL = set()


def is_instagram_url(text: str) -> bool:
    return bool(INSTA_REGEX.search(text or ""))


def clean_insta_url(text: str) -> str:
    m = INSTA_REGEX.search(text or "")
    return m.group(1) if m else (text or "").strip()


async def safe_edit(msg, text, reply_markup=None):
    try:
        await msg.edit(text, reply_markup=reply_markup)
    except:
        pass


# ===============================
# Utils: ffprobe metadata
# ===============================
def ffprobe_info(path: str):
    """
    returns dict: {"duration": float, "width": int, "height": int}
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
        duration = float(data.get("format", {}).get("duration", 0) or 0)
        streams = data.get("streams", []) or []
        width = int(streams[0].get("width", 0) or 0) if streams else 0
        height = int(streams[0].get("height", 0) or 0) if streams else 0
        return {"duration": duration, "width": width, "height": height}
    except:
        return {"duration": 0, "width": 0, "height": 0}


# ===============================
# Utils: thumbnail generator
# ===============================
def make_thumb(video_path: str):
    """
    Generate thumbnail from middle frame (not starting frame)
    """
    info = ffprobe_info(video_path)
    duration = info.get("duration", 0) or 0
    if duration <= 0:
        ts = 1
    else:
        ts = max(1, int(duration / 2))

    thumb_path = video_path + ".jpg"
    try:
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(ts),
            "-i", video_path,
            "-vframes", "1",
            "-q:v", "3",
            thumb_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 5000:
            return thumb_path
    except:
        pass
    return None


# ===============================
# Always alive progress animation
# ===============================
def fancy_bar(step: int):
    bars = [
        "⚪⚪⚪⚪⚪⚪⚪⚪⚪⚪⚪⚪⚪⚪",
        "🔴🔴⚪⚪⚪⚪⚪⚪⚪⚪⚪⚪⚪⚪",
        "🟠🟠🟠🟠⚪⚪⚪⚪⚪⚪⚪⚪⚪⚪",
        "🟡🟡🟡🟡🟡🟡⚪⚪⚪⚪⚪⚪⚪⚪",
        "🟢🟢🟢🟢🟢🟢🟢🟢⚪⚪⚪⚪⚪⚪",
        "✅✅✅✅✅✅✅✅✅✅✅✅✅✅",
    ]
    return bars[step % len(bars)]


async def insta_download(url: str, uid: int, status_msg=None):
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    url = clean_insta_url(url)

    outtmpl = os.path.join(DOWNLOAD_DIR, f"insta_{uid}_{int(time.time())}.%(ext)s")

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--socket-timeout", "30",
        "--retries", "10",
        "--newline",
        "-f", "bv*+ba/best",
        "--merge-output-format", "mp4",
        "-o", outtmpl,
        url
    ]

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])

    if status_msg:
        await safe_edit(
            status_msg,
            "📥 Instagram Reel Detected ✅\n\n⚙️ Downloading reel...\n⏳ Please wait...",
            reply_markup=kb
        )

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )

    last_ui = 0
    step = 0

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

        # Even if yt-dlp doesn't give progress, keep UI alive every 2-3 seconds
        if status_msg and time.time() - last_ui > 2.5:
            last_ui = time.time()
            step += 1
            await safe_edit(
                status_msg,
                f"📥 Instagram Reel Detected ✅\n\n⚙️ Downloading reel...\n\n{fancy_bar(step)}\n\n⏳ Please wait...",
                reply_markup=kb
            )

    await proc.wait()

    if proc.returncode != 0:
        raise Exception("Insta download failed")

    mp4_path = outtmpl.replace("%(ext)s", "mp4")
    if os.path.exists(mp4_path):
        return mp4_path

    # fallback
    files = [f for f in os.listdir(DOWNLOAD_DIR) if f.startswith(f"insta_{uid}_") and f.endswith(".mp4")]
    if not files:
        raise Exception("Downloaded mp4 not found")
    files.sort(key=lambda x: os.path.getmtime(os.path.join(DOWNLOAD_DIR, x)), reverse=True)
    return os.path.join(DOWNLOAD_DIR, files[0])


# =========================
# ENTRY: Auto start download
# =========================
async def insta_entry(client, message, url: str, USER_TASKS, main_menu_keyboard):
    uid = message.from_user.id

    # ✅ Auto start (no buttons)
    status = await message.reply("📥 Instagram Reel Detected ✅\n\n⏳ Starting...")

    async def job():
        file_path = None
        thumb_path = None
        try:
            USER_CANCEL.discard(uid)
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])

            file_path = await insta_download(url, uid, status_msg=status)

            if uid in USER_CANCEL:
                raise asyncio.CancelledError

            await safe_edit(
                status,
                f"📤 Uploading reel...\n\n{fancy_bar(4)}\n\n⏳ Please wait...",
                reply_markup=kb
            )

            # ✅ correct thumbnail + correct metadata
            thumb_path = make_thumb(file_path)
            info = ffprobe_info(file_path)

            await client.send_video(
                chat_id=message.chat.id,
                video=file_path,
                caption="✅ Instagram Reel 🎥",
                supports_streaming=True,
                duration=int(info.get("duration") or 0),
                width=int(info.get("width") or 0),
                height=int(info.get("height") or 0),
                thumb=thumb_path if thumb_path and os.path.exists(thumb_path) else None
            )

            await safe_edit(status, "✅ Done ✅", reply_markup=main_menu_keyboard())

        except asyncio.CancelledError:
            await safe_edit(status, "❌ Cancelled ✅", reply_markup=main_menu_keyboard())
        except Exception as e:
            await safe_edit(status, f"❌ Insta Failed!\n\nError: `{e}`", reply_markup=main_menu_keyboard())
        finally:
            USER_CANCEL.discard(uid)
            try:
                if file_path and os.path.exists(file_path):
                    os.remove(file_path)
            except:
                pass
            try:
                if thumb_path and os.path.exists(thumb_path):
                    os.remove(thumb_path)
            except:
                pass

    USER_TASKS[uid] = asyncio.create_task(job())
