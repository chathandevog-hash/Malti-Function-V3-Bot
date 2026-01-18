import os
import time
import asyncio
import aiohttp

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
# ✅ RapidAPI direct URL extraction (NO yt-dlp) ✅
# ===============================
async def terabox_extract_direct_url(url: str, uid: int) -> str:
    """
    Uses RapidAPI Terabox Direct Link Generator.
    Returns direct download url (mp4).
    """

    RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "").strip()
    RAPIDAPI_HOST = os.getenv("RAPIDAPI_HOST", "").strip()

    if not RAPIDAPI_KEY or not RAPIDAPI_HOST:
        raise Exception("RAPIDAPI_KEY / RAPIDAPI_HOST missing in env")

    api_url = f"https://{RAPIDAPI_HOST}/fetch"

    headers = {
        "Content-Type": "application/json",
        "x-rapidapi-host": RAPIDAPI_HOST,
        "x-rapidapi-key": RAPIDAPI_KEY,
    }

    payload = {"url": url}

    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(api_url, headers=headers, json=payload) as resp:
            raw_text = await resp.text()
            if resp.status != 200:
                raise Exception(f"Terabox API HTTP {resp.status}: {raw_text}")

            try:
                data = await resp.json(content_type=None)
            except:
                raise Exception(f"Terabox API invalid JSON: {raw_text}")

    # ✅ Parse possible fields
    direct = None

    if isinstance(data, dict):
        # format A
        direct = data.get("download_url") or data.get("direct_url") or data.get("url")

        # format B
        if not direct and isinstance(data.get("data"), dict):
            direct = (
                data["data"].get("download_url")
                or data["data"].get("direct_url")
                or data["data"].get("url")
            )

        # format C
        if not direct and isinstance(data.get("result"), dict):
            direct = (
                data["result"].get("download_url")
                or data["result"].get("direct_url")
                or data["result"].get("url")
            )

        # format D: list
        if not direct and isinstance(data.get("data"), list) and len(data["data"]) > 0:
            first = data["data"][0]
            if isinstance(first, dict):
                direct = first.get("download_url") or first.get("direct_url") or first.get("url")

    if not direct or not str(direct).startswith("http"):
        raise Exception(f"Terabox API no direct link: {data}")

    return str(direct).strip()


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

            # ✅ Step 1: Extract direct url from API
            anim = asyncio.create_task(progress_anim(uid, status, "Getting direct download link..."))
            direct_url = await terabox_extract_direct_url(url, uid)

            if anim and not anim.done():
                anim.cancel()

            if uid in USER_CANCEL:
                raise asyncio.CancelledError

            # ✅ Step 2: Auto URL Uploader (User won't see direct link)
            anim = asyncio.create_task(progress_anim(uid, status, "Uploading video..."))

            from url import url_flow  # avoid circular import

            # ✅ call URL uploader silently
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
