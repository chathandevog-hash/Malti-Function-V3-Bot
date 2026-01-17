import os
import re
import time
import asyncio

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

def quality_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1080p", callback_data="yt_q_1080p"),
         InlineKeyboardButton("720p", callback_data="yt_q_720p"),
         InlineKeyboardButton("480p", callback_data="yt_q_480p")],
        [InlineKeyboardButton("360p", callback_data="yt_q_360p"),
         InlineKeyboardButton("240p", callback_data="yt_q_240p"),
         InlineKeyboardButton("144p", callback_data="yt_q_144p")],
        [InlineKeyboardButton("⬅️ Back", callback_data="yt_back_fmt")]
    ])

def fmt_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎥 Video", callback_data="yt_fmt_video"),
            InlineKeyboardButton("📁 File", callback_data="yt_fmt_file"),
            InlineKeyboardButton("🎵 Audio", callback_data="yt_fmt_audio"),
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
    ])

YT_STATE = {}

async def youtube_entry(client, message, url: str):
    uid = message.from_user.id
    YT_STATE[uid] = {"url": url}

    await message.reply(
        f"✅ YouTube Link Detected ▶️\n\n📌 {url}\n\n👇 Choose format:",
        reply_markup=fmt_keyboard()
    )

def _yt_quality_format(q: str):
    h = int(q.replace("p", ""))
    return f"bv*[height<={h}]+ba/b[height<={h}]/best"

async def youtube_download(url: str, uid: int, quality: str, status_msg=None):
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    outtmpl = os.path.join(DOWNLOAD_DIR, f"yt_{uid}_{int(time.time())}.%(ext)s")

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--newline",
        "--progress",
        "--retries", "5",
        "--socket-timeout", "30",
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

    last_anim = 0
    anim_state = 0
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])

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

        if status_msg and time.time() - last_anim > 3:
            last_anim = time.time()
            anim_state = (anim_state + 1) % 3
            dots = "." * (anim_state + 1)

            await safe_edit(
                status_msg,
                f"📥 Downloading YouTube ({quality}){dots}\n\n🟠🟠🟠🟠🟠⚪⚪⚪⚪⚪⚪⚪⚪⚪\n\n⏳ Please wait...",
                reply_markup=kb
            )

    await proc.wait()
    if proc.returncode != 0:
        raise Exception("YouTube download failed")

    mp4_path = outtmpl.replace("%(ext)s", "mp4")
    if os.path.exists(mp4_path):
        return mp4_path

    # fallback
    files = [f for f in os.listdir(DOWNLOAD_DIR) if f.startswith(f"yt_{uid}_") and f.endswith(".mp4")]
    if not files:
        raise Exception("Downloaded mp4 not found")
    files.sort(key=lambda x: os.path.getmtime(os.path.join(DOWNLOAD_DIR, x)), reverse=True)
    return os.path.join(DOWNLOAD_DIR, files[0])

async def youtube_callback_router(client, cb, USER_TASKS, USER_CANCEL, get_or_create_status, main_menu_keyboard, DOWNLOAD_DIR):
    uid = cb.from_user.id
    data = cb.data

    if data == "yt_back_fmt":
        await cb.answer()
        url = YT_STATE.get(uid, {}).get("url", "")
        return await cb.message.edit(
            f"✅ YouTube Link Detected ▶️\n\n📌 {url}\n\n👇 Choose format:",
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
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])

        async def job():
            file_path = None
            try:
                USER_CANCEL.discard(uid)
                await safe_edit(status, f"📥 Starting YouTube download ({quality})...", kb)

                file_path = await youtube_download(url, uid, quality, status_msg=status)

                if uid in USER_CANCEL:
                    raise asyncio.CancelledError

                await safe_edit(status, "📤 Uploading...", kb)

                if fmt == "audio":
                    mp3_path = os.path.splitext(file_path)[0] + ".mp3"
                    proc = await asyncio.create_subprocess_exec(
                        "ffmpeg", "-y", "-i", file_path, "-vn",
                        "-acodec", "libmp3lame", "-b:a", "128k",
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
                    await client.send_video(cb.message.chat.id, video=file_path, caption=f"✅ YouTube Video 🎥 ({quality})", supports_streaming=True)

                await safe_edit(status, "✅ Done ✅", reply_markup=main_menu_keyboard())

            except asyncio.CancelledError:
                await safe_edit(status, "❌ Cancelled ✅", reply_markup=main_menu_keyboard())
            except Exception as e:
                await safe_edit(status, f"❌ YouTube Failed!\n\nError: `{e}`", reply_markup=main_menu_keyboard())
            finally:
                USER_CANCEL.discard(uid)
                try:
                    if file_path and os.path.exists(file_path):
                        os.remove(file_path)
                except:
                    pass

        USER_TASKS[uid] = asyncio.create_task(job())
