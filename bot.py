import os
import asyncio
import time

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait
from pyrogram.errors.exceptions.bad_request_400 import MessageNotModified

from config import BOT_TOKEN, API_ID, API_HASH, DOWNLOAD_DIR

# ✅ Modules
from url import is_url, url_flow, url_callback_router
from insta import is_instagram_url, clean_insta_url, insta_entry

# ✅ Spotify module (AUTO START)
from spotify import is_spotify_url, spotify_auto_download


# ===========================
# GLOBALS
# ===========================
USER_TASKS = {}
USER_CANCEL = set()
USER_STATE = {}
UI_STATUS_MSG = {}
LAST_WARN = {}
LAST_MENU_EDIT = {}


# ===========================
# FLOOD SAFE HELPERS ✅
# ===========================
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
        except MessageNotModified:
            return
        except FloodWait as e:
            await asyncio.sleep(int(e.value) + 1)
        except:
            return


async def safe_answer(cb, text="✅"):
    try:
        await cb.answer(text, show_alert=False)
    except FloodWait as e:
        await asyncio.sleep(int(e.value) + 1)
    except:
        pass


# ===========================
# HELPERS
# ===========================
async def get_or_create_status(message, uid):
    if uid in UI_STATUS_MSG:
        return UI_STATUS_MSG[uid]
    status = await safe_send(message, "⏳ Processing...")
    UI_STATUS_MSG[uid] = status
    return status


def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🌐 URL Uploader", callback_data="menu_url"),
            InlineKeyboardButton("📸 Instagram", callback_data="menu_insta")
        ],
        [
            InlineKeyboardButton("🎧 Spotify", callback_data="menu_spotify")
        ]
    ])


def back_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_main")]])


WELCOME_TEXT = (
    "✨ **Welcome to Multifunctional Bot! 🤖💫**\n\n"
    "🌐 **URL Uploader**\n"
    "➜ Send any direct link and I will upload it ✅\n"
    "⚠️ Limit: **2GB**\n\n"
    "📸 **Instagram Reel Downloader**\n"
    "➜ Send Reel link ✅\n\n"
    "🎧 **Spotify Music Downloader**\n"
    "➜ Send Spotify link ✅ (Auto Start)\n\n"
    "📌 How to use?\n"
    "1️⃣ Send a URL / Insta / Spotify link\n"
    "2️⃣ Wait processing ⏳\n"
    "3️⃣ Get output 🎉\n\n"
    "🚀 Now send something to start 👇😊"
)


# ===========================
# BOT INIT
# ===========================
app = Client(
    "MultiFunctionBot",
    bot_token=BOT_TOKEN,
    api_id=API_ID,
    api_hash=API_HASH
)


# ===========================
# START / BACK
# ===========================
@app.on_message(filters.private & filters.command("start"))
async def start_cmd(client, message):
    uid = message.from_user.id
    USER_STATE.pop(uid, None)
    await safe_send(message, WELCOME_TEXT, reply_markup=main_menu_keyboard())


@app.on_callback_query(filters.regex("^back_main$"))
async def back_main(client, cb):
    uid = cb.from_user.id
    USER_STATE.pop(uid, None)
    await safe_answer(cb)
    await safe_edit(cb.message, WELCOME_TEXT, reply_markup=main_menu_keyboard())


# ===========================
# MENU EDIT GUARD ✅
# ===========================
async def guarded_menu_edit(cb, uid, text):
    if LAST_MENU_EDIT.get(uid) == text:
        return
    LAST_MENU_EDIT[uid] = text
    await safe_edit(cb.message, text, reply_markup=back_keyboard())


# ===========================
# MENUS
# ===========================
@app.on_callback_query(filters.regex("^menu_url$"))
async def menu_url(client, cb):
    uid = cb.from_user.id
    USER_STATE[uid] = "WAIT_URL"
    await safe_answer(cb)
    await guarded_menu_edit(cb, uid, "🌐 **URL Uploader Mode**\n\nSend direct URL 👇")


@app.on_callback_query(filters.regex("^menu_insta$"))
async def menu_insta(client, cb):
    uid = cb.from_user.id
    USER_STATE[uid] = "WAIT_INSTA"
    await safe_answer(cb)
    await guarded_menu_edit(cb, uid, "📸 **Instagram Mode**\n\nSend Reel URL 👇")


@app.on_callback_query(filters.regex("^menu_spotify$"))
async def menu_spotify(client, cb):
    uid = cb.from_user.id
    USER_STATE[uid] = "WAIT_SPOTIFY"
    await safe_answer(cb)
    await guarded_menu_edit(cb, uid, "🎧 **Spotify Mode**\n\nSend Spotify link 👇 (Auto Start)")


# ===========================
# CANCEL
# ===========================
@app.on_callback_query(filters.regex("^cancel_"))
async def cancel_task(client, cb):
    try:
        uid = int(cb.data.split("_", 1)[1])
    except:
        return await safe_answer(cb, "Invalid")

    USER_CANCEL.add(uid)

    task = USER_TASKS.get(uid)
    if task and not task.done():
        task.cancel()

    await safe_answer(cb, "✅ Cancelled!")
    await safe_edit(cb.message, "❌ Cancelled ✅", reply_markup=main_menu_keyboard())


# ===========================
# CALLBACK ROUTER
# ===========================
@app.on_callback_query()
async def all_callbacks(client, cb):
    data = cb.data

    # ✅ URL uploader callbacks
    if data.startswith("url_"):
        return await url_callback_router(
            client, cb,
            USER_TASKS, USER_CANCEL,
            get_or_create_status,
            main_menu_keyboard,
            DOWNLOAD_DIR
        )

    await safe_answer(cb)


# ===========================
# TEXT HANDLER (Auto Detect)
# ===========================
@app.on_message(filters.private & filters.text)
async def text_handler(client, message):
    uid = message.from_user.id
    text = message.text.strip()

    if text.startswith("/"):
        return

    # ✅ Spotify auto detect + Auto start
    if is_spotify_url(text):
        USER_STATE[uid] = "WAIT_SPOTIFY"
        return await spotify_auto_download(
            client,
            message,
            text,
            USER_TASKS,
            USER_CANCEL,
            get_or_create_status,
            main_menu_keyboard,
            DOWNLOAD_DIR
        )

    # ✅ Insta auto
    if is_instagram_url(text):
        return await insta_entry(client, message, clean_insta_url(text), USER_TASKS, main_menu_keyboard)

    # ✅ URL auto
    if is_url(text):
        USER_STATE[uid] = "WAIT_URL"
        return await url_flow(client, message, text)

    state = USER_STATE.get(uid, "")

    if state == "WAIT_URL":
        return await url_flow(client, message, text)

    if state == "WAIT_INSTA":
        if is_instagram_url(text):
            return await insta_entry(client, message, clean_insta_url(text), USER_TASKS, main_menu_keyboard)
        return await safe_send(message, "❌ Instagram Reel link ayakku ✅", reply_markup=back_keyboard())

    if state == "WAIT_SPOTIFY":
        if is_spotify_url(text):
            return await spotify_auto_download(
                client,
                message,
                text,
                USER_TASKS,
                USER_CANCEL,
                get_or_create_status,
                main_menu_keyboard,
                DOWNLOAD_DIR
            )
        return await safe_send(message, "❌ Spotify music link ayakku ✅", reply_markup=back_keyboard())

    # anti spam
    now = time.time()
    if now - LAST_WARN.get(uid, 0) > 15:
        LAST_WARN[uid] = now
        return await safe_send(message, "❌ Menu select cheyyu ✅", reply_markup=main_menu_keyboard())


# ===========================
# RUN
# ===========================
if __name__ == "__main__":
    if not BOT_TOKEN or not API_ID or not API_HASH:
        print("❌ Please set BOT_TOKEN, API_ID, API_HASH in env!")
        raise SystemExit

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    print("✅ Bot started...")
    app.run()
