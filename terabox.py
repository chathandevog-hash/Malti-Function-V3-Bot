import os
import time
import asyncio
import subprocess

from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait

try:
    from bot import USER_CANCEL
except:
    USER_CANCEL = set()


def is_terabox_url(text: str) -> bool:
    if not text:
        return False
    t = text.lower().strip()
    return (t.startswith("http://") or t.startswith("https://")) and ("terabox" in t)


# ===============================
# FLOOD SAFE HELPERS ✅
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
        if now - last > 10:  # ✅ safe interval
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


# ===============================
# ✅ Extract direct URL only (No download)
# ===============================
async def terabox_extract_direct_url(url: str, uid: int) -> str:
    """
    Uses yt-dlp -g to extract direct stream URL.
    This does NOT download file. Only returns a direct URL.
    """
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--socket-timeout", "20",
        "--retries", "3",
        "-g",
        url
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    while True:
        if uid in USER_CANCEL:
            try:
                proc.kill()
            except:
                pass
            raise asyncio.CancelledError
        if proc.returncode is not None:
            break
        await asyncio.sleep(0.5)

    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise Exception("Terabox direct link extract failed")

    lines = out.decode(errors="ignore").strip().splitlines()
    if not lines:
        raise Exception("No direct link found")

    direct = lines[0].strip()
    if not direct.startswith("http"):
        raise Exception("Invalid direct link")

    return direct


# ===============================
# ENTRY
# ===============================
async def terabox_entry(client, message, url: str, USER_TASKS, main_menu_keyboard):
    uid = message.from_user.id

    status = await safe_send(message, "📦 Terabox Link Detected ✅\n\n⏳ Starting...")
    if not status:
        return

    async def job():
        anim = None
        try:
            USER_CANCEL.discard(uid)

            # ✅ Step 1: Extract direct stream url
            anim = asyncio.create_task(progress_anim(uid, status, "Extracting direct link..."))
            direct_url = await terabox_extract_direct_url(url, uid)

            if anim and not anim.done():
                anim.cancel()

            if uid in USER_CANCEL:
                raise asyncio.CancelledError

            # ✅ Step 2: Auto URL Uploader (User won't see direct link)
            anim = asyncio.create_task(progress_anim(uid, status, "Uploading video..."))

            # Import URL uploader flow inside function to avoid circular imports
            from url import url_flow

            # call url uploader silently
            await url_flow(client, message, direct_url)

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

    USER_TASKS[uid] = asyncio.create_task(job())
