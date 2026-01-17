import os
import re
import time
import json
import asyncio
import subprocess

from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")

YT_REGEX = re.compile(
    r"(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)[A-Za-z0-9_\-]+"
)

try:
    from bot import USER_CANCEL
except:
    USER_CANCEL = set()


def is_youtube_url(text: str) -> bool:
    return bool(YT_REGEX.search(text or ""))


def clean_youtube_url(text: str) -> str:
    return (text or "").strip()


async def safe_edit(msg, text, reply_markup=None):
    try:
        await msg.edit(text, reply_markup=reply_markup)
    except:
        pass


# =========================
# UI
# =========================
def fmt_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎥 Video", callback_data="yt_fmt_video"),
            InlineKeyboardButton("📁 File", callback_data="yt_fmt_file"),
            InlineKeyboardButton("🎵 Audio", callback_data="yt_fmt_audio"),
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
    ])


def quality_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1080p", callback_data="yt_q_1080p"),
            InlineKeyboardButton("720p", callback_data="yt_q_720p"),
            InlineKeyboardButton("480p", callback_data="yt_q_480p"),
        ],
        [
            InlineKeyboardButton("360p", callback_data="yt_q_360p"),
            InlineKeyboardButton("240p", callback_data="yt_q_240p"),
            InlineKeyboardButton("144p", callback_data="yt_q_144p"),
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="yt_back_fmt")]
    ])


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


async def progress_animator(uid: int, status_msg, label: str):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])
    step = 0
    while True:
        if uid in USER_CANCEL:
            return
        step += 1
        await safe_edit(
            status_msg,
            f"▶️ YouTube Detected ✅\n\n"
            f"⚙️ {label}\n\n"
            f"{fancy_bar(step)}\n\n"
            f"⏳ Please wait...",
            reply_markup=kb
        )
        await asyncio.sleep(2.3)


# =========================
# State
# =========================
YT_STATE = {}  # uid -> {"url": str, "fmt": str}


# =========================
# FFPROBE META + THUMB
# =========================
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


# =========================
# DOWNLOAD
# =========================
def _yt_quality_format(q: str):
    h = int(q.replace("p", ""))
    return f"bv*[height<={h}]+ba/b[height<={h}]/best"


async def youtube_download(url: str, uid: int, quality: str):
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    outtmpl = os.path.join(DOWNLOAD_DIR, f"yt_{uid}_{int(time.time())}.%(ext)s")

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--newline",
        "--progress",
        "--retries", "10",
        "--socket-timeout", "30",
        "--force-overwrites",
        "-f", _yt_quality_format(quality),
        "--merge-output-format", "mp4",
        "-o", outtmpl,
        url
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )

    last_lines = []
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

        txt = line.decode(errors="ignore").strip()
        if txt:
            last_lines.append(txt)
            last_lines = last_lines[-40:]

    await proc.wait()

    if proc.returncode != 0:
        joined = "\n".join(last_lines).lower()

        if "private" in joined:
            raise Exception("Video is private / restricted ❌")
        if "sign in" in joined or "login" in joined:
            raise Exception("Login required (age restricted / members only) ❌")
        if "403" in joined:
            raise Exception("HTTP 403 (blocked/throttled). Change server/IP or try later ❌")
        if "404" in joined or "not available" in joined:
            raise Exception("Video unavailable / deleted ❌")

        raise Exception("YouTube download failed ❌")

    mp4_path = outtmpl.replace("%(ext)s", "mp4")
    if os.path.exists(mp4_path):
        return mp4_path

    # fallback search
    files = [f for f in os.listdir(DOWNLOAD_DIR) if f.startswith(f"yt_{uid}_") and f.endswith(".mp4")]
    if not files:
        raise Exception("Downloaded mp4 not found ❌")
    files.sort(key=lambda x: os.path.getmtime(os.path.join(DOWNLOAD_DIR, x)), reverse=True)
    return os.path.join(DOWNLOAD_DIR, files[0])


# =========================
# ENTRY + CALLBACKS
# =========================
async def youtube_entry(client, message, url: str):
    uid = message.from_user.id
    YT_STATE[uid] = {"url": url, "fmt": None}

    await message.reply(
        f"▶️ **YouTube Video Detected ✅**\n\n📌 {url}\n\n👇 Choose format:",
        reply_markup=fmt_keyboard()
    )


async def youtube_callback_router(client, cb, USER_TASKS, USER_CANCEL, get_or_create_status, main_menu_keyboard, DOWNLOAD_DIR):
    uid = cb.from_user.id
    data = cb.data

    if data == "yt_back_fmt":
        await cb.answer()
        url = YT_STATE.get(uid, {}).get("url", "")
        return await cb.message.edit(
            f"▶️ **YouTube Video Detected ✅**\n\n📌 {url}\n\n👇 Choose format:",
            reply_markup=fmt_keyboard()
        )

    if data.startswith("yt_fmt_"):
        await cb.answer()
        fmt = data.replace("yt_fmt_", "")
        st = YT_STATE.get(uid) or {}
        st["fmt"] = fmt
        YT_STATE[uid] = st

        return await cb.message.edit(
            f"🎯 Selected: **{fmt.upper()}**\n\n👇 Select quality:",
            reply_markup=quality_keyboard()
        )

    if data.startswith("yt_q_"):
        await cb.answer()
        quality = data.replace("yt_q_", "")
        st = YT_STATE.get(uid) or {}
        url = st.get("url")
        fmt = st.get("fmt", "video")

        if not url:
            return await cb.message.edit("❌ Session expired. Send YouTube link again.", reply_markup=main_menu_keyboard())

        status = await get_or_create_status(cb.message, uid)

        async def job():
            file_path = None
            thumb_path = None
            anim_task = None
            try:
                USER_CANCEL.discard(uid)

                anim_task = asyncio.create_task(progress_animator(uid, status, f"Downloading ({quality})..."))
                file_path = await youtube_download(url, uid, quality)

                if anim_task and not anim_task.done():
                    anim_task.cancel()

                anim_task = asyncio.create_task(progress_animator(uid, status, "Uploading..."))

                if fmt == "audio":
                    mp3_path = os.path.splitext(file_path)[0] + ".mp3"
                    proc = await asyncio.create_subprocess_exec(
                        "ffmpeg", "-y", "-i", file_path,
                        "-vn", "-acodec", "libmp3lame", "-b:a", "128k",
                        mp3_path,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL
                    )
                    await proc.wait()

                    await client.send_audio(cb.message.chat.id, audio=mp3_path, caption=f"✅ YouTube Audio 🎵 ({quality})")
                    try:
                        os.remove(mp3_path)
                    except:
                        pass

                elif fmt == "file":
                    await client.send_document(cb.message.chat.id, document=file_path, caption=f"✅ YouTube File 📁 ({quality})")

                else:
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
                        cb.message.chat.id,
                        video=file_path,
                        caption=f"✅ YouTube Video 🎥 ({quality})",
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
                await safe_edit(status, f"❌ YouTube Failed!\n\nError: `{e}`", reply_markup=main_menu_keyboard())

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
