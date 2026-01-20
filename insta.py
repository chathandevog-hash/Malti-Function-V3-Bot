import os
import re
import time
import json
import asyncio
import subprocess

from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")

INSTA_REGEX = re.compile(r"(https?://(www\.)?instagram\.com/(reel|p)/[A-Za-z0-9_\-]+)")


try:
    from bot import USER_CANCEL
except:
    USER_CANCEL = set()


def is_instagram_url(text: str) -> bool:
    return bool(INSTA_REGEX.search(text or ""))


def clean_insta_url(text: str) -> str:
    m = INSTA_REGEX.search(text or "")
    return m.group(1) if m else (text or "").strip()


# ===============================
# FLOODWAIT SAFE HELPERS ✅
# ===============================
async def safe_send(message, text, reply_markup=None):
    while True:
        try:
            return await message.reply(text, reply_markup=reply_markup)
        except FloodWait as e:
            await asyncio.sleep(int(e.value) + 1)
        except:
            return None


async def safe_edit(msg, text, reply_markup=None):
    if not msg:
        return
    while True:
        try:
            await msg.edit(text, reply_markup=reply_markup)
            return
        except FloodWait as e:
            await asyncio.sleep(int(e.value) + 1)
        except:
            return


# ===============================
# ffprobe metadata
# ===============================
def ffprobe_info(path: str):
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration",
            "-of", "json", path
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
# middle thumbnail
# ===============================
def make_thumb(video_path: str):
    info = ffprobe_info(video_path)
    duration = info.get("duration", 0) or 0
    ts = 1 if duration <= 0 else max(1, int(duration / 2))

    thumb_path = video_path + ".jpg"
    try:
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(ts),
            "-i", video_path,
            "-vframes", "1",
            "-vf", "scale=640:-1",
            "-q:v", "3",
            thumb_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 5000:
            return thumb_path
    except:
        pass
    return None


def square_bar(percent: float) -> str:
    total = 12
    filled = int((percent / 100.0) * total)

    if percent < 5:
        fill = "⚪"
    elif percent < 25:
        fill = "🟥"
    elif percent < 50:
        fill = "🟧"
    elif percent < 80:
        fill = "🟨"
    else:
        fill = "🟩"

    bar = (fill * filled) + ("⬜" * (total - filled))
    return f"{bar}  {percent:.1f}%"


def has_aria2c():
    try:
        subprocess.run(["aria2c", "-v"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except:
        return False


# ===============================
# yt-dlp download ✅
# ===============================
async def insta_download(url: str, uid: int, status_msg):
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    url = clean_insta_url(url)

    outtmpl = os.path.join(DOWNLOAD_DIR, f"insta_{uid}_{int(time.time())}.%(ext)s")

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--newline",
        "--socket-timeout", "25",
        "--retries", "3",
        "--fragment-retries", "3",
        "-f", "best[ext=mp4]/best",
        "-o", outtmpl,
        url
    ]

    if has_aria2c():
        cmd.insert(1, "--downloader")
        cmd.insert(2, "aria2c")
        cmd.insert(3, "--downloader-args")
        cmd.insert(4, "aria2c:-x 16 -s 16 -k 1M")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])

    last_edit = 0
    last_percent = -1.0
    last_output_time = time.time()

    while True:
        if uid in USER_CANCEL:
            try:
                proc.kill()
            except:
                pass
            raise asyncio.CancelledError

        # ✅ Prevent infinite hang (no output)
        if time.time() - last_output_time > 90:
            try:
                proc.kill()
            except:
                pass
            raise Exception("Download timeout / No response from Instagram")

        line = await proc.stdout.readline()
        if not line:
            break

        last_output_time = time.time()

        s = line.decode("utf-8", errors="ignore").strip()

        m = re.search(r"\[download\]\s+(\d+(?:\.\d+)?)%", s)
        if m:
            percent = float(m.group(1))
            now = time.time()
            if (percent - last_percent >= 2.0) and (now - last_edit >= 8):
                last_percent = percent
                last_edit = now
                await safe_edit(
                    status_msg,
                    f"📥 Instagram Reel Detected ✅\n\n"
                    f"⬇️ Downloading Reel...\n\n"
                    f"{square_bar(percent)}\n\n"
                    f"⏳ Please wait...",
                    reply_markup=kb
                )

    await proc.wait()
    if proc.returncode != 0:
        raise Exception("Insta download failed (yt-dlp error)")

    base = outtmpl.replace("%(ext)s", "")
    for ext in ["mp4", "mkv", "webm"]:
        p = base + ext
        if os.path.exists(p):
            return p

    files = [f for f in os.listdir(DOWNLOAD_DIR) if f.startswith(f"insta_{uid}_")]
    if not files:
        raise Exception("Downloaded file not found")
    files.sort(key=lambda x: os.path.getmtime(os.path.join(DOWNLOAD_DIR, x)), reverse=True)
    return os.path.join(DOWNLOAD_DIR, files[0])


# =========================
# Upload animation
# =========================
async def upload_anim(uid: int, status_msg, label: str):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])
    step = 0
    last_edit = 0
    frames = ["⚪", "🟥", "🟧", "🟨", "🟩", "✅"]

    while True:
        if uid in USER_CANCEL:
            return

        now = time.time()
        if now - last_edit >= 8:
            last_edit = now
            step += 1
            fill = frames[step % len(frames)]
            bar = (fill * (step % 12)) + ("⬜" * (12 - (step % 12)))

            await safe_edit(
                status_msg,
                f"📥 Instagram Reel Detected ✅\n\n"
                f"⬆️ {label}\n\n"
                f"{bar}\n\n"
                f"⏳ Please wait...",
                reply_markup=kb
            )

        await asyncio.sleep(1.0)


# =========================
# ENTRY
# =========================
async def insta_entry(client, message, url: str, USER_TASKS, main_menu_keyboard):
    uid = message.from_user.id

    status = await safe_send(message, "📥 Instagram Reel Detected ✅\n\n⏳ Starting...")
    if not status:
        return

    async def job():
        file_path = None
        thumb_path = None
        anim_task = None
        try:
            USER_CANCEL.discard(uid)

            file_path = await insta_download(url, uid, status)

            if uid in USER_CANCEL:
                raise asyncio.CancelledError

            anim_task = asyncio.create_task(upload_anim(uid, status, "Uploading Reel..."))

            thumb_path = make_thumb(file_path)
            info = ffprobe_info(file_path)

            args = {}
            if info.get("duration", 0) > 0:
                args["duration"] = int(info["duration"])
            if info.get("width", 0) > 0:
                args["width"] = int(info["width"])
            if info.get("height", 0) > 0:
                args["height"] = int(info["height"])

            await client.send_video(
                chat_id=message.chat.id,
                video=file_path,
                caption="✅ Instagram Reel 🎥",
                supports_streaming=True,
                thumb=thumb_path if thumb_path and os.path.exists(thumb_path) else None,
                **args
            )

            if anim_task and not anim_task.done():
                anim_task.cancel()

            await safe_edit(status, "✅ Done ✅", reply_markup=main_menu_keyboard())

        except asyncio.CancelledError:
            if anim_task and not anim_task.done():
                anim_task.cancel()
            await safe_edit(status, "❌ Cancelled ✅", reply_markup=main_menu_keyboard())

        except Exception as e:
            if anim_task and not anim_task.done():
                anim_task.cancel()
            await safe_edit(status, f"❌ Insta Failed!\n\nError: `{e}`", reply_markup=main_menu_keyboard())

        finally:
            USER_CANCEL.discard(uid)

            try:
                if anim_task and not anim_task.done():
                    anim_task.cancel()
            except:
                pass

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
