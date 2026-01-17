import os
import time
import asyncio
import subprocess

from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")

try:
    from bot import USER_CANCEL
except:
    USER_CANCEL = set()


def is_terabox_url(text: str) -> bool:
    if not text:
        return False
    text = text.lower()
    return ("terabox" in text) and (text.startswith("http://") or text.startswith("https://"))


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


async def progress_anim(uid: int, status_msg, label: str):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{uid}")]])
    step = 0
    last = 0
    while True:
        if uid in USER_CANCEL:
            return
        now = time.time()
        if now - last > 10:
            last = now
            step += 1
            await safe_edit(
                status_msg,
                f"📦 Terabox Link Detected ✅\n\n"
                f"⚙️ {label}\n\n"
                f"{fancy_bar(step)}\n\n"
                f"⏳ Please wait...",
                reply_markup=kb
            )
        await asyncio.sleep(1)


async def terabox_download(url: str, uid: int):
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    outtmpl = os.path.join(DOWNLOAD_DIR, f"terabox_{uid}_{int(time.time())}.%(ext)s")

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--socket-timeout", "20",
        "--retries", "3",
        "-f", "bv*+ba/best",
        "--merge-output-format", "mp4",
        "-o", outtmpl,
        url
    ]

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
        raise Exception("Terabox download failed")

    mp4_path = outtmpl.replace("%(ext)s", "mp4")
    if os.path.exists(mp4_path):
        return mp4_path

    # fallback scan
    files = [f for f in os.listdir(DOWNLOAD_DIR) if f.startswith(f"terabox_{uid}_")]
    if not files:
        raise Exception("Downloaded file not found")
    files.sort(key=lambda x: os.path.getmtime(os.path.join(DOWNLOAD_DIR, x)), reverse=True)
    return os.path.join(DOWNLOAD_DIR, files[0])


async def terabox_entry(client, message, url: str, USER_TASKS, main_menu_keyboard):
    uid = message.from_user.id

    status = await safe_send(message, "📦 Terabox Link Detected ✅\n\n⏳ Starting...")
    if not status:
        return

    async def job():
        path = None
        anim = None
        try:
            USER_CANCEL.discard(uid)

            anim = asyncio.create_task(progress_anim(uid, status, "Downloading from Terabox..."))
            path = await terabox_download(url, uid)

            if anim and not anim.done():
                anim.cancel()

            anim = asyncio.create_task(progress_anim(uid, status, "Uploading..."))

            await client.send_video(
                chat_id=message.chat.id,
                video=path,
                caption="✅ Terabox Video 🎥",
                supports_streaming=True
            )

            if anim and not anim.done():
                anim.cancel()

            await safe_edit(status, "✅ Done ✅", reply_markup=main_menu_keyboard())

        except asyncio.CancelledError:
            try:
                if anim and not anim.done():
                    anim.cancel()
            except:
                pass
            await safe_edit(status, "❌ Cancelled ✅", reply_markup=main_menu_keyboard())

        except Exception as e:
            try:
                if anim and not anim.done():
                    anim.cancel()
            except:
                pass
            await safe_edit(status, f"❌ Terabox Failed!\n\nError: `{e}`", reply_markup=main_menu_keyboard())

        finally:
            USER_CANCEL.discard(uid)
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except:
                pass

    USER_TASKS[uid] = asyncio.create_task(job())
