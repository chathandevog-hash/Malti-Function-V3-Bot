import os
import re
import time
import json
import asyncio
import subprocess

from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto, InputMediaVideo
from pyrogram.errors import FloodWait

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")

# ✅ support reel + post + tv
INSTA_REGEX = re.compile(r"(https?://(www\.)?instagram\.com/(reel|p|tv)/[A-Za-z0-9_\-]+)")

try:
    from bot import USER_CANCEL
except:
    USER_CANCEL = set()


# ===============================
# URL HELPERS
# ===============================
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
# UI + PROGRESS
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


async def progress_animator(uid: int, status_msg, header: str, label: str):
    """
    ✅ safe edit interval (avoid FloodWait)
    """
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])
    step = 0
    last_edit = 0

    while True:
        if uid in USER_CANCEL:
            return

        now = time.time()
        if now - last_edit >= 11:
            last_edit = now
            step += 1
            await safe_edit(
                status_msg,
                f"{header}\n\n"
                f"⚙️ {label}\n\n"
                f"{fancy_bar(step)}\n\n"
                f"⏳ Please wait...",
                reply_markup=kb
            )

        await asyncio.sleep(1.0)


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
# yt-dlp download (FASTER) ✅
# ===============================
async def insta_download(url: str, uid: int):
    """
    ✅ supports reel + post + carousel
    ✅ tries best speed settings
    """
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    url = clean_insta_url(url)

    # save in per-user folder
    user_dir = os.path.join(DOWNLOAD_DIR, f"insta_{uid}")
    os.makedirs(user_dir, exist_ok=True)

    outtmpl = os.path.join(user_dir, f"%(title).80s_{uid}_{int(time.time())}.%(ext)s")

    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--no-playlist",  # ig single post is okay (carousel still extracted)
        "--socket-timeout", "25",
        "--retries", "6",
        "--concurrent-fragments", "8",  # ✅ faster downloads
        "--fragment-retries", "6",
        "--force-overwrites",
        "--restrict-filenames",
        "-f", "bv*+ba/b/best",
        "-o", outtmpl,
        url
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )

    # wait for finish
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

    # collect files from folder
    files = []
    for f in os.listdir(user_dir):
        p = os.path.join(user_dir, f)
        if os.path.isfile(p) and os.path.getsize(p) > 1000:
            if p.lower().endswith((".mp4", ".mkv", ".webm", ".jpg", ".jpeg", ".png")):
                files.append(p)

    if not files:
        raise Exception("Downloaded file not found")

    # newest first
    files.sort(key=lambda x: os.path.getmtime(x), reverse=True)
    return files


# =========================
# SEND OUTPUT (POST/REEL/CAROUSEL)
# =========================
async def send_insta_files(client, chat_id: int, files: list, caption: str):
    """
    ✅ If multiple files -> media group
    ✅ If single -> send photo/video
    """
    if not files:
        return

    # prefer to send max 10 in album
    if len(files) > 1:
        medias = []
        for p in files[:10]:
            if p.lower().endswith((".jpg", ".jpeg", ".png")):
                medias.append(InputMediaPhoto(media=p))
            else:
                medias.append(InputMediaVideo(media=p, supports_streaming=True))
        # put caption only on first
        if medias:
            medias[0].caption = caption
        await client.send_media_group(chat_id=chat_id, media=medias)
        return

    p = files[0]
    if p.lower().endswith((".jpg", ".jpeg", ".png")):
        await client.send_photo(chat_id=chat_id, photo=p, caption=caption)
    else:
        thumb = make_thumb(p)
        info = ffprobe_info(p)
        args = {}
        if info.get("duration", 0) > 0:
            args["duration"] = int(info["duration"])
        if info.get("width", 0) > 0:
            args["width"] = int(info["width"])
        if info.get("height", 0) > 0:
            args["height"] = int(info["height"])

        await client.send_video(
            chat_id=chat_id,
            video=p,
            caption=caption,
            supports_streaming=True,
            thumb=thumb if thumb and os.path.exists(thumb) else None,
            **args
        )


# =========================
# ENTRY (AUTO MODE)
# =========================
async def insta_entry(client, message, url: str, USER_TASKS, main_menu_keyboard):
    uid = message.from_user.id
    url = clean_insta_url(url)

    header = "📸 Instagram Detected ✅"
    if "/reel/" in url:
        header = "📥 Instagram Reel Detected ✅"
    elif "/p/" in url:
        header = "🖼️ Instagram Post Detected ✅"
    elif "/tv/" in url:
        header = "🎞️ Instagram TV Detected ✅"

    status = await safe_send(message, f"{header}\n\n⏳ Starting...")
    if not status:
        return

    async def job():
        files = []
        anim_task = None
        try:
            USER_CANCEL.discard(uid)

            anim_task = asyncio.create_task(progress_animator(uid, status, header, "Downloading..."))
            files = await insta_download(url, uid)

            if anim_task and not anim_task.done():
                anim_task.cancel()

            anim_task = asyncio.create_task(progress_animator(uid, status, header, "Uploading..."))

            await send_insta_files(
                client,
                message.chat.id,
                files,
                caption="✅ Instagram Download Complete 🎉"
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

            # cleanup files
            try:
                for p in files:
                    if p and os.path.exists(p):
                        os.remove(p)
            except:
                pass

            # cleanup folder
            try:
                user_dir = os.path.join(DOWNLOAD_DIR, f"insta_{uid}")
                if os.path.isdir(user_dir):
                    for f in os.listdir(user_dir):
                        fp = os.path.join(user_dir, f)
                        try:
                            os.remove(fp)
                        except:
                            pass
            except:
                pass

            try:
                if anim_task and not anim_task.done():
                    anim_task.cancel()
            except:
                pass

    USER_TASKS[uid] = asyncio.create_task(job())
