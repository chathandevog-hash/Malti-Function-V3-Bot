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


# ===============================
# Fancy bar
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


# ===============================
# Progress Animator (FLOODSAFE) ✅
# ===============================
async def progress_animator(uid: int, status_msg, label: str):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])
    step = 0
    last_edit = 0

    while True:
        if uid in USER_CANCEL:
            return

        now = time.time()
        if now - last_edit >= 11:  # ✅ safe
            last_edit = now
            step += 1
            await safe_edit(
                status_msg,
                f"📥 Instagram Reel Detected ✅\n\n"
                f"⚙️ {label}\n\n"
                f"{fancy_bar(step)}\n\n"
                f"⏳ Please wait...",
                reply_markup=kb
            )

        await asyncio.sleep(1.0)


def has_aria2c():
    try:
        subprocess.run(["aria2c", "-v"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except:
        return False


# ===============================
# yt-dlp download (FAST MODE ✅)
# ===============================
async def insta_download(url: str, uid: int):
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    url = clean_insta_url(url)

    outtmpl = os.path.join(DOWNLOAD_DIR, f"insta_{uid}_{int(time.time())}.%(ext)s")

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--socket-timeout", "15",
        "--retries", "3",

        # ✅ FAST: avoid heavy merge when possible
        "-f", "best[ext=mp4]/best",

        "-o", outtmpl,
        url
    ]

    # ✅ aria2c speed boost if available
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

    await proc.wait()

    if proc.returncode != 0:
        raise Exception("Insta download failed")

    # ✅ find produced mp4
    base = outtmpl.replace("%(ext)s", "")
    for ext in ["mp4", "mkv", "webm"]:
        p = base + ext
        if os.path.exists(p):
            return p

    # fallback scan
    files = [f for f in os.listdir(DOWNLOAD_DIR) if f.startswith(f"insta_{uid}_")]
    if not files:
        raise Exception("Downloaded file not found")
    files.sort(key=lambda x: os.path.getmtime(os.path.join(DOWNLOAD_DIR, x)), reverse=True)
    return os.path.join(DOWNLOAD_DIR, files[0])


# =========================
# ENTRY (AUTO MODE)
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

            anim_task = asyncio.create_task(progress_animator(uid, status, "Downloading reel..."))
            file_path = await insta_download(url, uid)

            if anim_task and not anim_task.done():
                anim_task.cancel()

            anim_task = asyncio.create_task(progress_animator(uid, status, "Uploading reel..."))

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
            try:
                if anim_task and not anim_task.done():
                    anim_task.cancel()
            except:
                pass
            await safe_edit(status, "❌ Cancelled ✅", reply_markup=main_menu_keyboard())

        except Exception as e:
            try:
                if anim_task and not anim_task.done():
                    anim_task.cancel()
            except:
                pass
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
